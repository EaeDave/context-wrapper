"""Lock compartilhado de auth.json entre providers."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meet.auth_store import clear_provider, load_provider, save_provider
from meet.config import Settings
from meet import anthropic_oauth as ant
from meet import openai_oauth as oai


def test_concurrent_saves_preserve_both_providers(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", output_dir=tmp_path / "out")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "auth.json"
    errors: list[BaseException] = []

    def write_ant() -> None:
        try:
            for i in range(40):
                save_provider(path, "anthropic", {"access": f"a{i}", "n": i})
        except BaseException as exc:  # noqa: BLE001 — coletar em thread
            errors.append(exc)

    def write_oai() -> None:
        try:
            for i in range(40):
                save_provider(path, "openai", {"access": f"o{i}", "n": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=write_ant)
    t2 = threading.Thread(target=write_oai)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors, errors

    raw = json.loads(path.read_text())
    assert "anthropic" in raw
    assert "openai" in raw
    assert load_provider(path, "anthropic") is not None
    assert load_provider(path, "openai") is not None


def test_exclusive_covers_refresh_section_without_deadlock(tmp_path: Path) -> None:
    """exclusive + save_provider_unlocked na mesma thread (caminho de refresh)."""
    from meet.auth_store import exclusive, load_provider_unlocked, save_provider_unlocked

    path = tmp_path / "auth.json"
    with exclusive(path):
        save_provider_unlocked(path, "anthropic", {"access": "a", "refresh": "r"})
        save_provider_unlocked(path, "openai", {"access": "o", "refresh": "r2"})
        assert load_provider_unlocked(path, "anthropic")["access"] == "a"
        assert load_provider_unlocked(path, "openai")["access"] == "o"


def test_oauth_modules_do_not_clobber_each_other(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", output_dir=tmp_path / "out")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    ant.save_tokens(settings, {"access": "ant", "refresh": "r", "expires": 1})
    oai.save_tokens(settings, {"access": "oai", "refresh": "r", "expires": 1})
    assert ant.load_tokens(settings)["access"] == "ant"
    assert oai.load_tokens(settings)["access"] == "oai"
    ant.clear_tokens(settings)
    assert ant.load_tokens(settings) is None
    assert oai.load_tokens(settings)["access"] == "oai"
    clear_provider(settings.data_dir / "auth.json", "openai")
    assert oai.load_tokens(settings) is None


def test_anthropic_invalid_grant_does_not_clear_rotated_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loser of refresh race must not wipe winner's rotated tokens."""
    import time

    from meet.auth_store import save_provider_unlocked

    settings = Settings(data_dir=tmp_path / "data", output_dir=tmp_path / "out")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "auth.json"
    ant.save_tokens(
        settings,
        {
            "access": "old-access",
            "refresh": "rt-old",
            "expires": 0,  # force refresh path
            "email": None,
            "account_id": None,
        },
    )

    calls = {"n": 0}

    def fake_refresh(rt: str) -> dict:
        calls["n"] += 1
        # Simula vencedor que já gravou (unlocked: get_access_token segura exclusive).
        save_provider_unlocked(
            path,
            "anthropic",
            {
                "access": "winner-access",
                "refresh": "rt-new",
                "expires": int(time.time() * 1000) + 3_600_000,
                "email": "w@x",
                "account_id": "id",
            },
        )
        raise RuntimeError('HTTP 400: {"error":"invalid_grant"}')

    monkeypatch.setattr(ant, "refresh", fake_refresh)
    token = ant.get_access_token(settings)
    assert token == "winner-access"
    assert ant.load_tokens(settings)["refresh"] == "rt-new"
    assert calls["n"] == 1
