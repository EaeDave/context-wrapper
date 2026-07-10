"""Testes da persistência SQLite do JobManager (Tier 3 — seção A).

Contratos defendidos:
- submit persiste: nova JobManager sobre mesmo db_path vê o job.
- startup recovery: job status='running' na tabela → marcado error "Interrompido…".
- db inacessível: manager funciona em memória sem levantar.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from meet.web.jobs import JobManager, JobStatus


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
