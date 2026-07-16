"""model_catalog — discovery/fallback paths without live network when mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from meet.config import Settings
from meet.model_catalog import (
    CODEX_FALLBACK_MODEL,
    _merge_models,
    get_model_catalog,
)


def _settings(tmp_path: Path, **kwargs) -> Settings:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Settings(data_dir=data, output_dir=tmp_path / "out", **kwargs)


def test_merge_models_discovered_wins_and_appends() -> None:
    base = [{"id": "a", "name": "A", "recommended": True}]
    disc = [
        {"id": "a", "name": "A-new", "recommended": False},
        {"id": "b", "name": "B", "recommended": True},
    ]
    merged = _merge_models(base, disc)
    assert [m["id"] for m in merged] == ["a", "b"]
    assert merged[0]["name"] == "A-new"


def test_get_model_catalog_invalid_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inválido"):
        get_model_catalog(_settings(tmp_path), "nope")


def test_get_model_catalog_anthropic_bundled(tmp_path: Path) -> None:
    cat = get_model_catalog(_settings(tmp_path), "anthropic")
    assert cat["source"] == "bundled"
    assert cat["stale"] is False
    assert any(m["id"] == "claude-sonnet-5" for m in cat["models"])
    rec = [m for m in cat["models"] if m["recommended"]]
    assert len(rec) == 1
    assert rec[0]["id"] == cat["default_model"]


def test_get_model_catalog_openai_discovery_error_stale_bundled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with (
        patch("meet.openai_oauth.load_tokens", return_value={"access": "x", "refresh": "r"}),
        patch("meet.openai_oauth.get_access_token", side_effect=RuntimeError("down")),
    ):
        cat = get_model_catalog(settings, "openai")
    assert cat["stale"] is True
    assert cat["source"] == "bundled"
    assert cat["warning"]
    assert any(m["id"] == CODEX_FALLBACK_MODEL for m in cat["models"])


def test_get_model_catalog_ollama_discovery(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ollama_url="http://ollama.test:11434")
    discovered = [
        {"id": "llama3:8b", "name": "llama3:8b", "recommended": False},
        {"id": "qwen3:14b", "name": "qwen3:14b", "recommended": True},
    ]
    with patch("meet.model_catalog._fetch_ollama_models", return_value=discovered):
        cat = get_model_catalog(settings, "ollama")
    assert cat["source"] == "provider"
    ids = {m["id"] for m in cat["models"]}
    assert "llama3:8b" in ids
    assert "qwen3:14b" in ids
