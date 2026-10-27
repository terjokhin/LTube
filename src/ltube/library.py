"""Index of the downloaded library: which video ID is on disk where, and playlist order.

One SQLite file inside the library (OUT/.ltube/library.db), so it moves with the music.
Paths are stored relative to the library root.
"""

from __future__ import annotations

import fcntl
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

META_DIR = ".ltube"
SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE tracks (
        video_id     TEXT PRIMARY KEY,
        path         TEXT NOT NULL,
        artist       TEXT,
        title        TEXT,
        album        TEXT,
        album_artist TEXT,
        track_number INTEGER,
        duration_s   REAL,
        added_at     REAL NOT NULL
    )
    """,
    """
    CREATE TABLE playlists (
        playlist_id    TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        m3u_name       TEXT,
        last_synced_at REAL
    )
    """,
    """
    CREATE TABLE playlist_items (
        playlist_id TEXT NOT NULL,
        position    INTEGER NOT NULL,
        video_id    TEXT NOT NULL,
        PRIMARY KEY (playlist_id, position)
    )
    """,
    "CREATE INDEX playlist_items_video ON playlist_items (video_id)",
)

_TAG_FIELDS = ("artist", "title", "album", "album_artist", "track_number", "duration_s")


@dataclass(frozen=True)
class Track:
    video_id: str
    path: Path  # absolute
    artist: str | None
    title: str | None
    album: str | None
    album_artist: str | None
    track_number: int | None
    duration_s: float | None
    added_at: float


@dataclass(frozen=True)
class Playlist:
    playlist_id: str
    title: str
    m3u_name: str | None  # file name under the playlists folder, to remove it if the title changes
    last_synced_at: float | None


class Library:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._meta = root / META_DIR
        self._meta.mkdir(parents=True, exist_ok=True)
        # Downloads are staged here and moved into the library when done, on the same
        # filesystem so the move is atomic and a media server never sees half a file.
        self.tmp_dir = self._meta / "tmp"
        # Keep media servers (Navidrome) out of the index, lock file and staging folder.
        ignore = self._meta / ".ndignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        self._db_path = self._meta / "library.db"
        self._init_schema()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the library for one run, blocking until any other run has finished.

        Another process, or another Library for the same folder, waits here, then sees the
        files the first run added when it reads the index.
        """
        with open(self._meta / "lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # --- tracks ---

    def get_track(self, video_id: str) -> Track | None:
        """The track, or None if it was never downloaded or its file is gone.

        A row whose file has disappeared is deleted, so the track is downloaded again.
        """
        with self._db() as db:
            row = db.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            track = self._track(row)
            if not track.path.exists():
                db.execute("DELETE FROM tracks WHERE video_id = ?", (video_id,))
                return None
            return track

    def add_track(self, video_id: str, path: Path, **tags) -> None:
        """Record a downloaded file; a known ID gets its path and tags replaced."""
        _check_tags(tags)
        values = {field: tags.get(field) for field in _TAG_FIELDS}
        with self._db() as db:
            db.execute(
                """
                INSERT INTO tracks (video_id, path, artist, title, album, album_artist,
                                    track_number, duration_s, added_at)
                VALUES (:video_id, :path, :artist, :title, :album, :album_artist,
                        :track_number, :duration_s, :added_at)
                ON CONFLICT (video_id) DO UPDATE SET
                    path = excluded.path, artist = excluded.artist, title = excluded.title,
                    album = excluded.album, album_artist = excluded.album_artist,
                    track_number = excluded.track_number, duration_s = excluded.duration_s
                """,
                {"video_id": video_id, "path": self._relative(path), "added_at": time.time()} | values,
            )

    def move_track(self, video_id: str, path: Path, **tags) -> None:
        """Point a known track at its new file; only the given tags change."""
        _check_tags(tags)
        assignments = ", ".join(f"{field} = :{field}" for field in ("path", *tags))
        with self._db() as db:
            updated = db.execute(
                f"UPDATE tracks SET {assignments} WHERE video_id = :video_id",
                {"video_id": video_id, "path": self._relative(path)} | tags,
            )
            if updated.rowcount == 0:
                raise KeyError(video_id)

    # --- playlists ---

    def get_playlist(self, playlist_id: str) -> Playlist | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM playlists WHERE playlist_id = ?", (playlist_id,)).fetchone()
        return Playlist(**dict(row)) if row else None

    def playlist_with_m3u_name(self, m3u_name: str) -> Playlist | None:
        """The playlist whose m3u file has this name, to keep two playlists from sharing a file."""
        with self._db() as db:
            row = db.execute("SELECT * FROM playlists WHERE m3u_name = ?", (m3u_name,)).fetchone()
        return Playlist(**dict(row)) if row else None

    def playlist_items(self, playlist_id: str) -> list[str]:
        """The video IDs of a playlist in order, downloaded or not."""
        with self._db() as db:
            return self._item_ids(db, playlist_id)

    def set_playlist_items(
        self, playlist_id: str, title: str, video_ids: Iterable[str], *, m3u_name: str | None = None
    ) -> None:
        """Replace a playlist's items with `video_ids`, in one transaction."""
        with self._db() as db:
            self._write_playlist(db, playlist_id, title, list(video_ids), m3u_name)

    def merge_playlist_items(
        self, playlist_id: str, title: str, video_ids: Iterable[str], *, m3u_name: str | None = None
    ) -> None:
        """Like `set_playlist_items`, but keep the old items missing from `video_ids`.

        For listings that can't be trusted to be complete (YouTube hides unavailable videos),
        where a missing track may merely be unplayable, not removed. Each kept item stays
        right after the item that preceded it before.
        """
        with self._db() as db:
            merged = list(video_ids)
            anchor = -1  # index in `merged` of the last old item placed
            for video_id in self._item_ids(db, playlist_id):
                if video_id in merged:
                    anchor = merged.index(video_id)
                else:
                    anchor += 1
                    merged.insert(anchor, video_id)
            self._write_playlist(db, playlist_id, title, merged, m3u_name)

    def playlist_tracks(self, playlist_id: str) -> list[Track]:
        """The playlist's tracks in order, leaving out those not downloaded or whose file is gone."""
        with self._db() as db:
            rows = db.execute(
                """
                SELECT tracks.* FROM playlist_items
                JOIN tracks USING (video_id)
                WHERE playlist_items.playlist_id = ?
                ORDER BY playlist_items.position
                """,
                (playlist_id,),
            ).fetchall()
        tracks = [self._track(row) for row in rows]
        return [track for track in tracks if track.path.exists()]

    # --- internals ---

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """A short-lived connection per operation, so any thread can use the Library."""
        db = sqlite3.connect(self._db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:  # commit, or roll back on an error
                yield db
        finally:
            db.close()

    def _init_schema(self) -> None:
        # One transaction that takes the write lock first: two processes opening a new library
        # at once must not both see "no tables yet" and both create them.
        db = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self._db_path} was written by a newer ltube (index version {version}); upgrade ltube"
                )
            if version == 0:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("COMMIT")
        finally:
            db.close()
        try:
            sqlite3.connect(self._db_path).execute("PRAGMA journal_mode = WAL").close()
        except sqlite3.DatabaseError:
            pass  # a filesystem without shared memory support keeps the default journal

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            raise ValueError(f"{path} is outside the library {self.root}") from None

    def _track(self, row: sqlite3.Row) -> Track:
        return Track(**(dict(row) | {"path": self.root / row["path"]}))

    @staticmethod
    def _item_ids(db: sqlite3.Connection, playlist_id: str) -> list[str]:
        rows = db.execute(
            "SELECT video_id FROM playlist_items WHERE playlist_id = ? ORDER BY position", (playlist_id,)
        )
        return [row[0] for row in rows]

    @staticmethod
    def _write_playlist(
        db: sqlite3.Connection, playlist_id: str, title: str, video_ids: list[str], m3u_name: str | None
    ) -> None:
        db.execute(
            """
            INSERT INTO playlists (playlist_id, title, m3u_name, last_synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (playlist_id) DO UPDATE SET
                title = excluded.title,
                m3u_name = COALESCE(excluded.m3u_name, m3u_name),
                last_synced_at = excluded.last_synced_at
            """,
            (playlist_id, title, m3u_name, time.time()),
        )
        db.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
        db.executemany(
            "INSERT INTO playlist_items (playlist_id, position, video_id) VALUES (?, ?, ?)",
            [(playlist_id, position, video_id) for position, video_id in enumerate(video_ids, 1)],
        )


def _check_tags(tags: dict) -> None:
    if unknown := sorted(set(tags) - set(_TAG_FIELDS)):
        raise TypeError(f"unknown track field {unknown[0]!r}")
