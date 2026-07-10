"""Fila simples de jobs em background (um worker, single-user local)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


@dataclass
class Job:
    id: str
    kind: str  # "process" | "mix" | "reprocess" | "reextract"
    label: str
    status: JobStatus = JobStatus.queued
    stage: str = "Na fila…"
    error: str | None = None
    meeting_id: int | None = None
    result_path: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    finished_at: str | None = None
    # params opacos pro worker
    params: dict[str, Any] = field(default_factory=dict)


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  error TEXT,
  meeting_id INTEGER,
  result_path TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  params TEXT NOT NULL DEFAULT '{}'
);
"""

_UPSERT = """
INSERT INTO jobs (id, kind, label, status, stage, error, meeting_id, result_path,
                  created_at, finished_at, params)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
  status=excluded.status,
  stage=excluded.stage,
  error=excluded.error,
  meeting_id=excluded.meeting_id,
  result_path=excluded.result_path,
  finished_at=excluded.finished_at,
  params=excluded.params;
"""


def _row_to_job(row: sqlite3.Row) -> Job:
    params: dict = {}
    try:
        params = json.loads(row["params"] or "{}")
    except Exception:
        pass
    return Job(
        id=row["id"],
        kind=row["kind"],
        label=row["label"],
        status=JobStatus(row["status"]),
        stage=row["stage"],
        error=row["error"],
        meeting_id=row["meeting_id"],
        result_path=row["result_path"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        params=params,
    )


class JobManager:
    """Fila FIFO com um thread worker — GPU Whisper não paraleliza bem."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._db_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

        # Abrir conexão SQLite e criar tabela; degradar graciosamente em falha.
        try:
            if db_path is None:
                from ..config import load_settings
                db_path = load_settings().db_path
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(_CREATE_TABLE)
            conn.commit()
            self._conn = conn
        except Exception:
            logger.exception("jobs: falha ao abrir SQLite — degradando para memória")
            self._conn = None

        # Startup recovery antes de iniciar o worker.
        self._recover()

        self._worker = threading.Thread(target=self._loop, name="meet-jobs", daemon=True)
        self._worker.start()

    # ── Persistência ─────────────────────────────────────────────────────────

    def _persist(self, job: Job) -> None:
        """UPSERT do job na tabela. Serializado por _db_lock. Silencioso em falha."""
        if self._conn is None:
            return
        # Snapshot fora de qualquer lock (campos são tipos simples — thread-safe como snapshot).
        row = (
            job.id,
            job.kind,
            job.label,
            job.status.value,
            job.stage,
            job.error,
            job.meeting_id,
            job.result_path,
            job.created_at,
            job.finished_at,
            json.dumps(job.params),
        )
        try:
            with self._db_lock:
                self._conn.execute(_UPSERT, row)
                self._conn.commit()
        except Exception:
            logger.exception("jobs: falha ao persistir job %s", job.id)

    def _recover(self) -> None:
        """Carrega jobs da tabela e corrige jobs presos em queued/running."""
        if self._conn is None:
            return
        try:
            with self._db_lock:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at ASC LIMIT 100"
                ).fetchall()
        except Exception:
            logger.exception("jobs: falha ao carregar jobs no startup")
            return

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stuck: list[Job] = []
        for row in rows:
            try:
                job = _row_to_job(row)
            except Exception:
                continue
            if job.status in (JobStatus.queued, JobStatus.running):
                job.status = JobStatus.error
                job.stage = "Interrompido"
                job.error = "Interrompido por reinício do servidor"
                job.finished_at = now
                stuck.append(job)
            self._jobs[job.id] = job
            self._order.append(job.id)

        for job in stuck:
            self._persist(job)

    # ── API pública ──────────────────────────────────────────────────────────

    def submit(self, kind: str, label: str, **params: Any) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, label=label, params=params)
        with self._cv:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._cv.notify()
        self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i] for i in ids]

    # ── Worker ───────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            with self._cv:
                while True:
                    next_id = next(
                        (
                            jid
                            for jid in self._order
                            if self._jobs[jid].status == JobStatus.queued
                        ),
                        None,
                    )
                    if next_id is not None:
                        break
                    self._cv.wait()
                job = self._jobs[next_id]
                job.status = JobStatus.running
                job.stage = "Iniciando…"

            self._persist(job)

            try:
                self._run(job)
                with self._lock:
                    job.status = JobStatus.done
                    job.finished_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                self._persist(job)
            except Exception as exc:
                with self._lock:
                    job.status = JobStatus.error
                    job.error = f"{exc}\n{traceback.format_exc()[-800:]}"
                    job.stage = "Falhou"
                    job.finished_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                self._persist(job)

    def _run(self, job: Job) -> None:
        from ..config import load_settings
        from ..store import Store

        settings = load_settings()
        store = Store(settings.db_path)

        def progress(msg: str) -> None:
            with self._lock:
                job.stage = msg
            self._persist(job)

        if job.kind == "process":
            from ..pipeline import run_pipeline

            video = Path(job.params["video"])
            meeting_id, _result, md_path = run_pipeline(
                video,
                settings=settings,
                store=store,
                title=job.params.get("title") or None,
                mic_track=int(job.params.get("mic_track", 1)),
                others_track=int(job.params.get("others_track", 2)),
                no_llm=bool(job.params.get("no_llm", False)),
                import_media=bool(job.params.get("import_media", True)),
                num_speakers=int(job.params.get("num_speakers", 0)),
                on_progress=progress,
            )
            with self._lock:
                job.meeting_id = meeting_id
                job.result_path = str(md_path)
            return

        if job.kind == "mix":
            from ..audio import (
                PREVIEW_FULL,
                PREVIEW_WEB,
                ensure_listen_mix,
                ensure_listen_preview,
            )

            video = Path(job.params["video"])
            mic = int(job.params.get("mic_track", 1))
            others = int(job.params.get("others_track", 2))
            progress("Gerando preview original (full)…")
            try:
                out = ensure_listen_preview(
                    video,
                    force=True,
                    mic_track=mic,
                    others_track=others,
                    quality=PREVIEW_FULL,
                )
                progress("Gerando preview leve (web)…")
                ensure_listen_preview(
                    video,
                    force=True,
                    mic_track=mic,
                    others_track=others,
                    quality=PREVIEW_WEB,
                )
            except Exception:
                progress("Sem vídeo — gerando só áudio…")
                out = ensure_listen_mix(
                    video, force=True, mic_track=mic, others_track=others
                )
            else:
                progress("Gerando mix só de áudio…")
                ensure_listen_mix(
                    video, force=True, mic_track=mic, others_track=others
                )
            with self._lock:
                job.result_path = str(out)
            return

        if job.kind == "reprocess":
            from ..pipeline import reprocess_meeting

            meeting_id = int(job.params["meeting_id"])
            result = reprocess_meeting(
                meeting_id,
                settings=settings,
                store=store,
                mic_track=int(job.params.get("mic_track", 1)),
                others_track=int(job.params.get("others_track", 2)),
                no_llm=bool(job.params.get("no_llm", False)),
                num_speakers=int(job.params.get("num_speakers", 0)),
                on_progress=progress,
            )
            row = store._conn.execute(
                "SELECT md_path FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            with self._lock:
                job.meeting_id = meeting_id
                job.result_path = row["md_path"] if row else None
            return

        if job.kind == "reextract":
            from ..pipeline import reextract_meeting

            meeting_id = int(job.params["meeting_id"])
            reextract_meeting(
                meeting_id,
                settings=settings,
                store=store,
                on_progress=progress,
            )
            with self._lock:
                job.meeting_id = meeting_id
            return

        raise ValueError(f"kind desconhecido: {job.kind}")


# Singleton do processo do servidor
manager = JobManager()
