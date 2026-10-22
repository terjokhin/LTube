"""HTTP API and static file serving for the web UI."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp.utils import parse_bytes

from ltube.backoff import BackoffConfig
from ltube.downloader import Event, NamePatterns, PlaylistDownloader, Subfolders, Summary
from ltube.metadata import validate_name_pattern
from ltube.web.playlists import PlaylistStore
from ltube.web.jobs import Job, JobManager, Runner

STATIC_DIR = Path(__file__).parent / "static"
MAX_COOKIES_BYTES = 1_000_000
KEEPALIVE_SECONDS = 15
DEFAULT_SUBFOLDERS = Subfolders(albums="Albums", playlists="Playlists")


class SettingsUpdate(BaseModel):
    """Only the fields present are changed."""

    out_dir: str | None = None
    use_subfolders: bool | None = None
    albums_subfolder: str | None = None
    playlists_subfolder: str | None = None
    album_pattern: str | None = None
    playlist_pattern: str | None = None
    delete_removed: bool | None = None  # resyncs delete tracks that left the playlist
    # Pacing, see BackoffConfig
    sleep_min: float | None = None
    sleep_max: float | None = None
    request_sleep: float | None = None
    retries: int | None = None
    backoff_base: float | None = None
    backoff_max: float | None = None
    max_consecutive_failures: int | None = None
    rate_limit: str | None = None  # e.g. "2M" bytes/s; empty = unlimited


PACING_FIELDS = (
    "sleep_min",
    "sleep_max",
    "request_sleep",
    "retries",
    "backoff_base",
    "backoff_max",
    "max_consecutive_failures",
    "rate_limit",
)


def _pacing_defaults() -> dict:
    d = BackoffConfig()
    return {
        "sleep_min": d.sleep_min,
        "sleep_max": d.sleep_max,
        "request_sleep": d.request_sleep,
        "retries": d.retries,
        "backoff_base": d.base,
        "backoff_max": d.cap,
        "max_consecutive_failures": d.max_consecutive_failures,
        "rate_limit": "",
    }


class PlaylistUpdate(BaseModel):
    favourite: bool


class JobRequest(BaseModel):
    url: str
    favourite: bool = False  # also list the playlist in Favourites


def create_app(
    out_dir: Path, data_dir: Path, *, lock_out_dir: bool = False, runner: Runner | None = None
) -> FastAPI:
    """`out_dir` is the initial download folder; Settings can change it unless `lock_out_dir`
    (Docker: the folder is a volume mount, a path typed in the UI would land inside the container).
    """
    cookies_path = data_dir / "cookies.txt"
    settings_path = data_dir / "settings.json"
    playlists = PlaylistStore(data_dir / "playlists.json", legacy_favourites=data_dir / "favourites.json")

    def current_out_dir() -> Path:
        if not lock_out_dir and (saved := _read_json(settings_path).get("out_dir")):
            return Path(saved)
        return out_dir

    def current_subfolders() -> Subfolders:
        saved = _read_json(settings_path)
        return Subfolders(
            albums=saved.get("albums_subfolder") or DEFAULT_SUBFOLDERS.albums,
            playlists=saved.get("playlists_subfolder") or DEFAULT_SUBFOLDERS.playlists,
        )

    def use_subfolders() -> bool:
        return bool(_read_json(settings_path).get("use_subfolders"))

    def current_patterns() -> NamePatterns:
        saved = _read_json(settings_path)
        default = NamePatterns()
        return NamePatterns(
            album=saved.get("album_pattern") or default.album,
            playlist=saved.get("playlist_pattern") or default.playlist,
        )

    def delete_removed() -> bool:
        return bool(_read_json(settings_path).get("delete_removed", False))

    def current_pacing() -> dict:
        saved = _read_json(settings_path)
        return _pacing_defaults() | {k: saved[k] for k in PACING_FIELDS if k in saved}

    def run_job(job: Job, on_event, stop: threading.Event) -> Summary:
        downloader = PlaylistDownloader(
            job.out_dir,
            cookies_path,
            job.backoff,
            subfolders=job.subfolders,
            patterns=job.patterns,
            prune=job.prune,
            on_event=on_event,
            stop=stop,
        )
        return downloader.run(job.url, directory=job.target_dir)

    def record_sync(job: Job) -> None:
        if not job.playlist_id:
            return
        fields = {
            "last_synced_at": time.time(),
            "last_state": job.state.value,
            "last_counts": job.public(tracks=False)["counts"],
            "last_error": job.error,
        }
        if job.directory:
            fields |= {"directory": job.directory, "title": job.title}
        playlists.update(job.playlist_id, **fields)
        publish_playlists()

    def publish_playlists() -> None:
        manager.broadcast({"type": "playlists", "playlists": playlists.list()})

    def submit(record: dict) -> dict:
        """Queue a run of a known playlist. If it was downloaded before, this is a resync: it fills
        the folder of the first run (found even if Settings changed since) and may prune.
        """
        if manager.active_for(record["id"]):
            raise HTTPException(409, "this playlist is already queued or downloading")
        root = current_out_dir()
        pinned = Path(record["directory"]) if record.get("directory") else None
        if pinned and lock_out_dir and not pinned.is_relative_to(root):
            pinned = None  # recorded outside the Docker mount; it would land inside the container
        options = {
            "playlist_id": record["id"],
            "subfolders": current_subfolders() if use_subfolders() else None,
            "patterns": current_patterns(),
            "target_dir": pinned,
            "prune": pinned is not None and delete_removed(),
        }
        return manager.submit(record["url"], root, _backoff(current_pacing()), **options).public()

    manager = JobManager(runner or run_job, on_finish=record_sync)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(title="ltube", lifespan=lifespan)
    app.state.manager = manager

    def settings_info() -> dict:
        subfolders = current_subfolders()
        patterns = current_patterns()
        return {
            "out_dir": str(current_out_dir()),
            "out_dir_locked": lock_out_dir,
            "use_subfolders": use_subfolders(),
            "albums_subfolder": subfolders.albums,
            "playlists_subfolder": subfolders.playlists,
            "album_pattern": patterns.album,
            "playlist_pattern": patterns.playlist,
            "delete_removed": delete_removed(),
            **current_pacing(),
            "pacing_defaults": _pacing_defaults(),
            "cookies": _cookies_info(cookies_path),
        }

    @app.get("/api/settings")
    def get_settings() -> dict:
        return settings_info()

    @app.put("/api/settings")
    def put_settings(update: SettingsUpdate) -> dict:
        changes: dict = {}
        if update.out_dir is not None and update.out_dir.strip() != str(current_out_dir()):
            if lock_out_dir:
                raise HTTPException(409, "the download folder is fixed by the server (a Docker volume mount)")
            changes["out_dir"] = str(_validate_out_dir(update.out_dir))
        if update.use_subfolders is not None:
            changes["use_subfolders"] = update.use_subfolders
        if update.delete_removed is not None:
            changes["delete_removed"] = update.delete_removed
        for key in ("albums_subfolder", "playlists_subfolder"):
            if (value := getattr(update, key)) is not None:
                changes[key] = _validate_subfolder(value)
        for key in ("album_pattern", "playlist_pattern"):
            if (value := getattr(update, key)) is not None:
                try:
                    changes[key] = validate_name_pattern(value)
                except ValueError as e:
                    raise HTTPException(422, str(e)) from None
        pacing = {k: v for k in PACING_FIELDS if (v := getattr(update, k)) is not None}
        if "rate_limit" in pacing:
            pacing["rate_limit"] = pacing["rate_limit"].strip()
        if pacing:
            _backoff(current_pacing() | pacing)  # validate the combination before saving
            changes |= pacing
        _write_json(settings_path, _read_json(settings_path) | changes)
        return settings_info()

    @app.put("/api/settings/cookies")
    async def put_cookies(request: Request) -> dict:
        body = await request.body()
        if len(body) > MAX_COOKIES_BYTES:
            raise HTTPException(413, "cookies file is too large")
        count = _validate_cookies(body)
        data_dir.mkdir(parents=True, exist_ok=True)
        # Write atomically and private: this is a live browser session.
        fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".cookies-")
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cookies_path)
        return settings_info() | {"loaded": count}

    @app.delete("/api/settings/cookies")
    def delete_cookies() -> dict:
        cookies_path.unlink(missing_ok=True)
        return settings_info()

    @app.get("/api/jobs")
    def list_jobs() -> list[dict]:
        return manager.snapshot()

    @app.post("/api/jobs", status_code=201)
    def create_job(req: JobRequest) -> dict:
        url = req.url.strip()
        if not url.startswith(("https://", "http://")):
            raise HTTPException(422, "URL must start with https://")
        if not cookies_path.exists():
            raise HTTPException(409, "upload cookies.txt in Settings first")
        # Every download is remembered, so downloading the same playlist again is a resync.
        known = playlists.find(url)
        record = playlists.add(url, favourite=req.favourite)
        if known is None or req.favourite:
            publish_playlists()
        return submit(record)

    @app.get("/api/playlists")
    def list_playlists() -> list[dict]:
        return playlists.list()

    @app.post("/api/playlists/{playlist_id}/sync", status_code=201)
    def sync_playlist(playlist_id: str) -> dict:
        record = playlists.get(playlist_id)
        if record is None:
            raise HTTPException(404, "no such playlist")
        if not cookies_path.exists():
            raise HTTPException(409, "upload cookies.txt in Settings first")
        return submit(record)

    @app.put("/api/playlists/{playlist_id}")
    def update_playlist(playlist_id: str, update: PlaylistUpdate) -> dict:
        """Add to or remove from Favourites. Files and the remembered folder are kept."""
        record = playlists.update(playlist_id, favourite=update.favourite)
        if record is None:
            raise HTTPException(404, "no such playlist")
        publish_playlists()
        return record

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str) -> dict:
        job = manager.cancel(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return job.public(tracks=False)

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        snapshot, queue = manager.subscribe()

        async def stream():
            try:
                yield _sse({"type": "snapshot", "jobs": snapshot, "playlists": playlists.list()})
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(message)
            finally:
                manager.unsubscribe(queue)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    else:

        @app.get("/", response_class=PlainTextResponse)
        def no_ui() -> str:
            return "The web UI is not built. Run `npm run build` in web/ (or use `npm run dev`)."

    return app


def _sse(message: dict) -> str:
    return f"data: {json.dumps(message)}\n\n"


def _validate_out_dir(value: str) -> Path:
    path = Path(value.strip()).expanduser()
    if not value.strip() or not path.is_absolute():
        raise HTTPException(422, "enter an absolute folder path, e.g. /Users/me/Music or ~/Music")
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise HTTPException(422, f"{path} is a file, not a folder")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(422, f"can't create {path}: {e.strerror}") from None
    if not os.access(path, os.W_OK):
        raise HTTPException(422, f"{path} is not writable")
    return path


def _validate_subfolder(value: str) -> str:
    """A relative folder (nesting allowed) that stays inside the download folder."""
    cleaned = value.strip().rstrip("/").strip()
    parts = Path(cleaned).parts
    if not cleaned or Path(cleaned).is_absolute() or any(p in ("..", ".") for p in parts):
        raise HTTPException(422, f"subfolder must be a plain folder name like Albums, got {value!r}")
    return "/".join(parts)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _backoff(pacing: dict) -> BackoffConfig:
    """Build (and so validate) a BackoffConfig from the pacing settings."""
    try:
        rate = None
        if pacing["rate_limit"]:
            rate = parse_bytes(pacing["rate_limit"])
            if rate is None:
                raise ValueError(f"can't parse bandwidth limit {pacing['rate_limit']!r}; use e.g. 2M or 500K")
        return BackoffConfig(
            sleep_min=pacing["sleep_min"],
            sleep_max=pacing["sleep_max"],
            request_sleep=pacing["request_sleep"],
            retries=pacing["retries"],
            base=pacing["backoff_base"],
            cap=pacing["backoff_max"],
            max_consecutive_failures=pacing["max_consecutive_failures"],
            rate_limit=rate,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


def _validate_cookies(body: bytes) -> int:
    """Parse as a Netscape cookies.txt; returns how many cookies it holds."""
    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        f.write(body)
        f.flush()
        jar = MozillaCookieJar()
        try:
            jar.load(f.name, ignore_discard=True, ignore_expires=True)
        except (LoadError, UnicodeDecodeError) as e:
            raise HTTPException(422, f"not a Netscape-format cookies.txt: {e}") from None
    if not any("youtube.com" in c.domain for c in jar):
        raise HTTPException(422, "cookies.txt has no youtube.com cookies; export it while signed in to YouTube")
    return len(jar)


def _cookies_info(path: Path) -> dict:
    if not path.exists():
        return {"present": False, "updated_at": None}
    return {"present": True, "updated_at": path.stat().st_mtime}
