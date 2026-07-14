"""Testes da persistência SQLite do JobManager (Tier 3 — seção A).

Contratos defendidos:
- submit persiste: nova JobManager sobre mesmo db_path vê o job.
- startup recovery: job status='running' na tabela → marcado error "Interrompido…".
- db inacessível: manager funciona em memória sem levantar.
"""

from __future__ import annotations

from dataclasses import replace
import sqlite3
import time
from pathlib import Path

import pytest

from meet.progress import ProgressStep, ProgressUpdate
from meet.web.app import _serialize_job
from meet.web.jobs import Job, JobManager, JobStatus


# ── helpers ──────────────────────────────────────────────────────────────────


def _wait_for_status(mgr: JobManager, job_id: str, timeout: float = 3.0) -> str:
    """Aguarda o job sair do status queued/running (processado pelo worker)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = mgr.get(job_id)
        if job and job.status not in (JobStatus.queued, JobStatus.running):
            return job.status.value
        time.sleep(0.05)
    job = mgr.get(job_id)
    return job.status.value if job else "?"


# ── testes ───────────────────────────────────────────────────────────────────


def test_submit_persiste(tmp_path: Path) -> None:
    """Job submetido deve aparecer em nova instância sobre o mesmo db."""
    db = tmp_path / "meet.db"
    mgr1 = JobManager(db_path=db)
    job = mgr1.submit("fake_kind", "Teste")

    # Worker vai processar o job (kind inválido → error rápido); aguardar.
    _wait_for_status(mgr1, job.id)

    # Nova instância lê do disco.
    mgr2 = JobManager(db_path=db)
    loaded = mgr2.get(job.id)
    assert loaded is not None, "Job deve ser carregado do SQLite"
    assert loaded.id == job.id
    assert loaded.label == "Teste"
    assert loaded.kind == "fake_kind"


def test_submit_persiste_imediatamente(tmp_path: Path) -> None:
    """Job deve aparecer no SQLite logo após submit (sem esperar worker)."""
    db = tmp_path / "meet.db"
    mgr = JobManager(db_path=db)
    job = mgr.submit("fake_kind", "Imediato")

    # Ler diretamente do SQLite sem passar pelo JobManager.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    conn.close()

    assert row is not None, "Job deve existir no SQLite logo após submit"
    assert row["label"] == "Imediato"


def test_recovery_running_marcado_error(tmp_path: Path) -> None:
    """Job com status 'running' no restart deve ser marcado como error."""
    db = tmp_path / "meet.db"

    # Inserir manualmente um job preso em 'running'.
    conn = sqlite3.connect(str(db))
    conn.execute("""
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
        )
    """)
    conn.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("abc123", "process", "Preso", "running", "Transcrevendo…",
         None, None, None, "2024-01-01T10:00:00", None, "{}"),
    )
    conn.commit()
    conn.close()

    # Novo manager: deve recuperar o job e marcá-lo como error.
    mgr = JobManager(db_path=db)
    job = mgr.get("abc123")
    assert job is not None
    assert job.status == JobStatus.error
    assert "Interrompido" in (job.error or "")

    # Persistência deve refletir o error no SQLite.
    conn2 = sqlite3.connect(str(db))
    conn2.row_factory = sqlite3.Row
    row = conn2.execute("SELECT status, error FROM jobs WHERE id = 'abc123'").fetchone()
    conn2.close()
    assert row["status"] == "error"
    assert "Interrompido" in (row["error"] or "")


def test_recovery_queued_marcado_error(tmp_path: Path) -> None:
    """Job com status 'queued' no restart deve também ser marcado como error."""
    db = tmp_path / "meet.db"

    conn = sqlite3.connect(str(db))
    conn.execute("""
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
        )
    """)
    conn.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("xyz789", "process", "Na fila", "queued", "Na fila…",
         None, None, None, "2024-01-01T09:00:00", None, "{}"),
    )
    conn.commit()
    conn.close()

    mgr = JobManager(db_path=db)
    job = mgr.get("xyz789")
    assert job is not None
    assert job.status == JobStatus.error


def test_db_inacessivel_degrada_sem_levantar(tmp_path: Path) -> None:
    """Path inválido para o SQLite não deve impedir o JobManager de funcionar."""
    bad_path = tmp_path / "nao_existe" / "meet.db"  # diretório pai não existe

    # Não deve levantar exceção.
    mgr = JobManager(db_path=bad_path)
    assert mgr._conn is None  # sem conexão — modo memória

    # submit e get devem funcionar normalmente.
    job = mgr.submit("fake_kind", "Memória")
    found = mgr.get(job.id)
    assert found is not None
    assert found.label == "Memória"


def test_list_recent_order(tmp_path: Path) -> None:
    """list_recent retorna jobs em ordem inversa (mais recente primeiro)."""
    db = tmp_path / "meet.db"
    mgr = JobManager(db_path=db)

    j1 = mgr.submit("fake_kind", "Primeiro")
    j2 = mgr.submit("fake_kind", "Segundo")
    j3 = mgr.submit("fake_kind", "Terceiro")

    recent = mgr.list_recent(3)
    assert recent[0].id == j3.id
    assert recent[1].id == j2.id
    assert recent[2].id == j1.id


def test_done_jobs_carregados_na_recovery(tmp_path: Path) -> None:
    """Jobs done/error da sessão anterior devem aparecer na nova instância."""
    db = tmp_path / "meet.db"

    conn = sqlite3.connect(str(db))
    conn.execute("""
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
        )
    """)
    conn.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("done01", "process", "Concluído", "done", "Pronto",
         None, 42, None, "2024-01-01T08:00:00", "2024-01-01T08:30:00", '{"v":1}'),
    )
    conn.commit()
    conn.close()

    mgr = JobManager(db_path=db)
    job = mgr.get("done01")
    assert job is not None
    assert job.status == JobStatus.done
    assert job.meeting_id == 42
    assert job.params == {"v": 1}


def _progress(percent: float = 37.5) -> ProgressUpdate:
    return ProgressUpdate(
        percent=percent,
        step="transcribe",
        step_label="Transcrição",
        step_percent=50.0,
        detail="Transcrevendo áudio",
        elapsed_seconds=12.5,
        steps=(
            ProgressStep("prepare", "Preparação", "done", 2.0),
            ProgressStep("transcribe", "Transcrição", "running", 10.5),
            ProgressStep("save", "Salvamento", "pending"),
        ),
    )


def test_progress_round_trip_e_json_publico(tmp_path: Path) -> None:
    db = tmp_path / "meet.db"
    mgr = JobManager(db_path=db)
    original = Job(
        id="progress01",
        kind="process",
        label="Com progresso",
        status=JobStatus.done,
        progress=_progress(),
    )
    mgr._persist(original)

    loaded = JobManager(db_path=db).get(original.id)
    assert loaded is not None
    assert loaded.progress == original.progress
    assert _serialize_job(loaded)["progress"] == {
        "percent": 37.5,
        "step": "transcribe",
        "step_label": "Transcrição",
        "step_percent": 50.0,
        "detail": "Transcrevendo áudio",
        "elapsed_seconds": 12.5,
        "steps": [
            {"key": "prepare", "label": "Preparação", "state": "done", "elapsed_seconds": 2.0},
            {"key": "transcribe", "label": "Transcrição", "state": "running", "elapsed_seconds": 10.5},
            {"key": "save", "label": "Salvamento", "state": "pending", "elapsed_seconds": None},
        ],
    }


def test_migracao_legada_adiciona_progress_nulo(tmp_path: Path) -> None:
    db = tmp_path / "meet.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT,
          meeting_id INTEGER, result_path TEXT, created_at TEXT NOT NULL,
          finished_at TEXT, params TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("legacy01", "process", "Legado", "done", "Pronto", None, None,
         None, "2024-01-01T08:00:00", "2024-01-01T08:30:00", "{}"),
    )
    conn.commit()
    conn.close()

    loaded = JobManager(db_path=db).get("legacy01")
    assert loaded is not None
    assert loaded.progress is None
    assert _serialize_job(loaded)["progress"] is None
    conn = sqlite3.connect(str(db))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()
    assert "progress_json" in columns


def test_recovery_marca_etapa_atual_como_error(tmp_path: Path) -> None:
    db = tmp_path / "meet.db"
    mgr = JobManager(db_path=db)
    interrupted = Job(
        id="running01",
        kind="process",
        label="Interrompido",
        status=JobStatus.running,
        progress=_progress(),
    )
    mgr._persist(interrupted)

    loaded = JobManager(db_path=db).get(interrupted.id)
    assert loaded is not None
    assert loaded.status == JobStatus.error
    assert loaded.progress is not None
    current = next(
        step for step in loaded.progress.steps if step.key == loaded.progress.step
    )
    assert current.state == "error"
    assert all(step.state != "running" for step in loaded.progress.steps)


@pytest.mark.parametrize("fails", [False, True])
def test_progress_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails: bool) -> None:
    def run(self: JobManager, job: Job) -> None:
        job.progress = _progress(80.0)
        if fails:
            raise RuntimeError("quebrou")

    monkeypatch.setattr(JobManager, "_run", run)
    mgr = JobManager(db_path=tmp_path / "meet.db")
    job = mgr.submit("process", "Terminal")
    _wait_for_status(mgr, job.id)

    assert job.progress is not None
    if fails:
        current = next(step for step in job.progress.steps if step.key == job.progress.step)
        assert job.status == JobStatus.error
        assert current.state == "error"
        assert all(step.state != "running" for step in job.progress.steps)
    else:
        assert job.status == JobStatus.done
        assert job.progress.percent == 100.0
        assert all(step.state == "done" for step in job.progress.steps)


def test_job_concluido_preserva_erro_nao_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run(self: JobManager, job: Job) -> None:
        progress = _progress(100.0)
        job.progress = replace(
            progress,
            step="save",
            step_label="Salvamento",
            step_percent=100.0,
            detail="Mídia não importada: disco cheio",
            steps=(
                replace(progress.steps[0], state="done"),
                replace(progress.steps[1], state="done"),
                ProgressStep("import", "Importação", "error", 0.1),
            ),
        )

    monkeypatch.setattr(JobManager, "_run", run)
    mgr = JobManager(db_path=tmp_path / "meet.db")
    job = mgr.submit("process", "Sucesso parcial")
    _wait_for_status(mgr, job.id)

    assert job.status == JobStatus.done
    assert job.progress is not None
    assert job.progress.percent == 100.0
    assert [step.state for step in job.progress.steps] == ["done", "done", "error"]
    assert job.progress.detail == "Mídia não importada: disco cheio"
