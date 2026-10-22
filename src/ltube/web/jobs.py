"""Download queue for the web UI: one playlist at a time, progress fanned out to listeners."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ltube.backoff import BackoffConfig
from ltube.downloader import (
    Event,
    NamePatterns,
    PlaylistStarted,
    PruneSkipped,
    Status,
    Subfolders,
    Summary,
    TrackFinished,
    TrackProgress,
    TrackRemoved,
    TrackRenamed,
    TrackRetry,
    TrackStarted,
    Waiting,
    YtdlpWarning,
)

# Finished jobs kept in memory for the UI; older ones are forgotten.
MAX_FINISHED_JOBS = 50
MAX_MESSAGES = 100


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"  # every track downloaded, already there, or unavailable
    FAILED = "failed"  # some tracks failed, or the run stopped early
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    url: str
    out_dir: Path
    backoff: BackoffConfig
    playlist_id: str | None = None  # the PlaylistStore record this run updates
    subfolders: Subfolders | None = None
    patterns: NamePatterns = field(default_factory=NamePatterns)
    target_dir: Path | None = None  # exact folder of an earlier run (resyncs)
    prune: bool = False  # delete tracks that left the playlist (resyncs)
    created_at: float = field(default_factory=time.time)
    state: JobState = JobState.QUEUED
    title: str | None = None
    directory: str | None = None
    total: int | None = None
    tracks: list[dict] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # file names deleted by pruning
    renamed: int = 0  # files renamed to the current pattern
    current: dict | None = None
    waiting_until: float | None = None
    messages: list[str] = field(default_factory=list)
    error: str | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)

    def apply(self, event: Event) -> dict | None:
        """Update from a downloader event; returns a finished track to broadcast, if any."""
        match event:
            case PlaylistStarted(title, directory, total):
                self.title, self.directory, self.total = title, str(directory), total
            case TrackStarted(index, _, title):
                self.current = {"index": index, "title": title}
                self.waiting_until = None
            case TrackProgress(_, downloaded, total_bytes, speed) if self.current:
                self.current |= {"downloaded_bytes": downloaded, "total_bytes": total_bytes, "speed": speed}
            case TrackRetry(index, attempt, delay, message):
                self._note(f"#{index} attempt {attempt} failed: {message}; retrying in {delay:.0f}s")
            case Waiting(seconds):
                self.waiting_until = time.time() + seconds
            case YtdlpWarning(message):
                self._note(f"warning: {message}")
            case TrackRenamed(old, new):
                self.renamed += 1
                self._note(f"renamed {old.name} → {new.name}")
            case TrackRemoved(path, _):
                self.removed.append(path.name)
                self._note(f"removed {path.name}: no longer in the playlist")
            case PruneSkipped(reason):
                self._note(f"kept tracks that left the playlist: {reason}")
            case TrackFinished(index, result):
                track = {
                    "index": index,
                    "title": result.label or (self.current or {}).get("title", ""),
                    "status": result.status.value,
                    "file": result.path.name if result.path else None,
                    "error": result.error,
                }
                self.tracks.append(track)
                self.current = None
                return track
        return None

    def _note(self, message: str) -> None:
        self.messages = [*self.messages, message][-MAX_MESSAGES:]

    def finish(self, summary: Summary | None, error: str | None = None) -> None:
        self.current = None
        self.waiting_until = None
        if summary is not None:
            error = summary.aborted
        self.error = error
        if self.stop.is_set():
            self.state = JobState.CANCELLED
        elif error or (summary and summary.count(Status.FAILED)):
            self.state = JobState.FAILED
        else:
            self.state = JobState.DONE

    def public(self, *, tracks: bool = True) -> dict:
        counts: dict[str, int] = {}
        for t in self.tracks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        if self.renamed:
            counts["renamed"] = self.renamed
        if self.removed:
            counts["removed"] = len(self.removed)
        data = {
            "id": self.id,
            "url": self.url,
            "out_dir": str(self.out_dir),
            "playlist_id": self.playlist_id,
            "created_at": self.created_at,
            "state": self.state.value,
            "title": self.title,
            "directory": self.directory,
            "total": self.total,
            "processed": len(self.tracks),
            "counts": counts,
            "current": self.current,
            "waiting_until": self.waiting_until,
            "messages": self.messages,
            "error": self.error,
        }
        if tracks:
            data["tracks"] = self.tracks
        return data


# Runs one job to completion: (job, on_event, stop) -> Summary.
Runner = Callable[[Job, Callable[[Event], None], threading.Event], Summary]


class JobManager:
    """Owns all jobs; a single worker thread downloads them in submission order.

    Listeners are asyncio queues; the worker hands them messages through their loop,
    so all mutation happens under one lock and the event loop never blocks.
    """

    def __init__(self, runner: Runner, on_finish: Callable[[Job], None] | None = None) -> None:
        self._runner = runner
        self._on_finish = on_finish
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pending: queue.Queue[Job] = queue.Queue()
        self._listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._worker: threading.Thread | None = None

    def submit(self, url: str, out_dir: Path, backoff: BackoffConfig, **options) -> Job:
        """`options`: the optional Job fields (playlist_id, subfolders, patterns, target_dir, prune)."""
        job = Job(id=uuid.uuid4().hex[:12], url=url, out_dir=out_dir, backoff=backoff, **options)
        with self._lock:
            self._jobs[job.id] = job
            self._prune()
            self._publish_job(job)
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="ltube-worker", daemon=True)
                self._worker.start()
        self._pending.put(job)
        return job

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.finished:
                return job
            job.stop.set()
            if job.state is JobState.QUEUED:
                # The worker skips it when it comes up.
                job.state = JobState.CANCELLED
                self._publish_job(job)
            return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active_for(self, playlist_id: str) -> Job | None:
        with self._lock:
            return next(
                (j for j in self._jobs.values() if j.playlist_id == playlist_id and not j.finished), None
            )

    def broadcast(self, message: dict) -> None:
        with self._lock:
            self._publish(message)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [job.public() for job in self._jobs.values()]

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        """Must be called from the event loop; the snapshot and the stream don't overlap or gap."""
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._listeners.append((asyncio.get_running_loop(), q))
            return [job.public() for job in self._jobs.values()], q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._listeners = [(loop, lq) for loop, lq in self._listeners if lq is not q]

    def shutdown(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                job.stop.set()

    def _work(self) -> None:
        while True:
            job = self._pending.get()
            with self._lock:
                if job.state is not JobState.QUEUED:
                    continue
                job.state = JobState.RUNNING
                self._publish_job(job)
            summary, error = None, None
            try:
                summary = self._runner(job, lambda event: self._on_event(job, event), job.stop)
            except Exception as e:  # noqa: BLE001 - surface anything to the UI, keep the worker alive
                error = str(e).removeprefix("ERROR: ") or type(e).__name__
            with self._lock:
                job.finish(summary, error)
                self._publish_job(job)
            if self._on_finish:
                try:
                    self._on_finish(job)
                except Exception:  # noqa: BLE001 - bookkeeping must not kill the worker
                    pass

    def _on_event(self, job: Job, event: Event) -> None:
        with self._lock:
            track = job.apply(event)
            if track is not None:
                self._publish({"type": "track", "job_id": job.id, "track": track})
            self._publish_job(job)

    def _publish_job(self, job: Job) -> None:
        self._publish({"type": "job", "job": job.public(tracks=False)})

    def _publish(self, message: dict) -> None:
        for loop, q in self._listeners:
            try:
                loop.call_soon_threadsafe(q.put_nowait, message)
            except RuntimeError:  # loop already closed
                pass

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if j.finished]
        for job in finished[:-MAX_FINISHED_JOBS]:
            del self._jobs[job.id]
            self._publish({"type": "removed", "job_id": job.id})
