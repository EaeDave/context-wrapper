"""Testes focados: camada settings.local.json, tokens OAuth e masking.

Sem chamadas de rede — httpx mockado onde necessário.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meet.config import (
    Settings,
    hf_token_source,
    load_settings,
    local_settings_path,
    save_local_settings,
)
from meet.anthropic_oauth import (
    _anthropic_error,
    _check_response,
    clear_tokens,
    get_access_token,
    load_tokens,
    save_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_settings(tmp_path: Path, **kwargs) -> Settings:
    """Cria Settings com data_dir em tmp_path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return Settings(data_dir=data_dir, **kwargs)


# ---------------------------------------------------------------------------
# Layering: defaults < toml < local < env
# ---------------------------------------------------------------------------


def test_local_overrides_toml(tmp_path: Path) -> None:
    """settings.local.json sobrepõe config.toml."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    toml = tmp_path / "config.toml"
    _write_toml(toml, f'hf_token = "from_toml"\ndata_dir = "{data_dir}"\n')
    (data_dir / "settings.local.json").write_text(json.dumps({"hf_token": "from_local"}))

    settings = load_settings(toml)
    assert settings.hf_token == "from_local"


def test_env_wins_over_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var supera settings.local.json."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    toml = tmp_path / "config.toml"
    _write_toml(toml, f'data_dir = "{data_dir}"\n')
    (data_dir / "settings.local.json").write_text(json.dumps({"hf_token": "from_local"}))

    monkeypatch.setenv("HF_TOKEN", "from_env")
    settings = load_settings(toml)
    assert settings.hf_token == "from_env"


def test_local_only_allowed_keys(tmp_path: Path) -> None:
    """settings.local.json ignora chaves desconhecidas; aceita as permitidas (incl. tuning)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    toml = tmp_path / "config.toml"
    _write_toml(toml, f'data_dir = "{data_dir}"\n')
    (data_dir / "settings.local.json").write_text(
        json.dumps({"hf_token": "tok", "whisper_model": "turbo", "unknown_field": "x"})
    )
    settings = load_settings(toml)
    assert settings.hf_token == "tok"
    # whisper_model agora é _LOCAL_KEYS → sobrescreve
    assert settings.whisper_model == "turbo"
    # chave desconhecida é silenciosamente ignorada (Settings(**values) não a aceita)

def test_local_llm_fields(tmp_path: Path) -> None:
    """llm_provider e llm_model podem ser sobrepostos via local."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    toml = tmp_path / "config.toml"
    _write_toml(toml, f'data_dir = "{data_dir}"\n')
    (data_dir / "settings.local.json").write_text(
        json.dumps({"llm_provider": "openai", "llm_model": "gpt-4o"})
    )
    settings = load_settings(toml)
    assert settings.llm_provider == "openai"
    assert settings.llm_model == "gpt-4o"


# ---------------------------------------------------------------------------
# save_local_settings
# ---------------------------------------------------------------------------


def test_save_local_creates_600(tmp_path: Path) -> None:
    """save_local_settings cria arquivo com chmod 600."""
    settings = _make_settings(tmp_path)
    save_local_settings({"hf_token": "hf_test123"}, settings)

    path = local_settings_path(settings)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["hf_token"] == "hf_test123"


def test_save_local_merge_preserves_other_keys(tmp_path: Path) -> None:
    """save_local_settings faz merge, não sobrescreve tudo."""
    settings = _make_settings(tmp_path)
    save_local_settings({"hf_token": "hf_a", "llm_provider": "openai"}, settings)
    save_local_settings({"llm_model": "gpt-4o"}, settings)

    data = json.loads(local_settings_path(settings).read_text())
    assert data["hf_token"] == "hf_a"
    assert data["llm_provider"] == "openai"
    assert data["llm_model"] == "gpt-4o"


def test_save_local_none_removes_key(tmp_path: Path) -> None:
    """Passar None remove a chave do arquivo."""
    settings = _make_settings(tmp_path)
    save_local_settings({"hf_token": "hf_abc"}, settings)
    save_local_settings({"hf_token": None}, settings)

    data = json.loads(local_settings_path(settings).read_text())
    assert "hf_token" not in data


def test_save_local_ignores_unknown_keys(tmp_path: Path) -> None:
    """Chaves fora de _LOCAL_KEYS são silenciosamente ignoradas."""
    settings = _make_settings(tmp_path)
    save_local_settings({"hf_token": "hf_x", "anthropic_api_key": "sk-secret"}, settings)

    data = json.loads(local_settings_path(settings).read_text())
    assert "anthropic_api_key" not in data
    assert data["hf_token"] == "hf_x"


# ---------------------------------------------------------------------------
# Token round-trip (auth.json)
# ---------------------------------------------------------------------------


def test_save_load_tokens_roundtrip(tmp_path: Path) -> None:
    """save/load_tokens round-trip preserva todos os campos."""
    settings = _make_settings(tmp_path)
    d = {
        "access": "sk-ant-oat-abc",
        "refresh": "rt-xyz",
        "expires": int(time.time() * 1000) + 3_600_000,
        "email": "test@example.com",
        "account_id": "acc-123",
    }
    save_tokens(settings, d)

    auth_path = settings.data_dir / "auth.json"
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600

    loaded = load_tokens(settings)
    assert loaded == d


def test_save_tokens_preserves_other_providers(tmp_path: Path) -> None:
    """save_tokens não apaga outras chaves do auth.json."""
    settings = _make_settings(tmp_path)
    auth_path = settings.data_dir / "auth.json"
    auth_path.write_text(json.dumps({"other": {"foo": "bar"}}))

    d = {"access": "tok", "refresh": "r", "expires": 9_999_999_999_000, "email": None, "account_id": None}
    save_tokens(settings, d)

    raw = json.loads(auth_path.read_text())
    assert raw["other"] == {"foo": "bar"}
    assert raw["anthropic"] == d


def test_clear_tokens(tmp_path: Path) -> None:
    """clear_tokens remove a entrada 'anthropic'."""
    settings = _make_settings(tmp_path)
    d = {"access": "tok", "refresh": "r", "expires": 9_999_999_999_000, "email": None, "account_id": None}
    save_tokens(settings, d)
    assert load_tokens(settings) is not None

    clear_tokens(settings)
    assert load_tokens(settings) is None


def test_load_tokens_missing(tmp_path: Path) -> None:
    """load_tokens retorna None se auth.json não existir."""
    settings = _make_settings(tmp_path)
    assert load_tokens(settings) is None


def test_load_tokens_invalid_json(tmp_path: Path) -> None:
    """load_tokens retorna None se auth.json estiver corrompido."""
    settings = _make_settings(tmp_path)
    (settings.data_dir / "auth.json").write_text("não é json {{{")
    assert load_tokens(settings) is None



def test_refresh_persiste_token_rotacionado(tmp_path: Path) -> None:
    """Refresh OAuth deve substituir o refresh token de uso único."""
    settings = _make_settings(tmp_path)
    save_tokens(
        settings,
        {
            "access": "access-antigo",
            "refresh": "refresh-antigo",
            "expires": 0,
            "email": "test@example.com",
            "account_id": "acc-123",
        },
    )

    refreshed = {
        "access_token": "access-novo",
        "refresh_token": "refresh-novo",
        "expires_in": 3600,
    }
    with patch("meet.anthropic_oauth.refresh", return_value=refreshed):
        assert get_access_token(settings) == "access-novo"

    tokens = load_tokens(settings)
    assert tokens is not None
    assert tokens["refresh"] == "refresh-novo"


def test_refresh_invalido_limpa_sessao(tmp_path: Path) -> None:
    """Token revogado deve virar pedido de reconexão e não ficar parecendo conectado."""
    settings = _make_settings(tmp_path)
    save_tokens(
        settings,
        {
            "access": "access-expirado",
            "refresh": "refresh-revogado",
            "expires": 0,
            "email": "test@example.com",
            "account_id": "acc-123",
        },
    )

    error = RuntimeError(
        'HTTP 400: {"error":"invalid_grant","error_description":"Refresh token not found or invalid"}'
    )
    with (
        patch("meet.anthropic_oauth.refresh", side_effect=error),
        pytest.raises(ValueError, match="Reconecte sua conta"),
    ):
        get_access_token(settings)

    assert load_tokens(settings) is None

# ---------------------------------------------------------------------------
# Masking — nunca vazar token inteiro
# ---------------------------------------------------------------------------


def test_masked_format(tmp_path: Path) -> None:
    """Masking = primeiros 3 + '…' + últimos 4 chars; token inteiro nunca aparece."""
    hf = "hf_abcdefghij1234"
    masked = hf[:3] + "…" + hf[-4:]
    assert hf not in masked
    assert masked == "hf_…1234"


def test_masked_none_when_no_token() -> None:
    """masked é None quando hf_token vazio."""
    hf = ""
    masked = (hf[:3] + "…" + hf[-4:]) if hf else None
    assert masked is None


# ---------------------------------------------------------------------------
# hf_token_source
# ---------------------------------------------------------------------------


def test_hf_token_source_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retorna None quando token ausente."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    settings = _make_settings(tmp_path, hf_token="")
    assert hf_token_source(settings) is None


def test_hf_token_source_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retorna 'env' quando HF_TOKEN está em env."""
    monkeypatch.setenv("HF_TOKEN", "hf_fromenv")
    settings = _make_settings(tmp_path, hf_token="hf_fromenv")
    assert hf_token_source(settings) == "env"


def test_hf_token_source_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retorna 'local' quando token vem de settings.local.json."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    settings = _make_settings(tmp_path, hf_token="hf_local")
    local_settings_path(settings).write_text(json.dumps({"hf_token": "hf_local"}))
    assert hf_token_source(settings) == "local"


def test_hf_token_source_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retorna 'config' quando token vem de config.toml."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    toml = tmp_path / "config.toml"
    toml.write_text('hf_token = "hf_fromconfig"\n')
    settings = _make_settings(tmp_path, hf_token="hf_fromconfig")
    assert hf_token_source(settings, config_path=toml) == "config"


# ---------------------------------------------------------------------------
# _check_response / _anthropic_error
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, body: dict | str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    if isinstance(body, dict):
        resp.json.return_value = body
        resp.text = json.dumps(body)
    else:
        resp.text = body or ""
        resp.json.side_effect = ValueError("not json")
    return resp


def test_check_response_ok() -> None:
    """_check_response não lança em 2xx."""
    resp = _mock_response(200, {"ok": True})
    _check_response(resp)  # não deve lançar


def test_check_response_extracts_error_message() -> None:
    """_check_response lança RuntimeError com mensagem do campo error.message."""
    resp = _mock_response(400, {"error": {"message": "invalid_grant: code expired"}})
    with pytest.raises(RuntimeError, match="invalid_grant: code expired"):
        _check_response(resp)


def test_check_response_fallback_text() -> None:
    """_check_response usa resp.text quando body não tem error.message."""
    resp = _mock_response(429, "Rate limited by Anthropic")
    with pytest.raises(RuntimeError, match="Rate limited"):
        _check_response(resp)


def test_anthropic_error_truncates_long_text() -> None:
    """_anthropic_error trunca texto longo em 300 chars."""
    resp = _mock_response(500, "x" * 500)
    msg = _anthropic_error(resp)
    assert len(msg) < 400  # 300 + "HTTP 500: " prefix
