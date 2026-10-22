"""Tag normalization and playlist (.m3u8) output."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from yt_dlp.postprocessor import PostProcessor
from yt_dlp.utils import sanitize_filename

TOPIC_SUFFIX = " - Topic"
# YouTube Music albums are playlists with this ID prefix, titled "Album - <name>".
ALBUM_PLAYLIST_PREFIX = "OLAK5uy_"
ALBUM_TITLE_PREFIX = "Album - "


def clean_artist(name: str) -> str:
    """YouTube's auto-generated music channels are named "<Artist> - Topic"."""
    return name.removesuffix(TOPIC_SUFFIX).strip()


class NormalizeMetadataPP(PostProcessor):
    """Make sure every track has an artist, even uploads YouTube doesn't tag as music.

    Runs right after extraction, so the artist is available both for the filename and
    for the tags written later by FFmpegMetadata.
    """

    def run(self, info):
        artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
        if not artists:
            uploader = info.get("uploader") or info.get("channel")
            if uploader:
                artists = [clean_artist(uploader)]
        if artists:
            # YouTube sometimes lists the same artist twice; keep the first occurrence.
            artists = list(dict.fromkeys(artists))
            info["artists"] = artists
            info["artist"] = ", ".join(artists)
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
