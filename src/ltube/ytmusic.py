"""YouTube Music's own view of a playlist, for tracks the YouTube website hides.

Songs uploaded to YouTube Music are private videos: the youtube.com playlist that yt-dlp reads
leaves them out, and their video pages carry no artist or album. YouTube Music itself lists
them with the artist and album from the uploaded files, so ltube asks it, signed in with the
same cookies.
"""

from __future__ import annotations

import http.cookiejar
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from ytmusicapi import YTMusic
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

ORIGIN = "https://music.youtube.com"
# Albums of uploaded songs; their pages list the tracks in album order, with year and artist.
UPLOAD_ALBUM_PREFIX = "FEmusic_library_privately_owned_release_detail"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


@dataclass(frozen=True)
class MusicTrack:
    video_id: str
    title: str
    artists: list[str]
    album: str | None
    album_id: str | None = None
    # From the album's own page (uploads only, see with_album_details):
    track_number: int | None = None
    track_count: int | None = None
    year: int | None = None
    album_artists: list[str] = field(default_factory=list)


def music_playlist(cookiefile: Path, playlist_id: str) -> list[MusicTrack] | None:
    """The playlist's tracks according to YouTube Music, or None if it can't be read.

    Never raises: this only improves on what yt-dlp found, so any failure (API change,
    expired cookies, a private playlist of someone else's) just means "no extra information".
    """
    try:
        playlist = _client(cookiefile).get_playlist(playlist_id, limit=None)
    except Exception:  # noqa: BLE001 - see docstring
        return None
    return parse_tracks(playlist.get("tracks") or [])


def parse_tracks(tracks: list[dict]) -> list[MusicTrack]:
    return [
        MusicTrack(
            video_id=t["videoId"],
            title=t.get("title") or t["videoId"],
            artists=_names(t.get("artists")),
            album=(t.get("album") or {}).get("name"),
            album_id=(t.get("album") or {}).get("id"),
        )
        for t in tracks
        if t.get("videoId")
    ]


def with_album_details(
    cookiefile: Path, tracks: list[MusicTrack], pause: Callable[[], None]
) -> list[MusicTrack]:
    """Add track number, track count, year and album artist from each uploaded album's page.

    One request per distinct album, with `pause()` before each (pacing and cancellation are
    the caller's). Albums that can't be read are left as they are.
    """
    album_ids = list(dict.fromkeys(t.album_id for t in tracks if (t.album_id or "").startswith(UPLOAD_ALBUM_PREFIX)))
    if not album_ids:
        return tracks
    try:
        client = _client(cookiefile)
    except Exception:  # noqa: BLE001 - see music_playlist
        return tracks
    details: dict[str, dict] = {}
    for album_id in album_ids:
        pause()
        try:
            album = client.get_library_upload_album(album_id)
        except Exception:  # noqa: BLE001 - see music_playlist
            continue
        details |= album_details(album)
    return [replace(t, **details[t.video_id]) if t.video_id in details else t for t in tracks]


def album_details(album: dict) -> dict[str, dict]:
    """Per video ID: the fields an album page adds to a MusicTrack."""
    album_tracks = album.get("tracks") or []
    year = str(album.get("year") or "")
    common = {
        "track_count": album.get("trackCount") or len(album_tracks),
        "year": int(year) if year.isdigit() else None,
        "album_artists": _names(album.get("artists")),
    }
    return {t["videoId"]: common | {"track_number": n} for n, t in enumerate(album_tracks, 1) if t.get("videoId")}


def _names(artists: list[dict] | None) -> list[str]:
    return [a["name"] for a in artists or [] if a.get("name")]


def _client(cookiefile: Path) -> YTMusic:
    """A YTMusic client authenticated like a browser, from a Netscape cookies.txt."""
    jar = http.cookiejar.MozillaCookieJar(str(cookiefile))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar if c.domain.endswith("youtube.com"))
    headers = {
        "cookie": cookie,
        "authorization": get_authorization(f"{sapisid_from_cookie(cookie)} {ORIGIN}"),
        "x-goog-authuser": "0",
        "origin": ORIGIN,
        "x-origin": ORIGIN,
        "user-agent": USER_AGENT,
    }
    return YTMusic(auth=headers)
