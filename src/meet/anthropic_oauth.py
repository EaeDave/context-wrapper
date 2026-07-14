"""OAuth Anthropic — PKCE, troca de código, refresh e gestão de tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import threading
from pathlib import Path

import httpx

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_AUTHORIZE_BASE = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_OAUTH_BETA = "oauth-2025-04-20"
_BUFFER_MS = 5 * 60 * 1000  # 5 min de margem antes do vencimento
_REFRESH_LOCK = threading.Lock()


def _anthropic_error(resp: httpx.Response) -> str:
    """Extrai mensagem legível de uma resposta não-2xx da API Anthropic."""
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message")
        if msg:
            return f"HTTP {resp.status_code}: {msg}"
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:300]}"


def _check_response(resp: httpx.Response) -> None:
    """Lança RuntimeError com detalhe do erro Anthropic em resposta não-2xx."""
    if resp.is_success:
        return
    raise RuntimeError(_anthropic_error(resp))


def generate_pkce() -> tuple[str, str]:
    """Gera (verifier, challenge) PKCE.

    verifier = 96 bytes aleatórios → base64url sem padding.
    challenge = SHA-256(verifier) → base64url sem padding.
    """
    verifier_bytes = os.urandom(96)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def build_authorize_url(state: str, challenge: str) -> str:
    """Monta URL de autorização claude.ai com PKCE S256."""
    from urllib.parse import urlencode

    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "org:create_api_key user:profile user:inference",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{_AUTHORIZE_BASE}?{urlencode(params)}"


def exchange_code(code: str, state: str, verifier: str) -> dict:
    """Troca authorization code por tokens.

    code pode vir como 'code#state' — split no '#', fragment vira state.
    Retorna resposta JSON do endpoint (access_token, refresh_token, expires_in, account?).
    """
    if "#" in code:
        code, state = code.split("#", 1)

    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "state": state,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(_TOKEN_URL, json=payload)
        _check_response(resp)
    return resp.json()


def refresh(refresh_token: str) -> dict:
    """Renova tokens OAuth. Anthropic rotaciona o refresh token a cada uso."""
    payload = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    headers = {"anthropic-beta": _OAUTH_BETA}
    last_exc: Exception | None = None
    for _ in range(2):
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(_TOKEN_URL, json=payload, headers=headers)
                _check_response(resp)
            return resp.json()
        except httpx.NetworkError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Persistência de tokens
# ---------------------------------------------------------------------------


def _auth_path(settings) -> Path:
    return settings.data_dir / "auth.json"


def load_tokens(settings) -> dict | None:
    """Carrega tokens anthropic do auth.json. Retorna None se ausente ou inválido."""
    path = _auth_path(settings)
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text())
        return d.get("anthropic") or None
    except Exception:
        return None


def save_tokens(settings, d: dict) -> None:
    """Persiste tokens em auth.json (chmod 600)."""
    path = _auth_path(settings)
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    existing["anthropic"] = d
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def clear_tokens(settings) -> None:
    """Remove entrada 'anthropic' do auth.json."""
    path = _auth_path(settings)
    if not path.is_file():
        return
    try:
        existing = json.loads(path.read_text())
        existing.pop("anthropic", None)
        path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        path.chmod(0o600)
    except Exception:
        pass


def get_access_token(settings) -> str:
    """Retorna access token válido, renovando e persistindo a rotação OAuth."""
    tokens = load_tokens(settings)
    if not tokens:
        raise ValueError(
            "Não autenticado com Anthropic OAuth. "
            "Acesse a página Configurações para conectar."
        )

    now_ms = int(time.time() * 1000)
    if tokens.get("expires", 0) > now_ms:
        return tokens["access"]

    # Refresh tokens são rotativos e de uso único. Serializar e reler o arquivo
    # evita que duas requisições deste processo tentem consumir o mesmo token.
    with _REFRESH_LOCK:
        tokens = load_tokens(settings)
        if not tokens:
            raise ValueError(
                "Não autenticado com Anthropic OAuth. "
                "Acesse a página Configurações para conectar."
            )
        now_ms = int(time.time() * 1000)
        if tokens.get("expires", 0) > now_ms:
            return tokens["access"]

        try:
            d = refresh(tokens["refresh"])
        except RuntimeError as exc:
            if "invalid_grant" in str(exc):
                clear_tokens(settings)
                raise ValueError(
                    "Sessão Claude expirada ou revogada. "
                    "Reconecte sua conta na página Configurações."
                ) from exc
            raise

        account = d.get("account") or {}
        tokens = {
            "access": d["access_token"],
            "refresh": d.get("refresh_token") or tokens["refresh"],
            "expires": now_ms + d["expires_in"] * 1000 - _BUFFER_MS,
            "email": account.get("email_address") or tokens.get("email"),
            "account_id": account.get("uuid") or tokens.get("account_id"),
        }
        save_tokens(settings, tokens)

    return tokens["access"]
