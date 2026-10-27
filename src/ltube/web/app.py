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

from ltube import get_version
from ltube.backoff import BackoffConfig
from ltube.downloader import PlaylistDownloader, Summary
from ltube.metadata import DEFAULT_TRACK_PATTERN, validate_track_pattern
from ltube.web.playlists import PlaylistStore
from ltube.web.jobs import Job, JobManager, Runner

STATIC_DIR = Path(__file__).parent / "static"
MAX_COOKIES_BYTES = 1_000_000
KEEPALIVE_SECONDS = 15


class SettingsUpdate(BaseModel):
    """Only the fields present are changed."""

    out_dir: str | None = None
    track_pattern: str | None = None  # file name inside Artist/Album/, see metadata.render_name
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

    def current_pattern() -> str:
        return _read_json(settings_path).get("track_pattern") or DEFAULT_TRACK_PATTERN

    def current_pacing() -> dict:
        saved = _read_json(settings_path)
        return _pacing_defaults() | {k: saved[k] for k in PACING_FIELDS if k in saved}

    def run_job(job: Job, on_event, stop: threading.Event) -> Summary:
        downloader = PlaylistDownloader(
            job.out_dir,
            cookies_path,
            job.backoff,
            pattern=job.pattern,
            on_event=on_event,
            stop=stop,
        )
        return downloader.run(job.url)

    def record_sync(job: Job) -> None:
        if not job.playlist_id:
            return
        fields = {
            "last_synced_at": time.time(),
            "last_state": job.state.value,
            "last_counts": job.public(tracks=False)["counts"],
            "last_error": job.error,
        }
        if job.title:
            fields["title"] = job.title
        playlists.update(job.playlist_id, **fields)
        publish_playlists()

    def publish_playlists() -> None:
        manager.broadcast({"type": "playlists", "playlists": playlists.list()})

    def submit(record: dict) -> dict:
        """Queue a run of a known playlist. Downloading one again is a sync: tracks the library
        already has are skipped, wherever they came from.
        """
        if manager.active_for(record["id"]):
            raise HTTPException(409, "this playlist is already queued or downloading")
        options = {"playlist_id": record["id"], "pattern": current_pattern()}
        return manager.submit(record["url"], current_out_dir(), _backoff(current_pacing()), **options).public()

    manager = JobManager(runner or run_job, on_finish=record_sync)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(title="ltube", version=get_version(), lifespan=lifespan)
    app.state.manager = manager

    def settings_info() -> dict:
        return {
            "out_dir": str(current_out_dir()),
            "out_dir_locked": lock_out_dir,
            "track_pattern": current_pattern(),
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
        if update.track_pattern is not None:
            try:
                changes["track_pattern"] = validate_track_pattern(update.track_pattern)
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
        """Add to or remove from Favourites. Downloaded files are kept."""
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
