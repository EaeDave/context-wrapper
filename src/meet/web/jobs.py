"""Fila simples de jobs em background (um worker, single-user local)."""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


@dataclass
class Job:
    id: str
    kind: str  # "process" | "mix"
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


class JobManager:
    """Fila FIFO com um thread worker — GPU Whisper não paraleliza bem."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._worker = threading.Thread(target=self._loop, name="meet-jobs", daemon=True)
        self._worker.start()

    def submit(self, kind: str, label: str, **params: Any) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, label=label, params=params)
        with self._cv:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._cv.notify()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i] for i in ids]

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

            try:
                self._run(job)
                with self._lock:
                    job.status = JobStatus.done
                    job.finished_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
            except Exception as exc:
                with self._lock:
                    job.status = JobStatus.error
                    job.error = f"{exc}\n{traceback.format_exc()[-800:]}"
                    job.stage = "Falhou"
                    job.finished_at = datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )

    def _run(self, job: Job) -> None:
        from ..config import load_settings
        from ..store import Store

        settings = load_settings()
        store = Store(settings.db_path)

        def progress(msg: str) -> None:
            with self._lock:
                job.stage = msg

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
