"""Configuração: env vars > ~/.config/meet/config.toml > defaults."""

from __future__ import annotations

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


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    """Carrega defaults, sobrepõe com TOML e depois com env vars."""
    values: dict[str, object] = {}
    if config_path.is_file():
        raw = tomllib.loads(config_path.read_text())
        known = {f.name for f in fields(Settings)}
        values = {k: v for k, v in raw.items() if k in known}

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
