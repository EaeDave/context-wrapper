"""Testes focados: openai_oauth.py e endpoints web OpenAI.

Sem rede real — httpx mockado onde necessário.
"""

from __future__ import annotations

import base64
import json
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meet.config import Settings
from meet.openai_oauth import (
    _NS_AUTH,
    _NS_PROFILE,
    _DEVICE_PAGE,
    _decode_jwt_claims,
    _extract_identity,
    _is_pending,
    _openai_error,
    build_stored_tokens,
    clear_tokens,
    exchange_device_code,
    get_access_token,
    load_tokens,
    poll_device_token,
    refresh,
    request_device_code,
    save_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path, **kwargs) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=data_dir, **kwargs)


def _mock_resp(status_code: int, body: dict | str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    if isinstance(body, dict):
        resp.json.return_value = body
        resp.text = json.dumps(body)
    else:
        resp.json.side_effect = ValueError("not json")
        resp.text = body or ""
    return resp


def _make_jwt(payload: dict) -> str:
    """Cria JWT mínimo (sem assinatura válida) para testes."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    body_bytes = json.dumps(payload).encode()
    body = base64.urlsafe_b64encode(body_bytes).rstrip(b"=").decode()
    return f"{header}.{body}.fakesig"


def _make_oai_jwt(email: str = "user@example.com", account_id: str = "acct-1",
                  plan: str = "plus", exp: int | None = None) -> str:
    """JWT com claims aninhadas no formato real da OpenAI."""
    payload: dict = {
        _NS_PROFILE: {"email": email},
        _NS_AUTH: {"chatgpt_account_id": account_id, "chatgpt_plan_type": plan},
    }
    if exp is not None:
        payload["exp"] = exp
    return _make_jwt(payload)


# ---------------------------------------------------------------------------
# _decode_jwt_claims
# ---------------------------------------------------------------------------


def test_decode_jwt_claims_extracts_nested_namespaces() -> None:
    """Extrai claims aninhados com namespace URI da OpenAI (objetos, não strings dotadas)."""
    payload = {
        _NS_PROFILE: {"email": "user@example.com"},
        _NS_AUTH: {"chatgpt_account_id": "acct-abc", "chatgpt_plan_type": "plus"},
        "exp": 9999999999,
    }
    token = _make_jwt(payload)
    claims = _decode_jwt_claims(token)
    assert claims[_NS_PROFILE] == {"email": "user@example.com"}
    assert claims[_NS_AUTH]["chatgpt_account_id"] == "acct-abc"
    assert claims[_NS_AUTH]["chatgpt_plan_type"] == "plus"
    assert claims["exp"] == 9999999999


def test_decode_jwt_claims_invalid_returns_empty() -> None:
    """Retorna {} para token malformado."""
    assert _decode_jwt_claims("not.a.jwt") == {}
    assert _decode_jwt_claims("") == {}
    assert _decode_jwt_claims("only_one_part") == {}


def test_decode_jwt_claims_bad_base64_returns_empty() -> None:
    """Retorna {} se o payload não é JSON válido."""
    assert _decode_jwt_claims("header.!!!.sig") == {}


# ---------------------------------------------------------------------------
# _extract_identity
# ---------------------------------------------------------------------------


def test_extract_identity_nested_claims() -> None:
    """_extract_identity lê profile.email e auth.chatgpt_* corretamente."""
    claims = {
        _NS_PROFILE: {"email": "alice@openai.com"},
        _NS_AUTH: {"chatgpt_account_id": "acct-999", "chatgpt_plan_type": "plus"},
    }
    email, account_id, plan = _extract_identity(claims)
    assert email == "alice@openai.com"
    assert account_id == "acct-999"
    assert plan == "plus"


def test_extract_identity_never_uses_sub() -> None:
    """account_id nunca vem de 'sub', apenas de NS_AUTH."""
    claims = {"sub": "sub-value-should-be-ignored"}
    _, account_id, _ = _extract_identity(claims)
    assert account_id is None


def test_extract_identity_accepts_top_level_email() -> None:
    """O ID token Codex também pode trazer email no topo."""
    assert _extract_identity({"email": "direct@example.com"}) == (
        "direct@example.com", None, None
    )


def test_extract_identity_non_dict_namespaces_safe() -> None:
    """Namespace com valor não-dict não levanta exceção."""
    claims = {_NS_PROFILE: "not-a-dict", _NS_AUTH: 42}
    email, account_id, plan = _extract_identity(claims)
    assert email is None
    assert account_id is None
    assert plan is None


# ---------------------------------------------------------------------------
# _openai_error
# ---------------------------------------------------------------------------


def test_openai_error_flat_error_string() -> None:
    """Extrai error + error_description do formato flat."""
    resp = _mock_resp(400, {"error": "invalid_grant", "error_description": "Token revoked"})
    msg = _openai_error(resp)
    assert "invalid_grant" in msg
    assert "Token revoked" in msg
    assert "400" in msg


def test_openai_error_nested_error_dict() -> None:
    """Extrai message do formato nested {error: {message: ...}}."""
    resp = _mock_resp(401, {"error": {"message": "Unauthorized access"}})
    msg = _openai_error(resp)
    assert "Unauthorized access" in msg


def test_openai_error_fallback_text() -> None:
    """Usa resp.text quando body não é JSON utilizável."""
    resp = _mock_resp(500, None)
    resp.text = "Internal Server Error"
    msg = _openai_error(resp)
    assert "500" in msg
    assert "Internal Server Error" in msg


# ---------------------------------------------------------------------------
# _is_pending
# ---------------------------------------------------------------------------


def test_is_pending_403_no_error() -> None:
    """403 sem corpo de erro = pending."""
    assert _is_pending(_mock_resp(403, {})) is True


def test_is_pending_404_pending_error() -> None:
    """404 com error=authorization_pending = pending."""
    assert _is_pending(_mock_resp(404, {"error": "authorization_pending"})) is True


def test_is_pending_403_access_denied_not_pending() -> None:
    """403 com error=access_denied = erro definitivo."""
    assert _is_pending(_mock_resp(403, {"error": "access_denied"})) is False


def test_is_pending_403_expired_token_not_pending() -> None:
    """403 com expired_token = erro definitivo."""
    assert _is_pending(_mock_resp(403, {"error": "expired_token"})) is False


def test_is_pending_400_never_pending() -> None:
    """400 nunca é pending."""
    assert _is_pending(_mock_resp(400, {"error": "bad_request"})) is False


def test_is_pending_200_never_pending() -> None:
    """200 nunca é pending."""
    assert _is_pending(_mock_resp(200, {"authorization_code": "code"})) is False


# ---------------------------------------------------------------------------
# request_device_code
# ---------------------------------------------------------------------------


def test_request_device_code_correct_url_and_payload() -> None:
    """Envia POST para _USERCODE_URL com {client_id} e User-Agent."""
    from meet.openai_oauth import CLIENT_ID, _USER_AGENT, _USERCODE_URL

    fake_response = {
        "device_auth_id": "dauth-123",
        "user_code": "ABCD-1234",
        "verification_uri_complete": "https://auth.openai.com/codex/device?code=ABCD-1234",
        "interval": 5,
        "expires_in": 900,
    }
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["headers"] = kwargs.get("headers", {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["body"] = kwargs.get("json")
            return _mock_resp(200, fake_response)

    with patch("meet.openai_oauth.httpx.Client", FakeClient):
        result = request_device_code()

    assert captured["url"] == _USERCODE_URL
    assert captured["body"]["client_id"] == CLIENT_ID
    assert captured["headers"].get("User-Agent") == _USER_AGENT
    assert result["device_auth_id"] == "dauth-123"
    assert result["user_code"] == "ABCD-1234"


def test_request_device_code_raises_on_error() -> None:
    """Lança RuntimeError em resposta não-2xx."""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kwargs):
            return _mock_resp(400, {"error": "invalid_client"})

    with patch("meet.openai_oauth.httpx.Client", FakeClient):
        with pytest.raises(RuntimeError, match="invalid_client"):
            request_device_code()


# ---------------------------------------------------------------------------
# poll_device_token — client único por chamada
# ---------------------------------------------------------------------------


def test_poll_device_token_single_client_for_loop() -> None:
    """poll_device_token usa um único httpx.Client para toda a sessão de polling."""
    from meet.openai_oauth import _TOKEN_POLL_URL

    success = {"authorization_code": "auth-code-xyz", "code_verifier": "verifier-abc"}
    client_instances: list = []
    post_calls: list = []

    class FakeClient:
        def __init__(self, **kwargs):
            client_instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kwargs):
            post_calls.append(url)
            if len(post_calls) == 1:
                return _mock_resp(403, {})   # pending
            return _mock_resp(200, success)  # success

    with patch("meet.openai_oauth.httpx.Client", FakeClient):
        with patch("meet.openai_oauth.time.sleep"):
            result = poll_device_token("dauth-id", "ABCD-1234", interval=1, timeout=60)

    assert result["authorization_code"] == "auth-code-xyz"
    # Um único Client criado, dois posts nele
    assert len(client_instances) == 1
    assert len(post_calls) == 2
    assert all(url == _TOKEN_POLL_URL for url in post_calls)


def test_poll_device_token_timeout() -> None:
    """Lança TimeoutError quando deadline esgota."""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kwargs):
            return _mock_resp(403, {})

    with patch("meet.openai_oauth.httpx.Client", FakeClient):
        with patch("meet.openai_oauth.time.sleep"):
            with patch("meet.openai_oauth.time.monotonic", side_effect=[0, 0, 999]):
                with pytest.raises(TimeoutError):
                    poll_device_token("dauth-id", "ABCD-1234", interval=1, timeout=1)


def test_poll_device_token_raises_on_terminal_error() -> None:
    """Lança RuntimeError imediatamente em access_denied (não faz retry)."""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kwargs):
            return _mock_resp(403, {"error": "access_denied"})

    with patch("meet.openai_oauth.httpx.Client", FakeClient):
        with pytest.raises(RuntimeError, match="access_denied"):
            poll_device_token("dauth-id", "ABCD-1234", interval=1, timeout=60)


# ---------------------------------------------------------------------------
# exchange_device_code
# ---------------------------------------------------------------------------


def test_exchange_device_code_form_urlencoded() -> None:
    """Envia form-urlencoded com redirect_uri correto; NÃO envia json=."""
    from meet.openai_oauth import CLIENT_ID, _REDIRECT_URI, _TOKEN_EXCHANGE_URL

    token_resp = {
        "access_token": "at-xyz",
        "refresh_token": "rt-xyz",
        "expires_in": 3600,
    }
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["data"] = kwargs.get("data")
            captured["has_json"] = "json" in kwargs
            return _mock_resp(200, token_resp)

    with patch("meet.openai_oauth.httpx.Client", FakeClient):
        result = exchange_device_code("auth-code", "code-verifier")

    assert captured["url"] == _TOKEN_EXCHANGE_URL
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["client_id"] == CLIENT_ID
    assert captured["data"]["code"] == "auth-code"
    assert captured["data"]["code_verifier"] == "code-verifier"
    assert captured["data"]["redirect_uri"] == _REDIRECT_URI
    assert not captured["has_json"]
    assert result["access_token"] == "at-xyz"


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_refresh_sends_json_payload() -> None:
    """Envia JSON com client_id, grant_type, refresh_token."""
    from meet.openai_oauth import CLIENT_ID, _TOKEN_EXCHANGE_URL

    token_resp = {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return _mock_resp(200, token_resp)

    with patch("meet.openai_oauth.httpx.Client", FakeClient):
        result = refresh("old-refresh-token")

    assert captured["url"] == _TOKEN_EXCHANGE_URL
    assert captured["json"]["grant_type"] == "refresh_token"
    assert captured["json"]["client_id"] == CLIENT_ID
    assert captured["json"]["refresh_token"] == "old-refresh-token"
    assert result["access_token"] == "at-new"


# ---------------------------------------------------------------------------
# build_stored_tokens — claim names aninhados, expires, partial refresh
# ---------------------------------------------------------------------------


def test_build_stored_tokens_uses_id_token_identity() -> None:
    """Extrai identidade do id_token e validade do access_token."""
    token_resp = {
        "id_token": _make_oai_jwt("alice@openai.com", "acct-999", "plus"),
        "access_token": _make_jwt({"exp": int(time.time()) + 3600}),
        "refresh_token": "rt-abc",
    }
    result = build_stored_tokens(token_resp)
    assert result["id_token"] == token_resp["id_token"]
    assert result["email"] == "alice@openai.com"
    assert result["account_id"] == "acct-999"
    assert result["plan"] == "plus"
    assert result["access"] == token_resp["access_token"]
    assert result["refresh"] == "rt-abc"


def test_build_stored_tokens_account_id_never_sub() -> None:
    """account_id não vem de 'sub' mesmo quando NS_AUTH ausente."""
    token_resp = {
        "access_token": _make_jwt({"sub": "sub-value"}),
        "refresh_token": "rt",
        "expires_in": 3600,
    }
    result = build_stored_tokens(token_resp)
    assert result["account_id"] is None


def test_build_stored_tokens_expires_from_expires_in() -> None:
    """expires = now + expires_in - 5min, tolerância 2s."""
    now_ms = int(time.time() * 1000)
    token_resp = {
        "access_token": _make_jwt({}),
        "refresh_token": "rt",
        "expires_in": 3600,
    }
    result = build_stored_tokens(token_resp)
    expected = now_ms + 3600 * 1000 - 5 * 60 * 1000
    assert abs(result["expires"] - expected) < 2000


def test_build_stored_tokens_expires_from_jwt_exp() -> None:
    """Usa JWT exp quando expires_in ausente."""
    future_exp = int(time.time()) + 7200
    token_resp = {
        "access_token": _make_jwt({"exp": future_exp}),
        "refresh_token": "rt",
    }
    result = build_stored_tokens(token_resp)
    expected = future_exp * 1000 - 5 * 60 * 1000
    assert abs(result["expires"] - expected) < 2000


def test_build_stored_tokens_exchange_requires_access_and_refresh() -> None:
    """Exchange sem fallback: KeyError se access_token ou refresh_token ausente."""
    with pytest.raises(KeyError):
        build_stored_tokens({"refresh_token": "rt", "expires_in": 3600})
    with pytest.raises(KeyError):
        build_stored_tokens({"access_token": _make_jwt({}), "expires_in": 3600})


def test_build_stored_tokens_partial_refresh_no_new_access() -> None:
    """Refresh parcial: access_token ausente → mantém access anterior (fallback)."""
    old_access = _make_oai_jwt("user@openai.com", "acct-old", "free")
    fallback = {
        "access": old_access,
        "refresh": "rt-old",
        "email": "user@openai.com",
        "account_id": "acct-old",
        "plan": "free",
    }
    # Servidor retorna só refresh_token novo, sem novo access_token
    partial_resp = {"refresh_token": "rt-rotated", "expires_in": 3600}
    result = build_stored_tokens(partial_resp, fallback=fallback)
    assert result["access"] == old_access
    assert result["refresh"] == "rt-rotated"
    assert result["email"] == "user@openai.com"
    assert result["account_id"] == "acct-old"


def test_build_stored_tokens_partial_refresh_new_access_overrides() -> None:
    """Refresh com novo access_token: claims e expiry vêm do novo token."""
    old_access = _make_jwt({})
    new_access = _make_oai_jwt("new@openai.com", "acct-new", "plus")
    fallback = {"access": old_access, "refresh": "rt-old", "email": "old@openai.com"}
    resp = {"access_token": new_access, "refresh_token": "rt-new", "expires_in": 3600}
    result = build_stored_tokens(resp, fallback=fallback)
    assert result["access"] == new_access
    assert result["email"] == "new@openai.com"
    assert result["account_id"] == "acct-new"
    assert result["refresh"] == "rt-new"


def test_build_stored_tokens_fallback_fills_missing_identity() -> None:
    """Refresh: identity ausente no novo token é preenchida pelo fallback."""
    # JWT sem claims OpenAI (ex. resposta mínima)
    new_access = _make_jwt({"exp": int(time.time()) + 3600})
    fallback = {
        "access": "old-at",
        "refresh": "rt-old",
        "email": "bob@example.com",
        "account_id": "acc-old",
        "plan": "free",
    }
    resp = {"access_token": new_access, "refresh_token": "rt-new", "expires_in": 1800}
    result = build_stored_tokens(resp, fallback=fallback)
    assert result["email"] == "bob@example.com"
    assert result["account_id"] == "acc-old"
    assert result["plan"] == "free"


# ---------------------------------------------------------------------------
# Persistência: save_tokens / load_tokens / clear_tokens
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """save/load round-trip preserva todos os campos."""
    settings = _make_settings(tmp_path)
    d = {
        "access": "at",
        "refresh": "rt",
        "expires": 9999999,
        "email": "user@example.com",
        "account_id": "acc-1",
        "plan": "plus",
    }
    save_tokens(settings, d)
    assert load_tokens(settings) == d


def test_save_tokens_chmod_600(tmp_path: Path) -> None:
    """auth.json criado com permissões 0o600."""
    settings = _make_settings(tmp_path)
    save_tokens(settings, {"access": "at", "refresh": "rt", "expires": 0})
    path = settings.data_dir / "auth.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_tokens_preserves_other_providers(tmp_path: Path) -> None:
    """save_tokens não apaga chaves de outros providers."""
    settings = _make_settings(tmp_path)
    path = settings.data_dir / "auth.json"
    path.write_text(json.dumps({"anthropic": {"access": "ant-at"}}))
    path.chmod(0o600)
    save_tokens(settings, {"access": "oai-at", "refresh": "oai-rt", "expires": 0})
    raw = json.loads(path.read_text())
    assert raw["anthropic"]["access"] == "ant-at"
    assert raw["openai"]["access"] == "oai-at"


def test_clear_tokens_removes_openai_preserves_others(tmp_path: Path) -> None:
    """clear_tokens remove openai mas preserva outros providers."""
    settings = _make_settings(tmp_path)
    path = settings.data_dir / "auth.json"
    path.write_text(json.dumps({
        "anthropic": {"access": "ant-at"},
        "openai": {"access": "oai-at", "refresh": "rt", "expires": 0},
    }))
    path.chmod(0o600)
    clear_tokens(settings)
    raw = json.loads(path.read_text())
    assert "openai" not in raw
    assert raw["anthropic"]["access"] == "ant-at"
    assert load_tokens(settings) is None


def test_load_tokens_missing_file(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    assert load_tokens(settings) is None


def test_load_tokens_invalid_json(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    (settings.data_dir / "auth.json").write_text("{{broken")
    assert load_tokens(settings) is None


def test_load_tokens_missing_openai_key(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    (settings.data_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"access": "at"}})
    )
    assert load_tokens(settings) is None


# ---------------------------------------------------------------------------
# get_access_token — válido, refresh rotativo, partial, erro permanente
# ---------------------------------------------------------------------------


def _seed_tokens(settings, *, expired: bool = True, **extra) -> None:
    now_ms = int(time.time() * 1000)
    save_tokens(settings, {
        "access": extra.get("access", "access-antigo"),
        "refresh": extra.get("refresh", "refresh-antigo"),
        "expires": 0 if expired else now_ms + 3_600_000,
        "email": extra.get("email", "user@example.com"),
        "account_id": "acc-123",
    })


def test_get_access_token_valid_no_refresh(tmp_path: Path) -> None:
    """Token válido e não-expirado é retornado sem chamar refresh."""
    settings = _make_settings(tmp_path)
    _seed_tokens(settings, expired=False, access="valid-token")
    assert get_access_token(settings) == "valid-token"


def test_get_access_token_refresh_rotativo(tmp_path: Path) -> None:
    """Token expirado dispara refresh e persiste o novo token rotacionado."""
    settings = _make_settings(tmp_path)
    _seed_tokens(settings, expired=True)

    new_access = _make_oai_jwt("user@example.com", "acct-1", "plus")
    refreshed = {
        "access_token": new_access,
        "refresh_token": "refresh-novo",
        "expires_in": 3600,
    }
    with patch("meet.openai_oauth.refresh", return_value=refreshed):
        token = get_access_token(settings)

    assert token == new_access
    stored = load_tokens(settings)
    assert stored is not None
    assert stored["refresh"] == "refresh-novo"


def test_get_access_token_partial_refresh(tmp_path: Path) -> None:
    """Refresh parcial (sem novo access_token) mantém acesso anterior."""
    settings = _make_settings(tmp_path)
    old_access = _make_oai_jwt()
    _seed_tokens(settings, expired=True, access=old_access, refresh="rt-old")

    partial = {"refresh_token": "rt-rotated", "expires_in": 3600}
    with patch("meet.openai_oauth.refresh", return_value=partial):
        token = get_access_token(settings)

    assert token == old_access
    stored = load_tokens(settings)
    assert stored["refresh"] == "rt-rotated"


def test_get_access_token_invalid_grant_limpa_sessao(tmp_path: Path) -> None:
    """invalid_grant no refresh limpa sessão e lança ValueError de reconexão."""
    settings = _make_settings(tmp_path)
    _seed_tokens(settings, expired=True)
    error = RuntimeError("HTTP 400: invalid_grant — refresh token not found")
    with patch("meet.openai_oauth.refresh", side_effect=error):
        with pytest.raises(ValueError, match="Reconecte sua conta"):
            get_access_token(settings)
    assert load_tokens(settings) is None


def test_get_access_token_revoked_limpa_sessao(tmp_path: Path) -> None:
    """'revoked' no erro de refresh também limpa sessão."""
    settings = _make_settings(tmp_path)
    _seed_tokens(settings, expired=True)
    error = RuntimeError("HTTP 400: revoked")
    with patch("meet.openai_oauth.refresh", side_effect=error):
        with pytest.raises(ValueError, match="Reconecte sua conta"):
            get_access_token(settings)
    assert load_tokens(settings) is None


def test_get_access_token_rejected_access_forces_refresh(tmp_path: Path) -> None:
    """rejected_access força refresh mesmo com token aparentemente não-expirado."""
    settings = _make_settings(tmp_path)
    _seed_tokens(settings, expired=False, access="stale-token")
    new_access = _make_oai_jwt()
    refreshed = {"access_token": new_access, "refresh_token": "rt-new", "expires_in": 3600}
    with patch("meet.openai_oauth.refresh", return_value=refreshed):
        token = get_access_token(settings, rejected_access="stale-token")
    assert token == new_access


def test_get_access_token_not_authenticated(tmp_path: Path) -> None:
    """Lança ValueError descritivo quando não há tokens."""
    settings = _make_settings(tmp_path)
    with pytest.raises(ValueError, match="Não autenticado com OpenAI"):
        get_access_token(settings)


# ---------------------------------------------------------------------------
# Endpoints web
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient com settings real apontando para tmp_path."""
    from fastapi.testclient import TestClient
    from meet.config import Settings as RealSettings
    from meet.store import Store as RealStore
    from meet.web.app import create_app
    import meet.web.app as app_module

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "test.db"
    settings = RealSettings(data_dir=data_dir, output_dir=tmp_path / "output")

    monkeypatch.setattr(app_module, "_settings_store", lambda: (settings, RealStore(db)))
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    monkeypatch.setattr(app_module, "save_local_settings", lambda patch, s: None)

    app = create_app()
    return TestClient(app, raise_server_exceptions=True), settings


def test_api_settings_includes_openai_block(web_client) -> None:
    """GET /api/settings inclui bloco openai com todos os campos esperados."""
    client, _ = web_client
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert "openai" in data
    oai = data["openai"]
    assert oai["connected"] is False
    assert oai["email"] is None
    assert oai["expires"] is None
    assert oai["plan"] is None
    assert "api_key_configured" in oai


def test_api_settings_openai_connected(web_client) -> None:
    """GET /api/settings reflete tokens OpenAI persistidos."""
    client, settings = web_client
    save_tokens(settings, {
        "access": "at",
        "refresh": "rt",
        "expires": 9_999_999_999_999,
        "email": "dev@openai.com",
        "plan": "plus",
    })
    oai = client.get("/api/settings").json()["openai"]
    assert oai["connected"] is True
    assert oai["email"] == "dev@openai.com"
    assert oai["plan"] == "plus"


def test_api_models_anthropic_returns_canonical_catalog(web_client) -> None:
    """Anthropic expõe IDs canônicos, nomes humanos e default recomendado."""
    client, _ = web_client
    r = client.get("/api/settings/models", params={"provider": "anthropic"})

    assert r.status_code == 200
    data = r.json()
    assert set(data) == {
        "provider", "default_model", "models", "source", "stale", "warning", "allows_custom",
    }
    assert data["provider"] == "anthropic"
    assert data["default_model"] == "claude-sonnet-5"
    assert data["source"] == "bundled"
    assert data["stale"] is False
    assert data["warning"] is None
    assert isinstance(data["allows_custom"], bool)
    models = {model["id"]: model for model in data["models"]}
    assert models["claude-sonnet-5"] == {
        "id": "claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "recommended": True,
    }
    assert models["claude-fable-5"] == {
        "id": "claude-fable-5",
        "name": "Claude Fable 5",
        "recommended": False,
    }
    assert all(set(model) == {"id", "name", "recommended"} for model in data["models"])


def test_api_models_openai_returns_connected_account_discovery(web_client) -> None:
    """OpenAI conectada preserva prioridade e recomendação da descoberta da conta."""
    client, settings = web_client
    save_tokens(settings, {
        "access": "stored-access",
        "refresh": "stored-refresh",
        "expires": 9_999_999_999_999,
        "account_id": "acct-models",
    })
    discovered = [
        {"id": "gpt-5.5-codex", "name": "GPT-5.5 Codex", "recommended": True},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini", "recommended": False},
    ]

    with (
        patch("meet.openai_oauth.get_access_token", return_value="account-access"),
        patch("meet.model_catalog.fetch_codex_models", return_value=discovered) as fetch_models,
    ):
        r = client.get("/api/settings/models", params={"provider": "openai"})

    assert r.status_code == 200
    data = r.json()
    assert data == {
        "provider": "openai",
        "default_model": "gpt-5.5-codex",
        "models": discovered,
        "source": "provider",
        "stale": False,
        "warning": None,
        "allows_custom": True,
    }
    assert data["models"][0]["recommended"] is True
    assert [model["id"] for model in data["models"]] == ["gpt-5.5-codex", "gpt-5.4-mini"]
    access, tokens = fetch_models.call_args.args
    assert access == "account-access"
    assert tokens["account_id"] == "acct-models"


def test_api_models_ollama_discovers_tags_without_rewriting_ids(web_client) -> None:
    """Ollama consulta /api/tags e mantém IDs canônicos exatamente como recebidos."""
    client, settings = web_client
    settings.ollama_url = "http://ollama.test:11434/"
    response = _mock_resp(200, {
        "models": [
            {"name": "llama3.2:latest"},
            {"name": "registry.local/team/model:Q4_K_M"},
        ],
    })

    with patch("httpx.get", return_value=response) as get:
        r = client.get("/api/settings/models", params={"provider": "ollama"})

    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "ollama"
    assert data["default_model"] == "qwen3:14b"
    assert data["source"] == "provider"
    assert data["stale"] is False
    assert data["warning"] is None
    assert data["allows_custom"] is True
    assert [model["id"] for model in data["models"]] == [
        "qwen3:14b",
        "llama3.2:latest",
        "registry.local/team/model:Q4_K_M",
    ]
    get.assert_called_once_with("http://ollama.test:11434/api/tags", timeout=10)


def test_api_models_discovery_error_returns_stale_bundled_catalog(web_client) -> None:
    """Erro de descoberta OpenAI degrada para catálogo bundled com aviso."""
    client, settings = web_client
    save_tokens(settings, {
        "access": "stored-access",
        "refresh": "stored-refresh",
        "expires": 9_999_999_999_999,
    })

    with (
        patch("meet.openai_oauth.get_access_token", return_value="account-access"),
        patch("meet.model_catalog.fetch_codex_models", side_effect=OSError("offline")),
    ):
        r = client.get("/api/settings/models", params={"provider": "openai"})

    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "openai"
    assert data["default_model"] == "gpt-5.5"
    assert data["source"] == "bundled"
    assert data["stale"] is True
    assert data["warning"]
    assert data["allows_custom"] is True
    assert [model["id"] for model in data["models"]] == ["gpt-5.5", "gpt-4o"]
    assert data["models"][0]["recommended"] is True


def test_api_models_invalid_provider_returns_400(web_client) -> None:
    """Provider fora do contrato retorna erro de cliente."""
    client, _ = web_client
    r = client.get("/api/settings/models", params={"provider": "unsupported"})

    assert r.status_code == 400
    assert "Provider inválido" in r.json()["detail"]


def test_api_openai_authorize_returns_url_state_user_code(web_client) -> None:
    """POST /api/auth/openai/authorize retorna url, state e user_code."""
    client, _ = web_client
    fake_data = {
        "device_auth_id": "dauth-abc",
        "user_code": "XXXX-9999",
        "verification_uri_complete": "https://auth.openai.com/codex/device?code=XXXX-9999",
        "interval": 5,
        "expires_in": 900,
    }
    with patch("meet.openai_oauth.request_device_code", return_value=fake_data):
        r = client.post("/api/auth/openai/authorize")

    assert r.status_code == 200
    data = r.json()
    assert data["user_code"] == "XXXX-9999"
    assert len(data["state"]) == 32  # 16 bytes hex
    assert "auth.openai.com" in data["url"]


def test_api_openai_authorize_device_page_fallback(web_client) -> None:
    """verification_uri_complete ausente → url = _DEVICE_PAGE."""
    client, _ = web_client
    fake_data = {"device_auth_id": "d", "user_code": "A-1", "interval": 5, "expires_in": 900}
    with patch("meet.openai_oauth.request_device_code", return_value=fake_data):
        r = client.post("/api/auth/openai/authorize")
    assert r.json()["url"] == _DEVICE_PAGE


def test_api_openai_exchange_saves_tokens_and_sets_provider(
    web_client, monkeypatch
) -> None:
    """POST /api/auth/openai/exchange persiste tokens e ativa llm_provider=openai."""
    client, settings = web_client

    fake_auth = {
        "device_auth_id": "dauth-xyz",
        "user_code": "ABCD-5678",
        "interval": 5,
        "expires_in": 900,
    }
    with patch("meet.openai_oauth.request_device_code", return_value=fake_auth):
        state = client.post("/api/auth/openai/authorize").json()["state"]

    poll_result = {"authorization_code": "auth-code", "code_verifier": "verifier"}
    token_resp = {
        "id_token": _make_oai_jwt("me@openai.com", "acct-42", "plus"),
        "access_token": _make_jwt({"exp": int(time.time()) + 3600}),
        "refresh_token": "rt-final",
    }

    saved_providers: list = []

    import meet.web.app as app_module
    monkeypatch.setattr(app_module, "save_local_settings",
                        lambda p, s: saved_providers.append(p))

    with (
        patch("meet.openai_oauth.poll_device_token", return_value=poll_result),
        patch("meet.openai_oauth.exchange_device_code", return_value=token_resp),
    ):
        r = client.post("/api/auth/openai/exchange", json={"state": state})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["email"] == "me@openai.com"

    stored = load_tokens(settings)
    assert stored is not None
    assert stored["email"] == "me@openai.com"
    assert stored["plan"] == "plus"
    assert stored["account_id"] == "acct-42"
    assert any(p.get("llm_model") == "" for p in saved_providers)

    assert any(p.get("llm_provider") == "openai" for p in saved_providers)


def test_api_openai_exchange_timeout_preserves_state_for_retry(web_client) -> None:
    """Clique prematuro falha rápido e permite concluir com o mesmo device code."""
    client, _ = web_client
    fake_auth = {"device_auth_id": "d", "user_code": "A-1", "interval": 1}
    with patch("meet.openai_oauth.request_device_code", return_value=fake_auth):
        state = client.post("/api/auth/openai/authorize").json()["state"]

    with patch("meet.openai_oauth.poll_device_token", side_effect=TimeoutError):
        first = client.post("/api/auth/openai/exchange", json={"state": state})
    assert first.status_code == 408

    poll_result = {"authorization_code": "code", "code_verifier": "verifier"}
    token_resp = {
        "access_token": _make_jwt({"exp": int(time.time()) + 3600}),
        "refresh_token": "refresh",
    }
    with (
        patch("meet.openai_oauth.poll_device_token", return_value=poll_result),
        patch("meet.openai_oauth.exchange_device_code", return_value=token_resp),
    ):
        retry = client.post("/api/auth/openai/exchange", json={"state": state})
    assert retry.status_code == 200


def test_api_openai_exchange_invalid_state_400(web_client) -> None:
    """POST /api/auth/openai/exchange com state desconhecido retorna 400."""
    client, _ = web_client
    r = client.post("/api/auth/openai/exchange", json={"state": "invalid-state-xyz"})
    assert r.status_code == 400


def test_api_openai_logout_removes_tokens(web_client) -> None:
    """DELETE /api/auth/openai remove tokens e retorna ok:true."""
    client, settings = web_client
    save_tokens(settings, {"access": "at", "refresh": "rt", "expires": 0})
    r = client.delete("/api/auth/openai")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert load_tokens(settings) is None


def test_api_test_openai_oauth_first(web_client) -> None:
    """POST /api/settings/test target=openai usa OAuth quando conectado; não vaza token."""
    client, settings = web_client
    future_ms = int(time.time() * 1000) + 3_600_000
    save_tokens(settings, {
        "access": "at-valid",
        "refresh": "rt",
        "expires": future_ms,
        "email": "user@openai.com",
    })
    with patch("meet.openai_oauth.get_access_token", return_value="at-valid"):
        r = client.post("/api/settings/test", json={"target": "openai"})

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "OAuth" in data["detail"]
    assert "user@openai.com" in data["detail"]
    assert "at-valid" not in json.dumps(data)


def test_api_test_openai_api_key_fallback(web_client) -> None:
    """POST /api/settings/test target=openai usa API key quando OAuth não configurado."""
    client, settings = web_client
    settings.openai_api_key = "sk-test-key"

    fake_models_resp = MagicMock()
    fake_models_resp.status_code = 200

    with patch("httpx.get", return_value=fake_models_resp):
        r = client.post("/api/settings/test", json={"target": "openai"})

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "API key" in data["detail"]


def test_api_test_openai_reports_oauth_error_without_api_key(web_client) -> None:
    """Sessão OAuth inválida orienta reconexão em vez de parecer ausente."""
    client, settings = web_client
    save_tokens(settings, {"access": "expired", "refresh": "rt", "expires": 0})
    with patch(
        "meet.openai_oauth.get_access_token",
        side_effect=ValueError("Sessão OpenAI expirada. Reconecte sua conta."),
    ):
        r = client.post("/api/settings/test", json={"target": "openai"})

    assert r.status_code == 200
    assert r.json() == {
        "ok": False,
        "detail": "Sessão OpenAI expirada. Reconecte sua conta.",
    }


def test_api_test_openai_none_configured(web_client) -> None:
    """POST /api/settings/test target=openai retorna ok:false quando nada configurado."""
    client, settings = web_client
    settings.openai_api_key = ""
    r = client.post("/api/settings/test", json={"target": "openai"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
