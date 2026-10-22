"""Download one playlist, track by track, with pacing and retries."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import imageio_ffmpeg
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, DownloadError, sanitize_filename

from ltube.backoff import BackoffConfig, inner_retry_sleep
from ltube.metadata import (
    ALBUM_PLAYLIST_PREFIX,
    ALBUM_TITLE_PREFIX,
    DEFAULT_NAME_PATTERN,
    NormalizeMetadataPP,
    TrackNamePP,
    album_folder_name,
    track_file_name,
    scan_existing,
    write_m3u8,
)

# Final extensions we produce: AAC stays in m4a, Opus is moved from webm into an .opus
# (Ogg) container. Both are stream copies, the audio is never re-encoded.
AUDIO_EXTS = ("m4a", "opus")

# YouTube thumbnails are 16:9; center-crop to a square cover and keep JPEG quality high.
COVER_FFMPEG_ARGS = [
    "-c:v", "mjpeg", "-qmin", "1", "-qscale:v", "1",
    "-vf", "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'",
]  # fmt: skip

# Checked in order: a rate-limit hint wins over anything else in the same message.
_RATE_LIMITED_MARKERS = ("http error 429", "too many requests", "not a bot", "rate-limited")
_UNAVAILABLE_MARKERS = (
    "video unavailable",
    "video is unavailable",
    "private video",
    "this video is not available",
    "has been removed",
    "copyright",
    "available in your country",
    "members-only",
    "join this channel",
    "account associated with this video has been terminated",
)


class ErrorKind(Enum):
    RATE_LIMITED = "rate_limited"  # stop the whole run
    UNAVAILABLE = "unavailable"  # skip the track, retrying won't help
    TRANSIENT = "transient"  # retry with backoff


def classify(message: str) -> ErrorKind:
    lowered = message.lower()
    if any(m in lowered for m in _RATE_LIMITED_MARKERS):
        return ErrorKind.RATE_LIMITED
    if any(m in lowered for m in _UNAVAILABLE_MARKERS):
        return ErrorKind.UNAVAILABLE
    return ErrorKind.TRANSIENT


class RateLimitedError(Exception):
    """YouTube is throttling or challenging this session; the run must stop."""


class NotAPlaylistError(Exception):
    pass


class Cancelled(DownloadCancelled):
    """The caller asked the run to stop.

    Subclasses yt-dlp's DownloadCancelled so it propagates out of a progress hook
    instead of being swallowed as a download error.
    """

    msg = "cancelled"


class Status(Enum):
    DOWNLOADED = "downloaded"
    EXISTS = "already exists"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass
class TrackResult:
    status: Status
    path: Path | None = None
    label: str = ""
    duration: float | None = None
    error: str | None = None


@dataclass
class Summary:
    playlist_title: str
    playlist_dir: Path
    total: int
    results: list[TrackResult] = field(default_factory=list)
    aborted: str | None = None
    removed: list[Path] = field(default_factory=list)  # deleted because they left the playlist

    def count(self, status: Status) -> int:
        return sum(r.status is status for r in self.results)


@dataclass(frozen=True)
class Subfolders:
    """Sort downloads into OUT/<albums>/... and OUT/<playlists>/... (relative paths)."""

    albums: str
    playlists: str


@dataclass(frozen=True)
class NamePatterns:
    """Track file name patterns (see metadata.render_name), chosen by playlist kind."""

    album: str = DEFAULT_NAME_PATTERN
    playlist: str = DEFAULT_NAME_PATTERN


@dataclass(frozen=True)
class PlaylistStarted:
    title: str
    directory: Path
    total: int


@dataclass(frozen=True)
class TrackStarted:
    index: int  # 1-based playlist position
    total: int
    title: str


@dataclass(frozen=True)
class TrackProgress:
    index: int
    downloaded_bytes: int
    total_bytes: int | None
    speed: float | None  # bytes per second


@dataclass(frozen=True)
class TrackRetry:
    index: int
    attempt: int  # 1-based number of the attempt that failed
    delay: float
    message: str


@dataclass(frozen=True)
class TrackFinished:
    index: int
    result: TrackResult


@dataclass(frozen=True)
class TrackRenamed:
    """A file already on disk was renamed to match the current name pattern."""

    old: Path
    new: Path


@dataclass(frozen=True)
class TrackRemoved:
    """A file was deleted because its track is no longer in the playlist (prune)."""

    path: Path
    label: str


@dataclass(frozen=True)
class PruneSkipped:
    """Pruning was requested but judged unsafe for this run; nothing was deleted."""

    reason: str


@dataclass(frozen=True)
class Waiting:
    seconds: float


@dataclass(frozen=True)
class YtdlpWarning:
    message: str


Event = (
    PlaylistStarted
    | TrackStarted
    | TrackProgress
    | TrackRetry
    | TrackFinished
    | TrackRenamed
    | TrackRemoved
    | PruneSkipped
    | Waiting
    | YtdlpWarning
)

# yt-dlp calls progress hooks many times a second; forward at most this often.
_PROGRESS_INTERVAL = 0.5

# YouTube hides unavailable videos from playlists. yt-dlp asks for them, but if that
# request fails it carries on silently with the shorter list, and pruning would then
# delete tracks that are still in the playlist (just unplayable).
_HIDDEN_UNAVAILABLE_RE = re.compile(r"unavailable videos? (?:is|are|will be) hidden", re.IGNORECASE)
# Refuse to prune more than this share of a folder's tracks in one run.
_PRUNE_MAX_SHARE = 0.5


@dataclass(frozen=True)
class _PlaylistItem:
    id: str
    url: str
    title: str


class _Logger:
    """Route yt-dlp output: chatter only when verbose, warnings as events (and recorded,
    for checks like hidden playlist entries). Errors surface as exceptions.
    """

    def __init__(self, emit: Callable[[Event], None], verbose: bool) -> None:
        self._emit = emit
        self._verbose = verbose
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        if self._verbose:
            print(msg)

    def info(self, msg: str) -> None:
        self.debug(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)
        if self._verbose:
            print(f"WARNING: {msg}", file=sys.stderr)
        else:
            self._emit(YtdlpWarning(msg))

    def error(self, msg: str) -> None:
        if self._verbose:
            print(msg, file=sys.stderr)


class PlaylistDownloader:
    def __init__(
        self,
        out_dir: Path,
        cookies: Path,
        backoff: BackoffConfig,
        *,
        verbose: bool = False,
        subfolders: Subfolders | None = None,
        patterns: NamePatterns = NamePatterns(),
        prune: bool = False,
        on_event: Callable[[Event], None] = lambda event: None,
        stop: threading.Event | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.cookies = cookies
        self.backoff = backoff
        self.verbose = verbose
        self.subfolders = subfolders
        self.patterns = patterns
        self.prune = prune
        self._pattern = patterns.playlist
        self.emit = on_event
        self.stop = stop or threading.Event()
        self._base_opts: dict = {}
        self._logger = _Logger(on_event, verbose)
        self._current_index = 0
        self._last_progress = 0.0

    def run(self, url: str, *, directory: Path | None = None) -> Summary:
        """Download every missing track of `url`.

        By default into OUT[/<subfolder>]/<playlist or album name>; `directory` pins the exact
        folder instead (resyncs keep using the first run's folder even if the playlist was
        renamed or the settings changed).
        """
        with tempfile.TemporaryDirectory(prefix="ltube-") as tmp:
            # yt-dlp writes refreshed cookies back to its cookie file; keep the user's file untouched.
            cookie_copy = Path(tmp) / "cookies.txt"
            shutil.copyfile(self.cookies, cookie_copy)
            self._base_opts = self._make_base_opts(cookie_copy)
            return self._run(url, directory)

    def _run(self, url: str, directory: Path | None) -> Summary:
        title, items, is_album = self._list_playlist(url)
        listing_warnings = list(self._logger.warnings)
        self._pattern = self.patterns.album if is_album else self.patterns.playlist
        if directory is not None:
            playlist_dir, title = directory, directory.name
        else:
            if is_album and items:
                title = self._album_name(title, items[0])
            root = self.out_dir
            if self.subfolders:
                root /= self.subfolders.albums if is_album else self.subfolders.playlists
            playlist_dir = root / sanitize_filename(title)
        playlist_dir.mkdir(parents=True, exist_ok=True)
        summary = Summary(title, playlist_dir, len(items))
        width = max(2, len(str(len(items))))
        self.emit(PlaylistStarted(title, playlist_dir, len(items)))
        # Tracks already on disk, by video ID from their tags: found without any request and
        # regardless of their number prefix, which shifts when the playlist changes.
        on_disk = self._rename_existing(items, scan_existing(playlist_dir), width)

        failures_in_row = 0
        previous: Status | None = None
        try:
            for idx, item in enumerate(items, 1):
                self._check_stop()
                self._current_index = idx
                result = None
                if known := on_disk.get(item.id):
                    result = TrackResult(Status.EXISTS, known.path, known.label, known.duration)
                else:
                    # Full pause after real downloads/failures, a short one after cheap lookups.
                    heavy = previous in (Status.DOWNLOADED, Status.FAILED)
                    self._sleep(self.backoff.track_pause() if heavy else self.backoff.request_sleep)

                self.emit(TrackStarted(idx, len(items), item.title))
                if result is None:
                    result = self._download_track(item.url, playlist_dir, f"{idx:0{width}d}")
                    previous = result.status
                summary.results.append(result)
                self.emit(TrackFinished(idx, result))

                failures_in_row = failures_in_row + 1 if result.status is Status.FAILED else 0
                if failures_in_row >= self.backoff.max_consecutive_failures:
                    summary.aborted = f"{failures_in_row} tracks failed in a row"
                    break
            if self.prune and summary.aborted is None:
                self._prune(items, on_disk, listing_warnings, summary)
        except RateLimitedError as e:
            summary.aborted = f"YouTube is rate-limiting this session: {e}"
        except Cancelled:
            summary.aborted = Cancelled.msg
        finally:
            # Albums need no playlist file: track numbers and album tags already order them.
            if not is_album:
                write_m3u8(
                    playlist_dir / f"{sanitize_filename(title)}.m3u8",
                    ((r.path, r.duration, r.label) for r in summary.results if r.path),
                )
        return summary

    def _list_playlist(self, url: str) -> tuple[str, list[_PlaylistItem], bool]:
        """Playlist title, its tracks, and whether it is a YouTube Music album."""
        self._logger.warnings.clear()  # _prune checks what this listing warned about
        opts = self._base_opts | {"extract_flat": "in_playlist", "noplaylist": False}
        with YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except DownloadError as e:
                if classify(str(e)) is ErrorKind.RATE_LIMITED:
                    raise RateLimitedError(_clean(e)) from e
                raise
        if not info or "entries" not in info:
            raise NotAPlaylistError(f"{url} is not a playlist")

        items = [
            _PlaylistItem(
                id=entry["id"],
                url=entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}",
                title=entry.get("title") or entry["id"],
            )
            for entry in info["entries"]
            if entry and entry.get("id")
        ]
        is_album = (info.get("id") or "").startswith(ALBUM_PLAYLIST_PREFIX)
        return info.get("title") or info.get("id") or "Untitled playlist", items, is_album

    def _rename_existing(self, items: list[_PlaylistItem], on_disk: dict, width: int) -> dict:
        """Rename tracks already on disk to what the current pattern and playlist positions
        would name them, from their own tags (no requests). Returns `on_disk` with new paths.

        Two phases (all to temporary names, then to final ones), so swaps like 01<->02 work.
        A name taken by a file ltube isn't moving is never overwritten.
        """
        plan: dict[str, tuple] = {}
        for idx, item in enumerate(items, 1):
            track = on_disk.get(item.id)
            if track is None or item.id in plan or not track.fields.get("title"):
                continue
            name = track_file_name(self._pattern, track.fields | {"number": f"{idx:0{width}d}"})
            new = track.path.with_name(name + track.path.suffix)
            if new != track.path:
                plan[item.id] = (track, new)

        moving = {track.path for track, _ in plan.values()}
        claimed: set[Path] = set()
        staged = []
        for vid, (track, new) in plan.items():
            taken = new.exists() and new not in moving and not new.samefile(track.path)
            if taken or new in claimed:
                self.emit(YtdlpWarning(f"kept {track.path.name}: {new.name} already exists"))
                continue
            claimed.add(new)
            tmp = track.path.with_name(f".ltube-rename-{vid}{track.path.suffix}")
            track.path.rename(tmp)
            staged.append((vid, track, tmp, new))

        renamed = dict(on_disk)
        for vid, track, tmp, new in staged:
            if new.exists():  # appeared since planning; never overwrite
                new = new.with_name(f"{new.stem} ({vid}){new.suffix}")
            tmp.rename(new)
            renamed[vid] = replace(track, path=new)
            self.emit(TrackRenamed(track.path, new))
        return renamed

    def _prune(
        self, items: list[_PlaylistItem], on_disk: dict, listing_warnings: list[str], summary: Summary
    ) -> None:
        """Delete files whose track left the playlist, if the listing can be trusted.

        Only files identified by the video ID in their tags are candidates, so anything
        ltube didn't download is never touched.
        """
        listed = {item.id for item in items}
        stale = [(vid, track) for vid, track in on_disk.items() if vid not in listed]
        if not stale:
            return
        if not items:
            self.emit(PruneSkipped("the playlist came back empty"))
            return
        if any(_HIDDEN_UNAVAILABLE_RE.search(w) for w in listing_warnings):
            self.emit(PruneSkipped("YouTube hid unavailable videos, so the track list may be incomplete"))
            return
        if len(stale) > 2 and len(stale) > len(on_disk) * _PRUNE_MAX_SHARE:
            self.emit(
                PruneSkipped(
                    f"{len(stale)} of {len(on_disk)} tracks would be deleted; "
                    "refusing to delete more than half at once, remove them by hand if this is intended"
                )
            )
            return
        for _, track in stale:
            self._check_stop()
            track.path.unlink(missing_ok=True)
            summary.removed.append(track.path)
            self.emit(TrackRemoved(track.path, track.label))

    def _album_name(self, playlist_title: str, first: _PlaylistItem) -> str:
        """Albums are named "Artist - Year - Album" from the first track's music metadata.

        The playlist itself carries neither artist nor year, and its uploader is a channel
        name that can differ from the artist, so this costs one extra metadata lookup.
        """
        info = self._track_info(first.url)
        album = info.get("album") or playlist_title.removeprefix(ALBUM_TITLE_PREFIX)
        artist = (info.get("artists") or [None])[0]
        return album_folder_name(artist, info.get("release_year"), album)

    def _track_info(self, url: str) -> dict:
        self._check_stop()
        with YoutubeDL(self._base_opts | {"format": "bestaudio"}) as ydl:
            ydl.add_post_processor(NormalizeMetadataPP(), when="pre_process")
            try:
                return ydl.extract_info(url, download=False)
            except DownloadError as e:
                if classify(str(e)) is ErrorKind.RATE_LIMITED:
                    raise RateLimitedError(_clean(e)) from e
                raise

    def _download_track(self, url: str, playlist_dir: Path, prefix: str) -> TrackResult:
        # TrackNamePP renders the user's pattern into ltube_name; yt-dlp sanitizes it.
        outtmpl = str(playlist_dir).replace("%", "%%") + os.sep + "%(ltube_name)s.%(ext)s"
        for attempt in range(self.backoff.retries + 1):
            try:
                return self._attempt(url, outtmpl, prefix)
            except DownloadError as e:
                message = _clean(e)
                kind = classify(message)
                if kind is ErrorKind.RATE_LIMITED:
                    raise RateLimitedError(message) from e
                if kind is ErrorKind.UNAVAILABLE:
                    return TrackResult(Status.UNAVAILABLE, error=message)
                if attempt == self.backoff.retries:
                    return TrackResult(Status.FAILED, error=message)
                delay = self.backoff.retry_delay(attempt)
                self.emit(TrackRetry(self._current_index, attempt + 1, delay, message))
                self._sleep(delay)
        raise AssertionError("unreachable")

    def _sleep(self, seconds: float) -> None:
        """Pause, waking up early (and raising) if the run is cancelled."""
        if seconds <= 0:
            self._check_stop()
            return
        self.emit(Waiting(seconds))
        if self.stop.wait(seconds):
            raise Cancelled()

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Cancelled()

    def _on_progress(self, status: dict) -> None:
        self._check_stop()
        if status.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - self._last_progress < _PROGRESS_INTERVAL:
            return
        self._last_progress = now
        self.emit(
            TrackProgress(
                self._current_index,
                status.get("downloaded_bytes") or 0,
                status.get("total_bytes") or status.get("total_bytes_estimate"),
                status.get("speed"),
            )
        )

    def _attempt(self, url: str, outtmpl: str, prefix: str) -> TrackResult:
        self._check_stop()
        with YoutubeDL(self._base_opts | self._track_opts(outtmpl)) as ydl:
            ydl.add_post_processor(NormalizeMetadataPP(), when="pre_process")
            ydl.add_post_processor(TrackNamePP(self._pattern, prefix), when="pre_process")
            ydl.add_progress_hook(self._on_progress)
            info = ydl.extract_info(url, download=False)
            label = f"{info.get('artist') or 'Unknown Artist'} - {info.get('track') or info.get('title')}"
            target = Path(ydl.prepare_filename(info))

            if existing := _find_existing(target):
                return TrackResult(Status.EXISTS, existing, label, info.get("duration"))

            ydl.process_ie_result(info, download=True)
            path = _find_existing(target)
            if path is None:
                raise DownloadError(f"download finished but no audio file found next to {target}")
            return TrackResult(Status.DOWNLOADED, path, label, info.get("duration"))

    def _make_base_opts(self, cookiefile: Path) -> dict:
        opts = {
            # ffmpeg ships inside the imageio-ffmpeg wheel (override with IMAGEIO_FFMPEG_EXE).
            # Deno needs no option: yt-dlp finds the `deno` package's binary in the venv.
            "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
            "cookiefile": str(cookiefile),
            "sleep_interval_requests": self.backoff.request_sleep,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "retry_sleep_functions": {
                "http": inner_retry_sleep,
                "fragment": inner_retry_sleep,
                "extractor": inner_retry_sleep,
            },
            "ratelimit": self.backoff.rate_limit,
        }
        if not self.verbose:
            opts |= {"quiet": True, "noprogress": True}
        return opts | {"logger": self._logger}

    @staticmethod
    def _track_opts(outtmpl: str) -> dict:
        return {
            "format": "bestaudio",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                {"key": "FFmpegVideoRemuxer", "preferedformat": "webm>opus"},
                {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False},
                {"key": "EmbedThumbnail"},
            ],
            "postprocessor_args": {"thumbnailsconvertor+ffmpeg_o": COVER_FFMPEG_ARGS},
        }


def _find_existing(target: Path) -> Path | None:
    """The final file for `target`, whatever audio container it ended up in."""
    for ext in AUDIO_EXTS:
        candidate = target.with_suffix(f".{ext}")
        if candidate.exists():
            return candidate
    return None


def _clean(error: Exception) -> str:
    return str(error).removeprefix("ERROR: ").strip()
