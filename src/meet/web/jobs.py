"""Fila simples de jobs em background (um worker, single-user local)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..progress import ProgressTracker, ProgressUpdate, StepSpec

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
    progress: ProgressUpdate | None = None
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
  params TEXT NOT NULL DEFAULT '{}',
  progress_json TEXT
);
"""

_UPSERT = """
INSERT INTO jobs (id, kind, label, status, stage, error, meeting_id, result_path,
                  created_at, finished_at, params, progress_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
  status=excluded.status,
  stage=excluded.stage,
  error=excluded.error,
  meeting_id=excluded.meeting_id,
  result_path=excluded.result_path,
  finished_at=excluded.finished_at,
  params=excluded.params,
  progress_json=excluded.progress_json;
"""


def _row_to_job(row: sqlite3.Row) -> Job:
    params: dict = {}
    try:
        params = json.loads(row["params"] or "{}")
    except Exception:
        pass
    progress: ProgressUpdate | None = None
    try:
        if "progress_json" in row.keys() and row["progress_json"]:
            raw_progress = json.loads(row["progress_json"])
            if isinstance(raw_progress, dict):
                progress = ProgressUpdate.from_dict(raw_progress)
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
        progress=progress,
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
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "progress_json" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN progress_json TEXT")
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
            json.dumps(job.progress.to_dict()) if job.progress is not None else None,
        )
        try:
            with self._db_lock:
                self._conn.execute(_UPSERT, row)
                self._conn.commit()
        except Exception:
            logger.exception("jobs: falha ao persistir job %s", job.id)

    def _recover(self) -> None:
        """Carrega jobs da tabela e corrige jobs presos em queued/running.

        Sempre recupera *todos* os non-terminais (não podem ficar stuck invisíveis
        após >100 jobs históricos). Terminais: só os N mais recentes (DESC).
        """
        if self._conn is None:
            return
        try:
            with self._db_lock:
                # Non-terminais: sem LIMIT — restart não pode deixar job recente
                # em running eterno fora da memória.
                active_rows = self._conn.execute(
                    "SELECT * FROM jobs"
                    " WHERE status IN ('queued', 'running')"
                    " ORDER BY created_at ASC"
                ).fetchall()
                terminal_rows = self._conn.execute(
                    "SELECT * FROM jobs"
                    " WHERE status IN ('done', 'error')"
                    " ORDER BY created_at DESC"
                    f" LIMIT {self._MEMORY_CAP}"
                ).fetchall()
        except Exception:
            logger.exception("jobs: falha ao carregar jobs no startup")
            return

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stuck: list[Job] = []
        # Ordem de memória: terminais antigos→recentes + non-terminais por created_at
        # (list_recent inverte; terminais DESC no SQL → reverse para ASC no _order).
        for row in reversed(terminal_rows):
            try:
                job = _row_to_job(row)
            except Exception:
                continue
            self._jobs[job.id] = job
            self._order.append(job.id)

        for row in active_rows:
            try:
                job = _row_to_job(row)
            except Exception:
                continue
            if job.status in (JobStatus.queued, JobStatus.running):
                job.status = JobStatus.error
                job.stage = "Interrompido"
                job.error = "Interrompido por reinício do servidor"
                job.finished_at = now
                if job.progress is not None:
                    job.progress = job.progress.failed(job.error)
                stuck.append(job)
            self._jobs[job.id] = job
            self._order.append(job.id)

        for job in stuck:
            self._persist(job)

    # ── API pública ──────────────────────────────────────────────────────────

    # Quantos jobs terminais manter em memória (lista da UI + get).
    _MEMORY_CAP = 100

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
            return [self._jobs[i] for i in ids if i in self._jobs]

    def retry(self, job_id: str) -> Job | None:
        """Reenvia um job terminal com os mesmos kind/params. None se não existir ou ainda ativo."""
        with self._lock:
            old = self._jobs.get(job_id)
            if old is None:
                return None
            if old.status not in (JobStatus.done, JobStatus.error):
                return None
            kind = old.kind
            label = old.label
            params = dict(old.params)
        return self.submit(kind=kind, label=f"retry · {label}", **params)

    def _evict_old_jobs(self) -> None:
        """Corta só jobs *terminais* antigos; nunca descarta queued/running.

        Com fila longa, manter os últimos N de ``_order`` inteiro apagava
        queued no começo da fila — o worker nunca os pegava de novo.
        """
        with self._lock:
            active: list[str] = []
            terminals: list[str] = []
            for jid in self._order:
                job = self._jobs.get(jid)
                if job is None:
                    continue
                if job.status in (JobStatus.queued, JobStatus.running):
                    active.append(jid)
                else:
                    terminals.append(jid)
            if len(terminals) <= self._MEMORY_CAP and len(active) + len(terminals) == len(
                self._order
            ):
                # Nada a cortar (e _order só tem ids vivos)
                if all(j in self._jobs for j in self._order):
                    return
            keep_terminals = terminals[-self._MEMORY_CAP :]
            keep_set = set(active) | set(keep_terminals)
            self._order = [jid for jid in self._order if jid in keep_set]
            for jid in list(self._jobs):
                if jid not in keep_set:
                    del self._jobs[jid]

    # ── Worker ───────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            with self._cv:
                while True:
                    next_id = next(
                        (
                            jid
                            for jid in self._order
                            if jid in self._jobs
                            and self._jobs[jid].status == JobStatus.queued
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
                    if job.progress is not None:
                        job.progress = replace(
                            job.progress,
                            percent=100.0,
                            step_percent=100.0,
                            steps=tuple(
                                step
                                if step.state == "error"
                                else replace(step, state="done")
                                for step in job.progress.steps
                            ),
                        )
                    job.finished_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                self._persist(job)
                self._evict_old_jobs()
            except Exception as exc:
                logger.exception("job %s (%s) falhou", job.id, job.kind)
                with self._lock:
                    job.status = JobStatus.error
                    # Mensagem curta para a API/UI; traceback só no log do servidor.
                    job.error = str(exc)[:500] or exc.__class__.__name__
                    job.stage = "Falhou"
                    if job.progress is not None:
                        job.progress = job.progress.failed(str(exc))
                    job.finished_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                self._persist(job)
                self._evict_old_jobs()

    def _run(self, job: Job) -> None:
        from ..config import load_settings
        from ..store import Store

        settings = load_settings()
        store = Store(settings.db_path)

        last_persisted_progress: ProgressUpdate | None = None
        last_progress_commit = 0.0

        def progress(update: ProgressUpdate) -> None:
            nonlocal last_persisted_progress, last_progress_commit
            now = time.monotonic()
            with self._lock:
                job.progress = update
                job.stage = update.detail
            elapsed = now - last_progress_commit
            delta = (
                abs(update.percent - last_persisted_progress.percent)
                if last_persisted_progress is not None
                else 100.0
            )
            should_persist = (
                last_persisted_progress is None
                or update.step != last_persisted_progress.step
                or elapsed >= 2.0
                or (elapsed >= 0.5 and delta >= 0.5)
            )
            if should_persist:
                self._persist(job)
                last_persisted_progress = update
                last_progress_commit = now

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
                analyze_visual=bool(job.params.get("analyze_visual", False)),
                import_media=bool(job.params.get("import_media", True)),
                num_speakers=int(job.params.get("num_speakers", 0)),
                on_progress=progress,
                project_id=job.params.get("project_id") or None,
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
            tracker = ProgressTracker(
                (
                    StepSpec("preview_full", "Preview original", 1.0),
                    StepSpec("preview_web", "Preview web", 1.0),
                    StepSpec("audio_mix", "Mix de áudio", 1.0),
                ),
                progress,
            )

            video = Path(job.params["video"])
            mic = int(job.params.get("mic_track", 1))
            others = int(job.params.get("others_track", 2))
            tracker.start("preview_full", "Gerando preview original (full)…", determinate=False)
            try:
                out = ensure_listen_preview(
                    video,
                    force=True,
                    mic_track=mic,
                    others_track=others,
                    quality=PREVIEW_FULL,
                )
                tracker.start("preview_web", "Gerando preview leve (web)…", determinate=False)
                ensure_listen_preview(
                    video,
                    force=True,
                    mic_track=mic,
                    others_track=others,
                    quality=PREVIEW_WEB,
                )
            except Exception:
                tracker.start("audio_mix", "Sem vídeo — gerando só áudio…", determinate=False)
                out = ensure_listen_mix(
                    video, force=True, mic_track=mic, others_track=others
                )
            else:
                tracker.start("audio_mix", "Gerando mix só de áudio…", determinate=False)
                ensure_listen_mix(
                    video, force=True, mic_track=mic, others_track=others
                )
            tracker.finish("Mix concluído")
            with self._lock:
                job.result_path = str(out)
            return

        if job.kind == "reprocess":
            from ..pipeline import reprocess_meeting

            meeting_id = int(job.params["meeting_id"])
            reprocess_meeting(
                meeting_id,
                settings=settings,
                store=store,
                mic_track=int(job.params.get("mic_track", 1)),
                others_track=int(job.params.get("others_track", 2)),
                no_llm=bool(job.params.get("no_llm", False)),
                analyze_visual=bool(job.params.get("analyze_visual", False)),
                num_speakers=int(job.params.get("num_speakers", 0)),
                on_progress=progress,
            )
            md_path = store.get_meeting_md_path(meeting_id)
            with self._lock:
                job.meeting_id = meeting_id
                job.result_path = md_path
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
