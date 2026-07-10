"""Configuração: env vars > settings.local.json > config.toml > defaults."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "meet" / "config.toml"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "meet"

# Mapeamento campo -> variável de ambiente.
_ENV = {
    "hf_token": "HF_TOKEN",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "llm_provider": "MEET_LLM_PROVIDER",
    "llm_model": "MEET_LLM_MODEL",
    "ollama_url": "MEET_OLLAMA_URL",
}

# Chaves que settings.local.json pode sobrescrever.
_LOCAL_KEYS: frozenset[str] = frozenset({"hf_token", "llm_provider", "llm_model"})


@dataclass
class Settings:
    # Transcrição
    # large-v3 > turbo em áudio baixo/ruidoso (A/B 2026-07-09); turbo = opção rápida
    whisper_model: str = "large-v3"
    language: str = "pt"
    device: str = "cuda"
    compute_type: str = "int8_float16"

    # Diarização / banco de vozes
    hf_token: str = ""
    similarity_threshold: float = 0.65  # cosseno mínimo p/ reconhecer voz conhecida

    # LLM extrator
    llm_provider: str = "claude-code"  # "claude-code" | "anthropic" | "openai" | "ollama"
    llm_model: str = ""  # vazio = default do provider
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    ollama_url: str = "http://localhost:11434"

    # Saída
    data_dir: Path = DEFAULT_DATA_DIR
    output_dir: Path = Path.home() / "reunioes"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meet.db"


def local_settings_path(settings: Settings) -> Path:
    """Caminho para <data_dir>/settings.local.json."""
    return settings.data_dir / "settings.local.json"


def save_local_settings(patch: dict, settings: Settings) -> None:
    """Merge patch em settings.local.json (chmod 600). None remove a chave."""
    path = local_settings_path(settings)
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    for k, v in patch.items():
        if k not in _LOCAL_KEYS:
            continue
        if v is None:
            existing.pop(k, None)
        else:
            existing[k] = v
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def hf_token_source(settings: Settings, config_path: Path = CONFIG_PATH) -> str | None:
    """Retorna de onde veio hf_token: 'env' | 'local' | 'config' | None."""
    if not settings.hf_token:
        return None
    if os.environ.get("HF_TOKEN"):
        return "env"
    local = local_settings_path(settings)
    if local.is_file():
        try:
            d = json.loads(local.read_text())
            if d.get("hf_token"):
                return "local"
        except Exception:
            pass
    if config_path.is_file():
        try:
            raw = tomllib.loads(config_path.read_text())
            if raw.get("hf_token"):
                return "config"
        except Exception:
            pass
    return None


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    """Carrega defaults, sobrepõe com TOML, settings.local.json e depois env vars."""
    values: dict[str, object] = {}
    if config_path.is_file():
        raw = tomllib.loads(config_path.read_text())
        known = {f.name for f in fields(Settings)}
        values = {k: v for k, v in raw.items() if k in known}

    # Resolve data_dir para localizar settings.local.json antes de continuar.
    _data_dir = Path(str(values.get("data_dir", DEFAULT_DATA_DIR))).expanduser()
    local_path = _data_dir / "settings.local.json"
    if local_path.is_file():
        try:
            local = json.loads(local_path.read_text())
            for k in _LOCAL_KEYS:
                if k in local:
                    values[k] = local[k]
        except Exception:
            pass

    for field_name, env_name in _ENV.items():
        env_val = os.environ.get(env_name)
        if env_val:
            values[field_name] = env_val

    for key in ("data_dir", "output_dir"):
        if key in values:
            values[key] = Path(str(values[key])).expanduser()

    settings = Settings(**values)  # type: ignore[arg-type]
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    return settings
