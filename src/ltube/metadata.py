"""Tag normalization and playlist (.m3u8) output."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import mutagen
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
# YouTube Music albums are playlists with this ID prefix, titled "Album - <name>".
ALBUM_PLAYLIST_PREFIX = "OLAK5uy_"
ALBUM_TITLE_PREFIX = "Album - "


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


# Track file names come from a user pattern (Settings), e.g. "{number} - {artist} - {title}".
NAME_FIELDS = ("number", "artist", "title", "album", "year")
DEFAULT_NAME_PATTERN = "{number} - {artist} - {title}"
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


def track_file_name(pattern: str, values: dict[str, str | None]) -> str:
    """The file name (without extension) for a track: rendered, sanitized like yt-dlp does, and
    trimmed to a safe length. Downloads and resync renames must both use this.
    """
    name = sanitize_filename(render_name(pattern, values))
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
    }


class TrackNamePP(PostProcessor):
    """Put the pattern-rendered file name in info["ltube_name"] for the output template.

    Registered after NormalizeMetadataPP, so the artist is already filled in.
    """

    def __init__(self, pattern: str, number: str) -> None:
        super().__init__()
        self._pattern = pattern
        self._number = number

    def run(self, info):
        info["ltube_name"] = track_file_name(self._pattern, track_fields(info, self._number))
        return [], info


def album_folder_name(artist: str | None, year: int | None, album: str) -> str:
    """ "Artist - Year - Album"; the year is left out when YouTube doesn't know it."""
    parts = [artist or "Unknown Artist", str(year) if year else None, album]
    return " - ".join(p for p in parts if p)


# FFmpegMetadata stores the video URL in `purl`/`comment` (`©cmt` in m4a).
_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/)([0-9A-Za-z_-]{11})")
_SKIP_TAGS = {"metadata_block_picture", "covr"}  # embedded cover art, large and irrelevant


@dataclass(frozen=True)
class ExistingTrack:
    path: Path
    label: str
    duration: float | None
    # Name fields from the tags (track_fields minus number), for renaming to a new pattern.
    fields: dict[str, str | None] = field(default_factory=dict)


def scan_existing(directory: Path, exts: Iterable[str] = ("m4a", "opus")) -> dict[str, ExistingTrack]:
    """Index the audio files in `directory` by the YouTube video ID found in their tags."""
    found: dict[str, ExistingTrack] = {}
    for ext in exts:
        for path in sorted(directory.glob(f"*.{ext}")):
            try:
                audio = mutagen.File(path)
            except mutagen.MutagenError:
                continue
            if audio is None or not audio.tags:
                continue
            values = {k: [v.decode(errors="replace") if isinstance(v, bytes) else str(v) for v in vs]
                      for k, vs in audio.tags.items() if k not in _SKIP_TAGS}  # fmt: skip
            video_id = next((m[1] for vs in values.values() for v in vs if (m := _VIDEO_ID_RE.search(v))), None)
            if video_id is None:
                continue
            artist = _first(values, "artist", "©ART")
            title = _first(values, "title", "©nam")
            date = _first(values, "date", "©day") or ""
            fields = {
                "artist": artist or "Unknown Artist",
                "title": title,
                "album": _first(values, "album", "©alb"),
                # A bare year is the release year ltube tagged; a full date is YouTube's upload
                # date (older downloads), which isn't the year a fresh download would name.
                "year": date if re.fullmatch(r"\d{4}", date) else None,
            }
            label = f"{fields['artist']} - {title or path.stem}"
            found.setdefault(video_id, ExistingTrack(path, label, audio.info.length or None, fields))
    return found


def _first(tags: dict[str, list[str]], *keys: str) -> str | None:
    return next((tags[k][0] for k in keys if tags.get(k)), None)


def write_m3u8(path: Path, tracks: Iterable[tuple[Path, float | None, str]]) -> None:
    """Write an extended M3U playlist; tracks are (file, duration in seconds, label)."""
    lines = ["#EXTM3U"]
    for file, duration, label in tracks:
        lines.append(f"#EXTINF:{round(duration) if duration else -1},{label}")
        lines.append(os.path.relpath(file, path.parent))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
