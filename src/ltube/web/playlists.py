"""Every playlist ltube has downloaded, so downloading one again acts as a resync.

Stored as JSON in the data folder. Favourites are just records with `favourite` set.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def playlist_key(url: str) -> str:
    """What makes two URLs the same playlist: the `list=` ID, an album's browse ID, or the URL."""
    parsed = urlparse(url.strip())
    if ids := parse_qs(parsed.query).get("list"):
        return ids[0]
    if m := re.search(r"/browse/([\w-]+)", parsed.path):
        return m[1]
    return url.strip()


class PlaylistStore:
    """After its first run a playlist pins the exact folder it downloaded into, so later runs
    fill the same folder even if the playlist is renamed or Settings change.
    """

    def __init__(self, path: Path, legacy_favourites: Path | None = None) -> None:
        self._path = path
        self._legacy = legacy_favourites
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        with self._lock:
            return self._load()

    def get(self, playlist_id: str) -> dict | None:
        return next((p for p in self.list() if p["id"] == playlist_id), None)

    def find(self, url: str) -> dict | None:
        key = playlist_key(url)
        return next((p for p in self.list() if p["key"] == key), None)

    def add(self, url: str, *, favourite: bool = False) -> dict:
        """The record for `url`, created if new; `favourite=True` also marks an existing one."""
        with self._lock:
            playlists = self._load()
            key = playlist_key(url)
            if existing := next((p for p in playlists if p["key"] == key), None):
                if favourite and not existing["favourite"]:
                    existing["favourite"] = True
                    self._save(playlists)
                return existing
            record = {
                "id": uuid.uuid4().hex[:12],
                "key": key,
                "url": url.strip(),
                "favourite": favourite,
                "directory": None,
                "title": None,
                "added_at": time.time(),
                "last_synced_at": None,
                "last_state": None,
                "last_counts": {},
                "last_error": None,
            }
            self._save([*playlists, record])
            return record

    def update(self, playlist_id: str, **fields) -> dict | None:
        with self._lock:
            playlists = self._load()
            record = next((p for p in playlists if p["id"] == playlist_id), None)
            if record is not None:
                record.update(fields)
                self._save(playlists)
            return record

    def _load(self) -> list[dict]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._migrate()

    def _migrate(self) -> list[dict]:
        """Adopt favourites.json from before this store existed (all of them were favourites)."""
        if not (self._legacy and self._legacy.exists()):
            return []
        playlists = [p | {"favourite": True} for p in json.loads(self._legacy.read_text(encoding="utf-8"))]
        self._save(playlists)
        self._legacy.rename(self._legacy.with_name(self._legacy.name + ".migrated"))
        return playlists

    def _save(self, playlists: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(playlists, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
