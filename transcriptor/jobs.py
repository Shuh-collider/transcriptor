"""Очередь заданий: SQLite, рабочий поток для видеокарты и отдельные потоки для Claude."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config
from .pipeline import STAGES, Cancelled, Models, Options, load_doc, save_doc, summarize_video, transcribe_video
from .summarize import SummaryError
from .youtube import DownloadError

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    batch_id TEXT,
    batch_title TEXT,
    video_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    channel TEXT,
    duration REAL,
    thumbnail TEXT,
    status TEXT NOT NULL,
    stage TEXT,
    progress REAL DEFAULT 0,
    message TEXT,
    error TEXT,
    notes TEXT DEFAULT '[]',
    options TEXT NOT NULL,
    language TEXT,
    cost_usd REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
"""

ACTIVE = ("queued", "running", "summarizing")


class JobManager:
    def __init__(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.models = Models()
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._cancel: set[str] = set()
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._last_emit: dict[str, float] = {}
        self._summary_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="claude")
        self._worker = threading.Thread(target=self._run, name="gpu-worker", daemon=True)
        self._idle_since = time.time()
        self._recover()

    # ---------- хранение ----------

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        job = dict(row)
        job["notes"] = json.loads(job.get("notes") or "[]")
        job["options"] = json.loads(job["options"])
        return job

    def _update(self, job_id: str, emit: bool = True, **values) -> dict | None:
        values["updated_at"] = time.time()
        for key in ("notes", "options"):
            if key in values:
                values[key] = json.dumps(values[key], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in values)
        with self.lock:
            self.db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", [*values.values(), job_id])
            self.db.commit()
            job = self.get(job_id)
        if emit and job:
            self._emit({"type": "job", "job": job})
        return job

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1000").fetchall()
        return [self._row(r) for r in rows]

    def _recover(self) -> None:
        """После перезапуска: прерванные задания снова в очередь, прерванные пересказы — заново."""
        with self.lock:
            self.db.execute(
                "UPDATE jobs SET status = 'queued', stage = NULL, progress = 0, message = NULL WHERE status = 'running'"
            )
            self.db.commit()
            pending = [r["id"] for r in self.db.execute("SELECT id FROM jobs WHERE status = 'summarizing'")]
        for job_id in pending:
            self._summary_pool.submit(self._summarize, job_id, None, None)

    # ---------- команды ----------

    def submit(self, videos: list[dict], options: dict, batch_title: str | None = None) -> list[dict]:
        opts = Options.from_dict(options).to_dict()
        batch_id = uuid.uuid4().hex[:12] if len(videos) > 1 else None
        now = time.time()
        created = []
        with self.lock:
            for i, v in enumerate(videos):
                job_id = uuid.uuid4().hex[:12]
                self.db.execute(
                    "INSERT INTO jobs (id, batch_id, batch_title, video_id, url, title, channel, duration, thumbnail,"
                    " status, options, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id, batch_id, batch_title if batch_id else None, v["id"], v["url"], v.get("title"),
                        v.get("channel"), v.get("duration"), v.get("thumbnail"), "queued",
                        json.dumps(opts, ensure_ascii=False), now + i * 1e-3, now,
                    ),
                )
                created.append(job_id)
            self.db.commit()
        jobs = [self.get(j) for j in created]
        for job in jobs:
            self._emit({"type": "job", "job": job})
        self._wakeup.set()
        return jobs

    def cancel(self, job_id: str) -> dict | None:
        job = self.get(job_id)
        if not job:
            return None
        if job["status"] == "queued":
            return self._update(job_id, status="canceled", message=None, finished_at=time.time())
        if job["status"] == "running":
            self._cancel.add(job_id)
        return job

    def retry(self, job_id: str) -> dict | None:
        job = self.get(job_id)
        if not job or job["status"] in ACTIVE:
            return job
        job = self._update(
            job_id, status="queued", stage=None, progress=0, message=None, error=None, notes=[],
            started_at=None, finished_at=None, created_at=time.time(),
        )
        self._wakeup.set()
        return job

    def resummarize(self, job_id: str, model: str | None, language: str | None) -> dict | None:
        job = self.get(job_id)
        if not job or job["status"] in ACTIVE:
            return job
        if load_doc(job["video_id"]) is None:
            return job
        job = self._update(job_id, status="summarizing", stage="summary", message="Готовлю пересказ…", error=None)
        self._summary_pool.submit(self._summarize, job_id, model, language)
        return job

    def delete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        if job["status"] == "running":
            self._cancel.add(job_id)
        with self.lock:
            self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self.db.commit()
        self._emit({"type": "deleted", "id": job_id})
        return True

    def status(self) -> dict:
        with self.lock:
            counts = dict(self.db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        return {"models_loaded": self.models.loaded(), "counts": counts}

    # ---------- события для веб-интерфейса ----------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers = {(lp, q) for lp, q in self._subscribers if q is not queue}

    def _emit(self, event: dict) -> None:
        for loop, queue in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except (RuntimeError, asyncio.QueueFull):
                self.unsubscribe(queue)

    # ---------- рабочие потоки ----------

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        self._summary_pool.shutdown(wait=False, cancel_futures=True)

    def _next(self) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._row(row)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._next()
            if job is None:
                if self.models.loaded() and time.time() - self._idle_since > config.IDLE_UNLOAD_SECONDS:
                    self.models.unload()
                    self._emit({"type": "status", **self.status()})
                self._wakeup.wait(timeout=5)
                self._wakeup.clear()
                continue
            try:
                self._process(job)
            except Exception:  # noqa: BLE001 — рабочий поток не должен умирать
                log.exception("job %s crashed", job["id"])
            self._idle_since = time.time()

    def _progress(self, job_id: str, stage: str, frac: float, message: str) -> None:
        order = list(STAGES)
        done = sum(STAGES[s] for s in order[: order.index(stage)]) if stage in STAGES else 0
        total = done + STAGES.get(stage, 0) * frac
        now = time.time()
        if now - self._last_emit.get(job_id, 0) < 0.25 and frac not in (0.0, 1.0):
            return
        self._last_emit[job_id] = now
        self._update(job_id, stage=stage, progress=round(total, 4), message=message)

    def _process(self, job: dict) -> None:
        job_id = job["id"]
        options = Options.from_dict(job["options"])
        self._update(job_id, status="running", started_at=time.time(), progress=0, error=None, notes=[])
        self._emit({"type": "status", **self.status()})
        try:
            doc = transcribe_video(
                job["video_id"],
                job["url"],
                options,
                self.models,
                report=lambda stage, frac, msg: self._progress(job_id, stage, frac, msg),
                cancelled=lambda: job_id in self._cancel,
            )
        except Cancelled:
            self._cancel.discard(job_id)
            if self.get(job_id):
                self._update(job_id, status="canceled", message=None, finished_at=time.time())
            return
        except DownloadError as exc:
            self._fail(job_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.error("job %s failed:\n%s", job_id, traceback.format_exc())
            self._fail(job_id, f"{type(exc).__name__}: {exc}")
            return
        finally:
            self._emit({"type": "status", **self.status()})

        video = doc["video"]
        values = dict(
            title=video.get("title") or job["title"],
            channel=video.get("channel") or job["channel"],
            duration=video.get("duration") or job["duration"],
            thumbnail=video.get("thumbnail") or job["thumbnail"],
            language=doc.get("language"),
            notes=doc.get("notes") or [],
            progress=1.0,
        )
        if options.summarize and doc["segments"]:
            self._update(job_id, status="summarizing", stage="summary", message="Готовлю пересказ…", **values)
            self._summary_pool.submit(self._summarize, job_id, None, None)
        else:
            self._update(job_id, status="done", stage=None, message=None, finished_at=time.time(), **values)

    def _fail(self, job_id: str, error: str) -> None:
        if self.get(job_id):
            self._update(job_id, status="error", message=None, error=error, finished_at=time.time())

    def _summarize(self, job_id: str, model: str | None, language: str | None) -> None:
        job = self.get(job_id)
        if not job:
            return
        options = Options.from_dict(job["options"])
        model = model if model in config.SUMMARY_MODELS else options.summary_model
        language = language or options.summary_language
        notes = [n for n in job["notes"] if not n.startswith("Пересказ")]
        try:
            doc = summarize_video(
                job["video_id"], model, language, lambda msg: self._update(job_id, message=msg)
            )
            cost = (doc.get("summary") or {}).get("cost_usd")
            self._update(job_id, status="done", stage=None, message=None, cost_usd=cost, notes=notes,
                         finished_at=time.time())
        except (SummaryError, FileNotFoundError) as exc:
            self._summary_failed(job_id, job["video_id"], str(exc), notes)
        except Exception as exc:  # noqa: BLE001
            log.error("summary %s failed:\n%s", job_id, traceback.format_exc())
            self._summary_failed(job_id, job["video_id"], f"Пересказ не сделан: {exc}", notes)

    def _summary_failed(self, job_id: str, video_id: str, error: str, notes: list[str]) -> None:
        doc = load_doc(video_id)
        if doc is not None:
            doc["summary_error"] = error
            save_doc(doc)
        if not error.startswith("Пересказ"):
            error = f"Пересказ не сделан: {error}"
        if self.get(job_id):
            self._update(job_id, status="done", stage=None, message=None, notes=notes + [error],
                         finished_at=time.time())
