"""Persistência compartilhada de auth.json com lock único.

Anthropic e OpenAI usam o mesmo arquivo; locks por módulo permitiam
que um provider apagasse a chave do outro em corrida.

``exclusive()`` segura o lock entre processos durante read-refresh-write
(tokens OAuth rotativos de uso único).
"""

from __future__ import annotations

import fcntl
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# RLock: mesma thread pode reentrar se helper público for chamado sob exclusive.
_THREAD_LOCK = threading.RLock()


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Lock de processo (fcntl) + thread — seções críticas multi-passo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _THREAD_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _read_all(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_all(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    path.chmod(0o600)


def load_provider_unlocked(path: Path, provider: str) -> dict | None:
    """Lê provider sem adquirir lock (caller deve estar em exclusive)."""
    entry = _read_all(path).get(provider)
    return entry if isinstance(entry, dict) else None


def save_provider_unlocked(path: Path, provider: str, tokens: dict) -> None:
    """Merge provider sem lock (caller em exclusive)."""
    existing = _read_all(path)
    existing[provider] = tokens
    _write_all(path, existing)


def clear_provider_unlocked(path: Path, provider: str) -> None:
    """Remove provider sem lock (caller em exclusive)."""
    if not path.is_file():
        return
    existing = _read_all(path)
    if provider not in existing:
        return
    existing.pop(provider, None)
    _write_all(path, existing)


def load_provider(path: Path, provider: str) -> dict | None:
    """Lê a entrada de um provider. Retorna None se ausente ou inválido."""
    try:
        with exclusive(path):
            return load_provider_unlocked(path, provider)
    except Exception:
        return None


def save_provider(path: Path, provider: str, tokens: dict) -> None:
    """Merge da chave do provider em auth.json (chmod 600)."""
    with exclusive(path):
        save_provider_unlocked(path, provider, tokens)


def clear_provider(path: Path, provider: str) -> None:
    """Remove a entrada do provider, preservando as demais."""
    try:
        with exclusive(path):
            clear_provider_unlocked(path, provider)
    except Exception:
        pass
