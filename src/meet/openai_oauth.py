"""OAuth OpenAI — device-code flow, exchange, refresh e gestão de tokens."""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import httpx

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_BASE = "https://auth.openai.com"
_USERCODE_URL = f"{_BASE}/api/accounts/deviceauth/usercode"
_TOKEN_POLL_URL = f"{_BASE}/api/accounts/deviceauth/token"
_TOKEN_EXCHANGE_URL = f"{_BASE}/oauth/token"
_DEVICE_PAGE = "https://auth.openai.com/codex/device"
_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
_BUFFER_MS = 5 * 60 * 1000  # 5-min margem antes do vencimento (milissegundos)
_POLL_TIMEOUT_S = 15 * 60  # 15 minutos máximo de polling
_REFRESH_LOCK = threading.Lock()

# Erros de refresh que tornam a sessão irrecuperável → limpar tokens
_PERMANENT_ERRORS = frozenset({"invalid_grant", "refresh_token_reused", "revoked"})

# Erros de polling que indicam falha definitiva (não apenas "pendente")
_POLL_TERMINAL_ERRORS = frozenset({"access_denied", "expired_token", "invalid_request"})

# User-Agent honesto (não spoofa browser); versão espelhada do pyproject.
_USER_AGENT = "context-wrapper/0.1.0"


def _auth_headers() -> dict[str, str]:
    """Cabeçalhos comuns enviados a todos os endpoints de autenticação OpenAI."""
    return {"User-Agent": _USER_AGENT}


def _openai_error(resp: httpx.Response) -> str:
    """Extrai mensagem legível de resposta não-2xx, suportando formato flat e nested."""
    try:
        body = resp.json()
        err = body.get("error")
        if isinstance(err, str) and err:
            desc = body.get("error_description") or body.get("message", "")
            suffix = f" — {desc}" if desc else ""
            return f"HTTP {resp.status_code}: {err}{suffix}"
        if isinstance(err, dict):
            msg = err.get("message", "")
            if msg:
                return f"HTTP {resp.status_code}: {msg}"
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:300]}"


def _is_pending(resp: httpx.Response) -> bool:
    """Retorna True se a resposta indica polling ainda pendente (403/404 sem erro terminal)."""
    if resp.status_code not in (403, 404):
        return False
    try:
        err = resp.json().get("error", "")
        if isinstance(err, str) and err in _POLL_TERMINAL_ERRORS:
            return False
    except Exception:
        pass
    return True


def _decode_jwt_claims(token: str) -> dict:
    """Decodifica claims JWT localmente sem validar assinatura.

    Extrai apenas exp/email/account_id/chatgpt_plan_type para fins informativos.
    Nunca usar claims como autorização.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        # Restaurar padding base64
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Device-code flow
# ---------------------------------------------------------------------------


def request_device_code(client_id: str = CLIENT_ID) -> dict:
    """Inicia device-code flow.

    Retorna dict com device_auth_id, user_code, verification_uri_complete,
    interval (segundos) e expires_in (segundos).
    """
    with httpx.Client(timeout=30, headers=_auth_headers()) as client:
        resp = client.post(_USERCODE_URL, json={"client_id": client_id})
    if not resp.is_success:
        raise RuntimeError(_openai_error(resp))
    return resp.json()


def poll_device_token(
    device_auth_id: str,
    user_code: str,
    interval: int = 5,
    timeout: int = _POLL_TIMEOUT_S,
) -> dict:
    """Faz polling até o usuário autorizar ou o timeout expirar.

    Usa um único httpx.Client para reutilizar conexão TLS durante todo o poll.
    Retorna {authorization_code, code_verifier} em caso de sucesso.
    Lança RuntimeError em erro definitivo, TimeoutError em timeout.
    """
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=30, headers=_auth_headers()) as client:
        while time.monotonic() < deadline:
            resp = client.post(
                _TOKEN_POLL_URL,
                json={"device_auth_id": device_auth_id, "user_code": user_code},
            )
            if resp.is_success:
                return resp.json()
            if _is_pending(resp):
                time.sleep(max(1, int(interval)))
                continue
            raise RuntimeError(_openai_error(resp))
    raise TimeoutError("OpenAI device-code timeout (15 min)")


def exchange_device_code(authorization_code: str, code_verifier: str) -> dict:
    """Troca authorization_code+code_verifier por access/refresh tokens.

    Envia form-urlencoded conforme spec OAuth2 device flow OpenAI.
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": authorization_code,
        "code_verifier": code_verifier,
        "redirect_uri": _REDIRECT_URI,
    }
    with httpx.Client(timeout=30, headers=_auth_headers()) as client:
        resp = client.post(_TOKEN_EXCHANGE_URL, data=payload)
    if not resp.is_success:
        raise RuntimeError(_openai_error(resp))
    return resp.json()


def refresh(refresh_token: str) -> dict:
    """Renova tokens OAuth via refresh token. Refresh pode ser rotativo."""
    payload = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    with httpx.Client(timeout=30, headers=_auth_headers()) as client:
        resp = client.post(_TOKEN_EXCHANGE_URL, json=payload)
    if not resp.is_success:
        raise RuntimeError(_openai_error(resp))
    return resp.json()


# ---------------------------------------------------------------------------
# Persistência de tokens
# ---------------------------------------------------------------------------


def _auth_path(settings) -> Path:
    return settings.data_dir / "auth.json"


def load_tokens(settings) -> dict | None:
    """Carrega tokens openai do auth.json. Retorna None se ausente ou inválido."""
    path = _auth_path(settings)
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text())
        return d.get("openai") or None
    except Exception:
        return None


def save_tokens(settings, d: dict) -> None:
    """Persiste tokens em auth.json sob chave 'openai' (chmod 600, preserva demais providers)."""
    path = _auth_path(settings)
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    existing["openai"] = d
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def clear_tokens(settings) -> None:
    """Remove entrada 'openai' do auth.json, preservando outros providers."""
    path = _auth_path(settings)
    if not path.is_file():
        return
    try:
        existing = json.loads(path.read_text())
        existing.pop("openai", None)
        path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        path.chmod(0o600)
    except Exception:
        pass


# Namespace keys do JWT OpenAI: os valores são *dicts*, não strings dotadas.
# profile = claims["https://api.openai.com/profile"] → {email: ...}
# auth    = claims["https://api.openai.com/auth"]    → {chatgpt_account_id: ..., chatgpt_plan_type: ...}
_NS_PROFILE = "https://api.openai.com/profile"
_NS_AUTH = "https://api.openai.com/auth"


def _extract_identity(claims: dict) -> tuple[str | None, str | None, str | None]:
    """Extrai (email, account_id, plan) dos claims do ID token OpenAI."""
    profile = claims.get(_NS_PROFILE) or {}
    auth = claims.get(_NS_AUTH) or {}
    return (
        claims.get("email")
        or (profile.get("email") if isinstance(profile, dict) else None),
        auth.get("chatgpt_account_id") if isinstance(auth, dict) else None,
        auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None,
    )


def build_stored_tokens(d: dict, fallback: dict | None = None) -> dict:
    """Constrói o dict a persistir a partir de uma resposta de token exchange/refresh.

    fallback=None (exchange): exige access_token e refresh_token no response.
    fallback=dict (refresh): access_token/refresh_token podem ser opcionais.
    Extrai identidade do ID token e validade do access token efetivos.
    """
    fb = fallback or {}

    if fallback is None:
        # Exchange inicial — access e refresh são obrigatórios.
        effective_access = d["access_token"]
        effective_refresh = d["refresh_token"]
    else:
        # Refresh — cada token pode ser omitido; manter o anterior.
        effective_access = d.get("access_token") or fallback["access"]
        effective_refresh = d.get("refresh_token") or fallback.get("refresh", "")

    effective_id = d.get("id_token") or fb.get("id_token")
    now_ms = int(time.time() * 1000)
    access_claims = _decode_jwt_claims(effective_access)
    identity_claims = (
        _decode_jwt_claims(effective_id) if effective_id else access_claims
    )
    email, account_id, plan = _extract_identity(identity_claims)

    if "expires_in" in d:
        expires = now_ms + int(d["expires_in"]) * 1000 - _BUFFER_MS
    elif access_claims.get("exp"):
        expires = int(access_claims["exp"]) * 1000 - _BUFFER_MS
    else:
        expires = now_ms + 3600 * 1000 - _BUFFER_MS  # fallback 1h

    return {
        "id_token": effective_id,
        "access": effective_access,
        "refresh": effective_refresh,
        "expires": expires,
        "email": email or fb.get("email"),
        "account_id": account_id or fb.get("account_id"),
        "plan": plan or fb.get("plan"),
    }


# ---------------------------------------------------------------------------
# Token válido com refresh automático
# ---------------------------------------------------------------------------


def get_access_token(settings, rejected_access: str | None = None) -> str:
    """Retorna access token válido, renovando via refresh se necessário.

    rejected_access: token que resultou em 401 — forçar renovação mesmo sem expirar
    no cache (ex. 401 concorrente recebido por outro thread).
    """
    tokens = load_tokens(settings)
    if not tokens:
        raise ValueError(
            "Não autenticado com OpenAI. "
            "Acesse a página Configurações para conectar."
        )

    now_ms = int(time.time() * 1000)
    current_access = tokens.get("access", "")

    # Token ainda válido e não rejeitado por 401
    if tokens.get("expires", 0) > now_ms and current_access != rejected_access:
        return current_access

    # Refresh serializado: apenas uma thread renova por vez
    with _REFRESH_LOCK:
        # Re-ler dentro do lock — pode ter sido renovado por outra thread enquanto esperava
        tokens = load_tokens(settings)
        if not tokens:
            raise ValueError(
                "Não autenticado com OpenAI. "
                "Acesse a página Configurações para conectar."
            )
        now_ms = int(time.time() * 1000)
        current_access = tokens.get("access", "")
        if tokens.get("expires", 0) > now_ms and current_access != rejected_access:
            return current_access

        if not tokens.get("refresh"):
            raise ValueError(
                "Sessão OpenAI expirada sem refresh token. "
                "Reconecte na página Configurações."
            )

        try:
            d = refresh(tokens["refresh"])
        except RuntimeError as exc:
            exc_str = str(exc).lower()
            if any(e in exc_str for e in _PERMANENT_ERRORS):
                clear_tokens(settings)
                raise ValueError(
                    "Sessão OpenAI expirada ou revogada. "
                    "Reconecte sua conta na página Configurações."
                ) from exc
            raise

        new_tokens = build_stored_tokens(d, fallback=tokens)
        save_tokens(settings, new_tokens)

    return new_tokens["access"]
