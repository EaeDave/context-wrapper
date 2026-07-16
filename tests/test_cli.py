"""CLI entrypoints — smoke + edge paths without GPU/pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from meet.cli import _fmt_duration, app


runner = CliRunner()


def test_fmt_duration() -> None:
    assert _fmt_duration(0) == "0:00"
    assert _fmt_duration(65) == "0:01"
    assert _fmt_duration(3661) == "1:01"


def test_cli_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "process" in result.stdout.lower() or "Pipeline" in result.stdout


def test_process_missing_file_exits_1(tmp_path: Path) -> None:
    missing = tmp_path / "nope.mkv"
    result = runner.invoke(app, ["process", str(missing)])
    assert result.exit_code == 1
    assert "não encontrado" in (result.stdout + result.stderr).lower() or "Erro" in (
        result.stdout + result.stderr
    )


def test_speakers_list_empty(tmp_path: Path, monkeypatch) -> None:
    from meet.config import Settings
    from meet.store import Store

    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data, output_dir=tmp_path / "out")
    store = Store(settings.db_path)

    monkeypatch.setattr("meet.cli._load_store", lambda: (settings, store))
    result = runner.invoke(app, ["speakers", "list"])
    assert result.exit_code == 0


def test_serve_missing_uvicorn_exits_1() -> None:
    with patch.dict("sys.modules", {"uvicorn": None}):
        # Force ImportError on import uvicorn inside serve
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "uvicorn":
                raise ImportError("no uvicorn")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            result = runner.invoke(app, ["serve", "--no-open"])
    assert result.exit_code == 1


def test_speakers_rename_and_rm(tmp_path: Path, monkeypatch) -> None:
    from meet.config import Settings
    from meet.store import Store

    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data, output_dir=tmp_path / "out")
    store = Store(settings.db_path)
    store.upsert_voice("Alice", b"\x00\x01\x02\x03")
    monkeypatch.setattr("meet.cli._load_store", lambda: (settings, store))

    r1 = runner.invoke(app, ["speakers", "rename", "Alice", "Alicia"])
    assert r1.exit_code == 0
    assert store.get_voice("Alice") is None
    assert store.get_voice("Alicia") == b"\x00\x01\x02\x03"

    r2 = runner.invoke(app, ["speakers", "rm", "Alicia"])
    assert r2.exit_code == 0
    assert store.get_voice("Alicia") is None

    r3 = runner.invoke(app, ["speakers", "rm", "Ghost"])
    assert r3.exit_code == 1


def test_speakers_assign_missing_pending_exits_1(tmp_path: Path, monkeypatch) -> None:
    from meet.config import Settings
    from meet.models import MeetingResult, TranscriptSegment
    from meet.store import Store

    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data, output_dir=tmp_path / "out")
    store = Store(settings.db_path)
    mid = store.save_meeting(
        MeetingResult(
            source="x.mkv",
            date="2024-01-01",
            title="t",
            duration=1.0,
            segments=[
                TranscriptSegment(start=0, end=1, text="hi", speaker="SPEAKER_00"),
            ],
        ),
        tmp_path / "m.md",
    )
    monkeypatch.setattr("meet.cli._load_store", lambda: (settings, store))
    result = runner.invoke(
        app, ["speakers", "assign", str(mid), "SPEAKER_00", "Alice"]
    )
    assert result.exit_code == 1
