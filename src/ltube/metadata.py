"""Tag normalization and playlist (.m3u8) output."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import mutagen
import mutagen.mp4
from yt_dlp.postprocessor import FFmpegPostProcessor, PostProcessor
from yt_dlp.utils import PostProcessingError, replace_extension, sanitize_filename

if TYPE_CHECKING:
    from ltube.ytmusic import MusicTrack

TOPIC_SUFFIX = " - Topic"
# YouTube Music stores songs people upload as private videos on this system channel; it's no artist.
UPLOADS_CHANNEL = "Music Library Uploads"

# YouTube thumbnails are 16:9; center-crop to a square cover and keep JPEG quality high.
COVER_FFMPEG_ARGS = [
    "-c:v", "mjpeg", "-qmin", "1", "-qscale:v", "1",
    "-vf", "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'",
    "-update", "1",
]  # fmt: skip
# YouTube Music albums are playlists with this ID prefix.
ALBUM_PLAYLIST_PREFIX = "OLAK5uy_"
ALBUM_TITLE_PREFIX = "Album - "  # YouTube Music titles an album's playlist "Album - <name>"


def clean_artist(name: str) -> str:
    """YouTube's auto-generated music channels are named "<Artist> - Topic"."""
    return name.removesuffix(TOPIC_SUFFIX).strip()


class NormalizeMetadataPP(PostProcessor):
    """Tidy what YouTube returns before the file name, tags and cover are chosen.

    Makes sure every track has an artist, even uploads YouTube doesn't tag as music, and
    narrows the thumbnails to ones that can be fetched. Runs right after extraction, so the
    results reach the filename, the tags written later by FFmpegMetadata, and the cover.

    `music` is YouTube Music's own data for the track (see ltube.ytmusic), when known: for
    uploads it's the only source of the artist and album.
    """

    def __init__(self, music: MusicTrack | None = None) -> None:
        super().__init__()
        self._music = music

    def run(self, info):
        artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
        if UPLOADS_CHANNEL in artists:
            artists = []  # yt-dlp falls back to the uploader, which for uploads is no artist
        if music := self._music:
            artists = artists or music.artists
            if not info.get("album") and music.album:
                info["album"] = music.album
            if not info.get("release_year") and music.year:
                info["release_year"] = music.year
            if music.album_artists and not info.get("album_artists"):
                info["album_artists"] = music.album_artists
            if music.track_number and not info.get("track_number"):
                info["track_number"] = music.track_number
                # "3/10": players need the total to show and sort the album properly.
                info["meta_track"] = f"{music.track_number}/{music.track_count}" if music.track_count else str(music.track_number)
        if not artists:
            uploader = info.get("uploader") or info.get("channel")
            if uploader and uploader != UPLOADS_CHANNEL:
                artists = [clean_artist(uploader)]
        if artists:
            # YouTube sometimes lists the same artist twice; keep the first occurrence.
            artists = list(dict.fromkeys(artists))
            info["artists"] = artists
            info["artist"] = ", ".join(artists)
        else:
            info.pop("artists", None)
            info.pop("artist", None)
            # FFmpegMetadata would fall back to the uploader for the tag; an empty override
            # leaves the artist untagged instead, matching the "Unknown Artist" file name.
            info["meta_artist"] = ""
        info["thumbnails"] = usable_thumbnails(info)
        # FFmpegMetadata tags `date` with YouTube's upload date; prefer the release year, which
        # players should show and which scan_existing reads back for {year} when renaming.
        if year := info.get("release_year"):
            info["meta_date"] = str(year)
        return [], info


# Track file names come from a user pattern (Settings), see DEFAULT_TRACK_PATTERN.
NAME_FIELDS = ("number", "artist", "title", "album", "year", "id")
# The library layout (OUT/Album Artist/Album/<this>.ext): the video ID in the name lets the
# index be rebuilt from the files alone.
DEFAULT_TRACK_PATTERN = "{number} - {title} [{id}]"
SINGLES_FOLDER = "Singles"
_FIELD_RE = re.compile(r"(\{\w+\})")
# Most filesystems allow 255 bytes per name; leave room for the extension and yt-dlp's
# temporary suffixes (".part", ".temp").
MAX_NAME_BYTES = 230


def usable_thumbnails(info: dict) -> list[dict]:
    """The thumbnails worth trying, in yt-dlp's order (it tries the last one first).

    For private videos, such as songs uploaded to YouTube Music, only signed URLs work. yt-dlp
    would otherwise try ~40 that fail, one request each. Of those, prefer the upload's own
    square cover art ("vi_locker"), which yt-dlp ranks last.
    """
    thumbnails = info.get("thumbnails") or []
    signed = [t for t in thumbnails if "sqp=" in (t.get("url") or "")]
    if info.get("availability") != "private" or not signed:
        return thumbnails
    covers = [t for t in signed if "/vi_locker/" in t["url"]]
    return sorted(covers or signed, key=lambda t: t.get("width") or 0)


class CoverArtPP(FFmpegPostProcessor):
    """Turn the downloaded thumbnail into a square JPEG for EmbedThumbnail (runs before_dl).

    Replaces FFmpegThumbnailsConvertor, which makes ffmpeg decode by file extension and so
    fails on the mislabelled images YouTube serves for uploaded music (JPEG data named .png).
    Here ffmpeg detects the format from the content. A cover that still can't be converted is
    dropped with a warning rather than failing the track.
    """

    def run(self, info):
        for thumbnail in info.get("thumbnails") or []:
            source = thumbnail.get("filepath")
            if not source or not os.path.exists(source):
                continue
            target = replace_extension(source, "jpg")
            tmp = replace_extension(source, "cover.jpg")
            try:
                self.real_run_ffmpeg([(source, [])], [(tmp, COVER_FFMPEG_ARGS)])
            except PostProcessingError as e:
                self.report_warning(f"Skipping the cover, it couldn't be converted: {e}")
                for path in (source, tmp):
                    if os.path.exists(path):
                        os.remove(path)
                thumbnail.pop("filepath")
                continue
            os.replace(tmp, target)
            if source != target:
                os.remove(source)
            thumbnail["filepath"] = target
        return [], info


def validate_name_pattern(pattern: str) -> str:
    pattern = pattern.strip()
    if "{title}" not in pattern:
        raise ValueError("the pattern must contain {title}")
    if unknown := sorted({f[1:-1] for f in _FIELD_RE.findall(pattern)} - set(NAME_FIELDS)):
        names = ", ".join("{" + f + "}" for f in NAME_FIELDS)
        raise ValueError(f"unknown placeholder {{{unknown[0]}}}; use {names}")
    if "/" in pattern or "\\" in pattern:
        raise ValueError("a file name pattern can't contain slashes")
    return pattern


def render_name(pattern: str, values: dict[str, str | None]) -> str:
    """Fill `pattern`; an empty field disappears together with its separator.

    "{number} - {year} - {title}" without a year gives "01 - Title", and
    "{title} ({year})" gives "Title" rather than "Title ()".
    """
    tokens = _FIELD_RE.split(pattern)  # literal, field, literal, ..., literal
    parts = [tokens[0]]
    have_value = False
    for field, after in zip(tokens[1::2], tokens[2::2]):
        if value := values.get(field[1:-1]):
            parts += [value, after]
            have_value = True
        elif have_value:
            before = parts[-1].rstrip()
            if before[-1:] in ("(", "[") and after.lstrip()[:1] in (")", "]"):
                parts[-1] = after.lstrip()[1:]  # drop the whole "(...)"
            else:
                parts[-1] = after  # drop the separator before the empty field
    return "".join(parts).strip().strip("-_ ")


def validate_track_pattern(pattern: str) -> str:
    pattern = validate_name_pattern(pattern)
    if "{id}" not in pattern:
        raise ValueError("the pattern must contain {id}, which is how ltube recognises a downloaded track")
    return pattern


def _trim_bytes(name: str) -> str:
    encoded = name.encode()
    if len(encoded) > MAX_NAME_BYTES:
        name = encoded[:MAX_NAME_BYTES].decode(errors="ignore").rstrip(" .")
    return name


def track_fields(info: dict, number: str) -> dict[str, str | None]:
    year = info.get("release_year")
    return {
        "number": number,
        "artist": info.get("artist") or "Unknown Artist",
        "title": info.get("track") or info.get("title") or info.get("id"),
        "album": info.get("album"),
        "year": str(year) if year else None,
        "id": info.get("id"),
    }


def folder_name(text: str | None, fallback: str) -> str:
    """One folder level (or playlist file name): sanitized and length-limited. Leading dots go, since ".." would climb
    out of the library and ".x" would hide the folder from media servers.
    """
    name = _trim_bytes(sanitize_filename(text or "")).strip().lstrip(".").strip()
    return name or fallback


def _fit_name(pattern: str, values: dict[str, str | None]) -> str:
    """Like track_file_name, but a name that is too long loses the end of the title, not the
    end of the name, so the [id] survives.
    """
    title = values["title"] or ""
    while True:
        name = sanitize_filename(render_name(pattern, values | {"title": title}))
        excess = len(name.encode()) - MAX_NAME_BYTES
        if excess <= 0 or not title:
            return _trim_bytes(name)
        title = title.encode()[:-excess].decode(errors="ignore").rstrip()


def library_location(pattern: str, info: dict, number: int | None = None) -> tuple[Path, str]:
    """Where a track goes in the library: (folder relative to its root, file name without extension).

    The folder is `Album Artist/Album`, or `Artist/Singles` for a track without an album.
    `number` is the track's position on its album when the caller knows it (an album download);
    otherwise the track's own number from YouTube, and 00 if it has none.
    """
    album_artist = (
        (info.get("album_artists") or [None])[0]
        or info.get("album_artist")
        or (info.get("artists") or [None])[0]
        or "Unknown Artist"
    )
    folder = Path(folder_name(album_artist, "Unknown Artist"), folder_name(info.get("album"), SINGLES_FOLDER))
    return folder, _fit_name(pattern, track_fields(info, f"{number or info.get('track_number') or 0:02d}"))


class LibraryNamePP(PostProcessor):
    """Put the track's library folder and file name in info["ltube_dir"] and info["ltube_name"].

    Registered after NormalizeMetadataPP, so the artist is already filled in. The folder is kept
    apart from the name because yt-dlp would sanitize a "/" inside a single field value.

    `number` is the track's position on its album, when the caller knows it. It also becomes the
    track number tag, which is what media servers order an album by.
    """

    def __init__(self, pattern: str, number: int | None = None) -> None:
        super().__init__()
        self._pattern = pattern
        self._number = number

    def run(self, info):
        if self._number:
            info["track_number"] = self._number
        folder, name = library_location(self._pattern, info, self._number)
        info["ltube_dir"], info["ltube_name"] = folder.as_posix(), name
        return [], info


def retag_album(
    path: Path, *, album: str, album_artist: str | None, track_number: int, track_total: int | None, year: int | None
) -> None:
    """Put a track on an album in place: album, album artist, track number (with the album's
    track count) and release year. Other tags and the embedded cover are kept.
    """
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError as e:
        raise ValueError(f"can't read the tags of {path.name}: {e}") from e
    if audio is None:
        raise ValueError(f"{path.name} is not an audio file mutagen can tag")
    if isinstance(audio, mutagen.mp4.MP4):
        audio["\xa9alb"] = [album]
        if album_artist:
            audio["aART"] = [album_artist]
        audio["trkn"] = [(track_number, track_total or 0)]
        if year:
            audio["\xa9day"] = [str(year)]
    else:  # Ogg Opus: Vorbis comments
        audio["album"] = [album]
        if album_artist:
            audio["albumartist"] = [album_artist]
        audio["tracknumber"] = [str(track_number)]
        if track_total:
            audio["tracktotal"] = [str(track_total)]
        if year:
            audio["date"] = [str(year)]
    try:
        audio.save()
    except mutagen.MutagenError as e:
        raise ValueError(f"can't write the tags of {path.name}: {e}") from e


def write_m3u8(path: Path, tracks: Iterable[tuple[Path, float | None, str]]) -> None:
    """Write an extended M3U playlist atomically; tracks are (file, duration in seconds, label).

    A media server rescanning meanwhile sees the old file or the new one, never half of it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for file, duration, label in tracks:
        lines.append(f"#EXTINF:{round(duration) if duration else -1},{label}")
        lines.append(os.path.relpath(file, path.parent))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
