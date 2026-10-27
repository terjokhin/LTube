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
from yt_dlp.utils import DownloadCancelled, DownloadError

from ltube import ytmusic
from ltube.backoff import BackoffConfig, inner_retry_sleep
from ltube.library import Library, Track
from ltube.metadata import (
    ALBUM_PLAYLIST_PREFIX,
    ALBUM_TITLE_PREFIX,
    DEFAULT_TRACK_PATTERN,
    CoverArtPP,
    LibraryNamePP,
    NormalizeMetadataPP,
    folder_name,
    library_location,
    retag_album,
    write_m3u8,
)

# Final extensions we produce: AAC stays in m4a, Opus is moved from webm into an .opus
# (Ogg) container. Both are stream copies, the audio is never re-encoded.
AUDIO_EXTS = ("m4a", "opus")
# Playlists are m3u files here, in the library root; the tracks themselves live in Artist/Album.
PLAYLISTS_DIR = "playlists"

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
    library_dir: Path
    total: int
    results: list[TrackResult] = field(default_factory=list)
    aborted: str | None = None

    def count(self, status: Status) -> int:
        return sum(r.status is status for r in self.results)


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
class TrackMoved:
    """A track downloaded on its own turned up on an album and was filed there (no download)."""

    old: Path
    new: Path


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
    | TrackMoved
    | Waiting
    | YtdlpWarning
)

# yt-dlp calls progress hooks many times a second; forward at most this often.
_PROGRESS_INTERVAL = 0.5

# YouTube hides unavailable videos from playlists. yt-dlp asks for them, but if that
# request fails it carries on silently with the shorter list, so a listing with this
# warning may be missing tracks that are still in the playlist (just unplayable).
_HIDDEN_UNAVAILABLE_RE = re.compile(r"unavailable videos? (?:is|are|will be) hidden", re.IGNORECASE)
# A listing that drops more than this share of a playlist's tracks at once is not believed.
_SHRINK_MAX_SHARE = 0.5


@dataclass(frozen=True)
class _PlaylistItem:
    id: str
    url: str
    title: str


@dataclass(frozen=True)
class _Listing:
    id: str
    title: str
    items: list[_PlaylistItem]
    is_album: bool  # a YouTube Music album


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
        pattern: str = DEFAULT_TRACK_PATTERN,
        on_event: Callable[[Event], None] = lambda event: None,
        stop: threading.Event | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.cookies = cookies
        self.backoff = backoff
        self.verbose = verbose
        self.pattern = pattern
        self.emit = on_event
        self.stop = stop or threading.Event()
        self._base_opts: dict = {}
        self._logger = _Logger(on_event, verbose)
        self._current_index = 0
        self._current_id: str | None = None
        self._cookie_copy: Path | None = None
        self._library: Library | None = None
        self._album_info: dict | None = None  # looked up once per album run, {} if that failed
        # Artist/album from YouTube Music for tracks the website hides (uploads), by video ID.
        self._music: dict[str, ytmusic.MusicTrack] = {}
        self._last_progress = 0.0

    def run(self, url: str) -> Summary:
        """Download every track of `url` that the library doesn't have yet.

        Tracks go to OUT/Album Artist/Album/, whichever playlist or album they came from, and
        a playlist also gets OUT/playlists/<title>.m3u8. The library is held for the whole
        run, so a second run (another job, the CLI) waits and then finds these tracks.
        """
        with tempfile.TemporaryDirectory(prefix="ltube-") as tmp:
            # yt-dlp writes refreshed cookies back to its cookie file; keep the user's file untouched.
            cookie_copy = Path(tmp) / "cookies.txt"
            shutil.copyfile(self.cookies, cookie_copy)
            self._cookie_copy = cookie_copy
            self._base_opts = self._make_base_opts(cookie_copy)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._library = Library(self.out_dir)
            with self._library.lock():
                # Leftovers of a run that was killed; nobody else uses the staging folder now.
                shutil.rmtree(self._library.tmp_dir, ignore_errors=True)
                return self._run(url)

    def _run(self, url: str) -> Summary:
        library = self._library
        assert library is not None
        listing = self._list_playlist(url)
        title, items, is_album = listing.title, listing.items, listing.is_album
        listing_warnings = list(self._logger.warnings)
        self._album_info = None
        summary = Summary(title, self.out_dir, len(items))
        self.emit(PlaylistStarted(title, self.out_dir, len(items)))
        # The order is remembered at once, so even a cancelled or failed run leaves a playlist
        # file with everything downloaded so far, in the right order.
        playlist = self._record_playlist(listing, listing_warnings) if items and not is_album else None

        failures_in_row = 0
        previous: Status | None = None
        try:
            if not items:
                hidden = any(_HIDDEN_UNAVAILABLE_RE.search(w) for w in listing_warnings)
                summary.aborted = "YouTube listed no tracks for this playlist" + (
                    "; it hid all of them as unavailable (private videos in a private playlist can't be listed)"
                    if hidden
                    else ""
                )
            for idx, item in enumerate(items, 1):
                self._check_stop()
                self._current_index = idx
                self._current_id = item.id
                result = None
                # Already in the library (from any playlist or album): no request and no pause.
                if known := library.get_track(item.id):
                    if is_album and known.album is None:
                        known = self._file_under_album(known, listing, idx) or known
                    label = f"{known.artist or 'Unknown Artist'} - {known.title or known.path.stem}"
                    result = TrackResult(Status.EXISTS, known.path, label, known.duration_s)
                else:
                    # Full pause after real downloads/failures, a short one after cheap lookups.
                    heavy = previous in (Status.DOWNLOADED, Status.FAILED)
                    self._sleep(self.backoff.track_pause() if heavy else self.backoff.request_sleep)

                self.emit(TrackStarted(idx, len(items), item.title))
                if result is None:
                    # An album's tracks are numbered by their place on it.
                    result = self._download_track(item.url, idx if is_album else None)
                    previous = result.status
                summary.results.append(result)
                self.emit(TrackFinished(idx, result))

                failures_in_row = failures_in_row + 1 if result.status is Status.FAILED else 0
                if failures_in_row >= self.backoff.max_consecutive_failures:
                    summary.aborted = f"{failures_in_row} tracks failed in a row"
                    break
        except RateLimitedError as e:
            summary.aborted = f"YouTube is rate-limiting this session: {e}"
        except Cancelled:
            summary.aborted = Cancelled.msg
        finally:
            # Albums need no playlist file: track numbers and album tags already order them.
            if playlist:
                self._write_playlist_file(listing.id, *playlist)
        return summary

    def _record_playlist(self, listing: _Listing, warnings: list[str]) -> tuple[str, str | None]:
        """Remember the playlist's tracks and their order; returns its m3u file name and the
        one it had before (to be removed if the playlist was renamed).

        A listing that may be incomplete only adds to what is remembered: a track missing from
        it may merely be unplayable (hidden by YouTube), not removed from the playlist.
        """
        library = self._library
        assert library is not None
        ids = [item.id for item in listing.items]
        previous = library.get_playlist(listing.id)
        m3u_name = self._m3u_name(listing.id, listing.title)
        old = library.playlist_items(listing.id)
        gone = [video_id for video_id in dict.fromkeys(old) if video_id not in set(ids)]
        if gone and (reason := _untrusted_listing(warnings, old, gone)):
            library.merge_playlist_items(listing.id, listing.title, ids, m3u_name=m3u_name)
            self.emit(YtdlpWarning(f"kept {len(gone)} tracks that are no longer listed: {reason}"))
        else:
            library.set_playlist_items(listing.id, listing.title, ids, m3u_name=m3u_name)
        return m3u_name, previous.m3u_name if previous else None

    def _m3u_name(self, playlist_id: str, title: str) -> str:
        assert self._library is not None
        base = folder_name(title, "Playlist")
        owner = self._library.playlist_with_m3u_name(f"{base}.m3u8")
        # Two playlists with one title must not share a file.
        return f"{base}.m3u8" if owner is None or owner.playlist_id == playlist_id else f"{base} ({playlist_id}).m3u8"

    def _write_playlist_file(self, playlist_id: str, m3u_name: str, old_name: str | None) -> None:
        """Write playlists/<title>.m3u8 from the index: every downloaded track of the playlist,
        whichever run fetched it.
        """
        assert self._library is not None
        folder = self.out_dir / PLAYLISTS_DIR
        tracks = self._library.playlist_tracks(playlist_id)
        write_m3u8(
            folder / m3u_name,
            ((t.path, t.duration_s, f"{t.artist or 'Unknown Artist'} - {t.title or t.path.stem}") for t in tracks),
        )
        if old_name and old_name != m3u_name:
            (folder / old_name).unlink(missing_ok=True)

    def _file_under_album(self, known: Track, listing: _Listing, position: int) -> Track | None:
        """A track that came in on its own (so it sits in Singles) is on this album: tag it
        and move it into the album's folder, with no download. None if that isn't possible.
        """
        assert self._library is not None
        album = self._album_of(listing)
        if not album:
            return None
        info = {
            "id": known.video_id,
            "title": known.title,
            "artist": known.artist,
            "artists": [known.artist] if known.artist else [],
            "album": album["album"],
            "album_artists": [album["artist"]] if album["artist"] else [],
        }
        folder, name = library_location(self.pattern, info, position)
        new = self.out_dir / folder / f"{name}{known.path.suffix}"
        if new.exists():
            self.emit(YtdlpWarning(f"left {known.path.name} in {known.path.parent.name}: {new.name} already exists"))
            return None
        try:
            retag_album(
                known.path,
                album=album["album"],
                album_artist=album["artist"],
                track_number=position,
                track_total=len(listing.items),
                year=album["year"],
            )
        except ValueError as e:
            self.emit(YtdlpWarning(f"left {known.path.name} where it is: {e}"))
            return None
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(known.path, new)
        self._library.move_track(
            known.video_id, new, album=album["album"], album_artist=album["artist"], track_number=position
        )
        _remove_empty_dirs(known.path.parent, self.out_dir)
        self.emit(TrackMoved(known.path, new))
        return self._library.get_track(known.video_id)

    def _album_of(self, listing: _Listing) -> dict | None:
        """Album, album artist and year, from the first track's metadata: the playlist itself
        carries neither artist nor year. One request per album run, and only when needed.
        """
        if self._album_info is None:
            self._album_info = {}
            try:
                info = self._track_info(listing.items[0].url)
            except DownloadError as e:
                self.emit(YtdlpWarning(f"couldn't look up the album, so tracks stay where they are: {_clean(e)}"))
            else:
                self._album_info = {
                    "album": info.get("album") or listing.title.removeprefix(ALBUM_TITLE_PREFIX),
                    "artist": (info.get("artists") or [None])[0],
                    "year": info.get("release_year"),
                }
        return self._album_info or None

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

    def _list_playlist(self, url: str) -> _Listing:
        """The playlist's ID and title, its tracks, and whether it is a YouTube Music album."""
        self._logger.warnings.clear()  # _run checks what this listing warned about
        info = self._extract_listing(url, signed_in=True)
        if not info or "entries" not in info:
            raise NotAPlaylistError(f"{url} is not a playlist")
        items = _playlist_items(info)
        expected = info.get("playlist_count") or 0
        if len(items) < expected and any(_HIDDEN_UNAVAILABLE_RE.search(w) for w in self._logger.warnings):
            items = self._recover_hidden(url, info.get("id") or "", items, expected)
        is_album = (info.get("id") or "").startswith(ALBUM_PLAYLIST_PREFIX)
        title = info.get("title") or info.get("id") or "Untitled playlist"
        return _Listing(info.get("id") or title, title, items, is_album)

    def _extract_listing(self, url: str, *, signed_in: bool) -> dict:
        opts = self._base_opts | {"extract_flat": "in_playlist", "noplaylist": False}
        if not signed_in:
            opts = {k: v for k, v in opts.items() if k != "cookiefile"}
        with YoutubeDL(opts) as ydl:
            try:
                return ydl.extract_info(url, download=False)
            except DownloadError as e:
                if classify(str(e)) is ErrorKind.RATE_LIMITED:
                    raise RateLimitedError(_clean(e)) from e
                raise

    def _recover_hidden(
        self, url: str, playlist_id: str, items: list[_PlaylistItem], expected: int
    ) -> list[_PlaylistItem]:
        """Find the tracks YouTube hid from the signed-in listing.

        A signed-in listing leaves out private videos, such as songs uploaded to YouTube Music,
        even when asked to show unavailable videos. The tracks themselves download normally
        with the cookies. First ask YouTube Music, which lists them with artist and album
        (remembered for tagging). Failing that, a signed-out listing of the same (public or
        unlisted) playlist still includes them, untitled. A private playlist can't be listed
        signed out; keep what we have.
        """
        music = ytmusic.music_playlist(self._cookie_copy, playlist_id) if self._cookie_copy else None
        if music and len(music) > len(items):
            # Track numbers and years only exist on each album's page: one request per album.
            music = ytmusic.with_album_details(
                self._cookie_copy, music, pause=lambda: self._sleep(self.backoff.request_sleep)
            )
            self._music = {track.video_id: track for track in music}
            merged = [
                _PlaylistItem(t.video_id, f"https://music.youtube.com/watch?v={t.video_id}", t.title) for t in music
            ]
            source = "YouTube Music"
        else:
            try:
                recovered = _playlist_items(self._extract_listing(url, signed_in=False))
            except DownloadError:
                return items
            if len(recovered) <= len(items):
                return items
            titles = {item.id: item.title for item in items}
            merged = [replace(item, title=titles.get(item.id, item.title)) for item in recovered]
            source = "a signed-out listing"
        self.emit(YtdlpWarning(f"YouTube hid {len(merged) - len(items)} tracks when signed in; found them in {source}"))
        if len(merged) >= expected:
            # The listing is complete after all, so the hidden-videos warning no longer means a
            # partial track list and mustn't block pruning.
            self._logger.warnings = [w for w in self._logger.warnings if not _HIDDEN_UNAVAILABLE_RE.search(w)]
        return merged

    def _download_track(self, url: str, number: int | None) -> TrackResult:
        for attempt in range(self.backoff.retries + 1):
            try:
                return self._attempt(url, number)
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

    def _attempt(self, url: str, number: int | None) -> TrackResult:
        library = self._library
        assert library is not None and self._current_id is not None
        self._check_stop()
        # The track is built in its own staging folder and moved into the library only when it
        # is complete and tagged, so nothing half-done is ever in the library (or taken for a
        # finished track by the index), whatever way the attempt ends.
        staging = library.tmp_dir / self._current_id
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        outtmpl = str(staging).replace("%", "%%") + os.sep + "%(ltube_name)s.%(ext)s"
        try:
            with YoutubeDL(self._base_opts | self._track_opts(outtmpl)) as ydl:
                ydl.add_post_processor(NormalizeMetadataPP(self._music.get(self._current_id)), when="pre_process")
                ydl.add_post_processor(LibraryNamePP(self.pattern, number), when="pre_process")
                ydl.add_post_processor(CoverArtPP(ydl), when="before_dl")
                ydl.add_progress_hook(self._on_progress)
                info = ydl.extract_info(url, download=False)
                label = f"{info.get('artist') or 'Unknown Artist'} - {info.get('track') or info.get('title')}"
                folder = self.out_dir / info["ltube_dir"]

                # The file is there but the index doesn't know it (e.g. the index was lost).
                if existing := _find_existing(folder / info["ltube_name"]):
                    library.add_track(info["id"], existing, **_index_tags(info))
                    return TrackResult(Status.EXISTS, existing, label, info.get("duration"))

                ydl.process_ie_result(info, download=True)
            staged = _find_staged(staging)
            if staged is None:
                raise DownloadError(f"download finished but no audio file found in {staging}")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / staged.name
            os.replace(staged, path)
            library.add_track(info["id"], path, **_index_tags(info))
            return TrackResult(Status.DOWNLOADED, path, label, info.get("duration"))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

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
            # The cover is made square by CoverArtPP (added in _attempt), before EmbedThumbnail.
            "postprocessors": [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "webm>opus"},
                {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False},
                {"key": "EmbedThumbnail"},
            ],
        }


def _playlist_items(info: dict) -> list[_PlaylistItem]:
    return [
        _PlaylistItem(
            id=entry["id"],
            url=entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}",
            title=entry.get("title") or entry["id"],
        )
        for entry in info.get("entries") or []
        if entry and entry.get("id")
    ]


def _untrusted_listing(warnings: list[str], old: list[str], gone: list[str]) -> str | None:
    """Why a listing that lacks `gone` (tracks the playlist held before) can't be believed, if so."""
    if any(_HIDDEN_UNAVAILABLE_RE.search(w) for w in warnings):
        return "YouTube hid unavailable videos, so the track list may be incomplete"
    if len(gone) > 2 and len(gone) > len(old) * _SHRINK_MAX_SHARE:
        return f"{len(gone)} of {len(old)} tracks vanished at once"
    return None


def _remove_empty_dirs(folder: Path, root: Path) -> None:
    """Remove `folder` and then its parents, up to but not including `root`, while they are empty."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _find_existing(base: Path) -> Path | None:
    """The final file for `base` (a path without extension), whatever audio container it is in."""
    for ext in AUDIO_EXTS:
        candidate = base.with_name(f"{base.name}.{ext}")
        if candidate.exists():
            return candidate
    return None


def _find_staged(staging: Path) -> Path | None:
    return next((p for ext in AUDIO_EXTS for p in sorted(staging.glob(f"*.{ext}"))), None)


def _index_tags(info: dict) -> dict:
    album_artists = info.get("album_artists") or []
    return {
        "artist": info.get("artist"),
        "title": info.get("track") or info.get("title"),
        "album": info.get("album"),
        "album_artist": album_artists[0] if album_artists else None,
        "track_number": info.get("track_number"),
        "duration_s": info.get("duration"),
    }


def _clean(error: Exception) -> str:
    return str(error).removeprefix("ERROR: ").strip()
