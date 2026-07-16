"""LLM providers (Anthropic / OpenAI / Ollama / Claude Code).

Separado de ``extract`` para o orquestrador de prompts/chunking não carregar
todos os clientes HTTP junto com parsing de action items.
"""

from __future__ import annotations

import base64
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .model_catalog import (
    CODEX_BACKEND as _CODEX_BACKEND,
    CODEX_CLIENT_VERSION as _CODEX_CLIENT_VERSION,
    CODEX_FALLBACK_MODEL as _CODEX_FALLBACK_MODEL,
    fetch_codex_models,
)

_MAX_OUTPUT_TOKENS = 16_384
_TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 429})
_HTTP_RETRY_DELAYS = (1.0, 2.0, 4.0)


def _fmt_timestamp(seconds: float) -> str:
    """Formata segundos para rótulos de timestamp em prompts multimodais."""
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class LLMOutputTruncated(RuntimeError):
    """Provider encerrou resposta antes de completar o documento."""

    def __init__(self, partial: str = "") -> None:
        super().__init__("A resposta da LLM atingiu o limite de saída.")
        self.partial = partial


def _complete_provider_text(text: str, stop_reason: object = None) -> str:
    reason = str(stop_reason or "").casefold()
    if reason in {"max_tokens", "max_output_tokens", "length"} or "max_output" in reason:
        raise LLMOutputTruncated(text)
    if not text:
        raise RuntimeError(f"Resposta da LLM concluída sem texto (motivo={stop_reason!r}).")
    return text

def _is_transient_http_status(status_code: int) -> bool:
    return status_code in _TRANSIENT_HTTP_STATUSES or status_code >= 500


def _anthropic_post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    payload: dict,
    headers: dict,
) -> httpx.Response:
    """Repete somente falhas transitórias de transporte/gateway."""
    last_error: httpx.TransportError | None = None
    for attempt in range(len(_HTTP_RETRY_DELAYS) + 1):
        try:
            response = client.post(url, json=payload, headers=headers)
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if not _is_transient_http_status(response.status_code):
                return response
            last_error = None
            if attempt == len(_HTTP_RETRY_DELAYS):
                return response
        if attempt < len(_HTTP_RETRY_DELAYS):
            time.sleep(_HTTP_RETRY_DELAYS[attempt])
    if last_error is not None:
        raise last_error
    raise AssertionError("retry HTTP sem resposta ou erro")

@dataclass(frozen=True)
class ImageContent:
    """Imagem JPEG temporária acompanhada do timestamp da reunião."""

    path: Path
    timestamp: float


def _image_base64(image: ImageContent) -> str:
    return base64.b64encode(image.path.read_bytes()).decode("ascii")




class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str: ...

    def complete_with_images(
        self, system: str, user: str, images: list[ImageContent]
    ) -> str:
        raise NotImplementedError("O provider/modelo configurado não aceita imagens.")


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model or "claude-sonnet-5"

    def complete(self, system: str, user: str) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self._api_key)
        msg = client.messages.create(
            model=self._model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            block.text
            for block in msg.content
            if getattr(block, "type", None) == "text"
        )
        return _complete_provider_text(text, msg.stop_reason)

    def complete_with_images(
        self, system: str, user: str, images: list[ImageContent]
    ) -> str:
        import anthropic

        content: list[dict] = []
        for image in images:
            content.extend(
                [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _image_base64(image),
                        },
                    },
                    {"type": "text", "text": f"Timestamp {_fmt_timestamp(image.timestamp)}"},
                ]
            )
        content.append({"type": "text", "text": user})
        client = anthropic.Anthropic(api_key=self._api_key)
        msg = client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(
            block.text for block in msg.content if getattr(block, "type", None) == "text"
        )
        return _complete_provider_text(text, msg.stop_reason)


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model or "gpt-4o"

    def complete(self, system: str, user: str) -> str:
        import openai

        client = openai.OpenAI(api_key=self._api_key)
        resp = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        choice = resp.choices[0]
        return _complete_provider_text(
            choice.message.content or "",
            choice.finish_reason,
        )

    def complete_with_images(
        self, system: str, user: str, images: list[ImageContent]
    ) -> str:
        import openai

        content: list[dict] = []
        for image in images:
            content.extend(
                [
                    {"type": "text", "text": f"Timestamp {_fmt_timestamp(image.timestamp)}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{_image_base64(image)}",
                            "detail": "low",
                        },
                    },
                ]
            )
        content.append({"type": "text", "text": user})
        client = openai.OpenAI(api_key=self._api_key)
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        )
        choice = response.choices[0]
        return _complete_provider_text(choice.message.content or "", choice.finish_reason)



class OpenAIOAuthProvider(LLMProvider):
    """Usa assinatura ChatGPT/Codex via OAuth no backend Responses."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.llm_model
        self._resolved_model: str | None = None

    def _resolve_model(self, access: str, tokens: dict) -> str:
        """Seleciona primeiro modelo visível da conta quando não há override."""
        if self._model:
            return self._model
        if self._resolved_model:
            return self._resolved_model

        try:
            candidates = fetch_codex_models(access, tokens)
            if candidates:
                self._resolved_model = str(candidates[0]["id"])
        except Exception:
            # Catálogo remoto é otimização; indisponibilidade não bloqueia análise.
            pass

        self._resolved_model = self._resolved_model or _CODEX_FALLBACK_MODEL
        return self._resolved_model

    def complete(self, system: str, user: str) -> str:
        return self._complete_content(
            system,
            [{"type": "input_text", "text": user}],
        )

    def complete_with_images(
        self, system: str, user: str, images: list[ImageContent]
    ) -> str:
        content: list[dict] = []
        for image in images:
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": f"Timestamp {_fmt_timestamp(image.timestamp)}",
                    },
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:image/jpeg;base64,{_image_base64(image)}"
                        ),
                        "detail": "low",
                    },
                ]
            )
        content.append({"type": "input_text", "text": user})
        return self._complete_content(system, content)

    def _complete_content(self, system: str, content: list[dict]) -> str:
        import json

        import httpx

        from .openai_oauth import get_access_token, load_tokens

        access = get_access_token(self._settings)
        tokens = load_tokens(self._settings) or {}
        model = self._resolve_model(access, tokens)
        headers = {
            "Authorization": f"Bearer {access}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "originator": "context-wrapper",
            "User-Agent": "context-wrapper/0.1.0",
            "version": _CODEX_CLIENT_VERSION,
        }
        if tokens.get("account_id"):
            headers["ChatGPT-Account-ID"] = tokens["account_id"]

        payload = {
            "model": model,
            "instructions": system,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": content,
                }
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": "medium", "summary": "auto"},
            "store": False,
            "stream": True,
            "include": [],
        }

        def request(current_access: str) -> tuple[str, bool]:
            headers["Authorization"] = f"Bearer {current_access}"
            parts: list[str] = []
            stop_reason: object = None
            with httpx.Client(timeout=300) as client:
                with client.stream(
                    "POST",
                    f"{_CODEX_BACKEND}/responses",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code == 401:
                        return "", True
                    if not resp.is_success:
                        body = resp.read().decode(errors="replace")
                        raise RuntimeError(
                            f"OpenAI Codex respondeu HTTP {resp.status_code}: {body[:500]}"
                        )
                    for line in resp.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        kind = event.get("type")
                        response = event.get("response") or {}
                        if kind == "response.output_text.delta":
                            parts.append(event.get("delta") or "")
                        elif kind == "response.incomplete":
                            details = response.get("incomplete_details") or {}
                            stop_reason = details.get("reason") or "max_output_tokens"
                        elif kind == "response.failed":
                            error = response.get("error") or {}
                            detail = error.get("message") or error.get("code") or "falha desconhecida"
                            raise RuntimeError(f"Resposta OpenAI falhou: {detail}")
                        elif kind == "error":
                            error = event.get("error") or event
                            detail = error.get("message") or error.get("code") or "falha desconhecida"
                            raise RuntimeError(f"Stream OpenAI falhou: {detail}")
            return _complete_provider_text("".join(parts), stop_reason), False

        result, unauthorized = request(access)
        if unauthorized:
            # Token pode ter sido revogado/rotacionado por outro cliente.
            # Forçar refresh uma vez; demais erros nunca são repetidos.
            access = get_access_token(self._settings, rejected_access=access)
            result, unauthorized = request(access)
        if unauthorized:
            raise ValueError(
                "Sessão OpenAI expirada ou revogada. "
                "Reconecte sua conta na página Configurações."
            )
        return result


class OllamaProvider(LLMProvider):
    def __init__(self, url: str, model: str) -> None:
        self._url = url.rstrip("/")
        self._model = model or "qwen3:14b"

    def complete(self, system: str, user: str) -> str:
        import httpx

        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        resp = httpx.post(f"{self._url}/api/chat", json=payload, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        return _complete_provider_text(
            data.get("message", {}).get("content", ""),
            data.get("done_reason"),
        )

    def complete_with_images(
        self, system: str, user: str, images: list[ImageContent]
    ) -> str:
        labels = "\n".join(
            f"Imagem {index + 1}: timestamp {_fmt_timestamp(image.timestamp)}"
            for index, image in enumerate(images)
        )
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"{labels}\n\n{user}",
                    "images": [_image_base64(image) for image in images],
                },
            ],
        }
        response = httpx.post(f"{self._url}/api/chat", json=payload, timeout=300.0)
        response.raise_for_status()
        data = response.json()
        return _complete_provider_text(
            data.get("message", {}).get("content", ""), data.get("done_reason")
        )


class ClaudeCodeProvider(LLMProvider):
    """Usa o CLI do Claude Code (`claude -p`) — consome a assinatura, não a API."""

    def __init__(self, model: str) -> None:
        self._model = model or "sonnet"

    def complete(self, system: str, user: str) -> str:
        import shutil
        import subprocess

        if shutil.which("claude") is None:
            raise ValueError(
                "CLI 'claude' não encontrado no PATH. "
                "Instale o Claude Code ou troque llm_provider "
                "(anthropic/openai/ollama) em ~/.config/meet/config.toml."
            )

        cmd = [
            "claude", "-p",
            "--model", self._model,
            "--append-system-prompt", system,
            "--output-format", "text",
        ]
        # Transcript via stdin: evita estourar limite de tamanho de argv.
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, timeout=600
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p falhou: {proc.stderr[-500:]}")
        return proc.stdout


class AnthropicOAuthProvider(LLMProvider):
    """Provider usando token OAuth (sk-ant-oat…) — sem API key, via assinatura Claude."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = settings.llm_model or "claude-sonnet-5"

    def complete(self, system: str, user: str) -> str:
        import httpx

        from .anthropic_oauth import _check_response, get_access_token

        access = get_access_token(self._settings)
        headers = {
            "Authorization": f"Bearer {access}",
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-cli/2.0.0 (external, cli)",
            "Content-Type": "application/json",
        }
        # system como lista de blocks — PRIMEIRO block é o spoof obrigatório.
        system_blocks = [
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": system},
        ]
        payload = {
            "model": self._model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user}],
        }
        with httpx.Client(timeout=300) as client:
            resp = _anthropic_post_with_retry(
                client,
                "https://api.anthropic.com/v1/messages",
                payload=payload,
                headers=headers,
            )
            _check_response(resp)
        data = resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return _complete_provider_text("".join(parts), data.get("stop_reason"))

    def complete_with_images(
        self, system: str, user: str, images: list[ImageContent]
    ) -> str:
        from .anthropic_oauth import _check_response, get_access_token

        access = get_access_token(self._settings)
        content: list[dict] = []
        for image in images:
            content.extend(
                [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _image_base64(image),
                        },
                    },
                    {"type": "text", "text": f"Timestamp {_fmt_timestamp(image.timestamp)}"},
                ]
            )
        content.append({"type": "text", "text": user})
        headers = {
            "Authorization": f"Bearer {access}",
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-cli/2.0.0 (external, cli)",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": 4096,
            "system": [
                {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
                {"type": "text", "text": system},
            ],
            "messages": [{"role": "user", "content": content}],
        }
        with httpx.Client(timeout=300) as client:
            response = _anthropic_post_with_retry(
                client,
                "https://api.anthropic.com/v1/messages",
                payload=payload,
                headers=headers,
            )
            _check_response(response)
        data = response.json()
        text = "".join(
            block["text"] for block in data.get("content", []) if block.get("type") == "text"
        )
        return _complete_provider_text(text, data.get("stop_reason"))



def get_provider(settings: Settings) -> LLMProvider:
    """Instancia o provider LLM configurado, validando credenciais."""
    provider = settings.llm_provider.lower()
    model = settings.llm_model

    if provider == "claude-code":
        return ClaudeCodeProvider(model)

    if provider == "anthropic":
        from .anthropic_oauth import load_tokens

        if load_tokens(settings):
            return AnthropicOAuthProvider(settings)
        if settings.anthropic_api_key:
            return AnthropicProvider(settings.anthropic_api_key, model)
        raise ValueError(
            "Provider 'anthropic' sem credenciais. "
            "Acesse a página Configurações para conectar via OAuth "
            "ou configure ANTHROPIC_API_KEY."
        )

    if provider == "openai":
        from .openai_oauth import load_tokens

        if load_tokens(settings):
            return OpenAIOAuthProvider(settings)
        if settings.openai_api_key:
            return OpenAIProvider(settings.openai_api_key, model)
        raise ValueError(
            "Provider 'openai' sem credenciais. "
            "Acesse a página Configurações para conectar com ChatGPT/Codex "
            "ou configure OPENAI_API_KEY."
        )

    if provider == "ollama":
        return OllamaProvider(settings.ollama_url, model)

    raise ValueError(
        f"Provider LLM desconhecido: {provider!r}. "
        "Valores válidos: 'claude-code', 'anthropic', 'openai', 'ollama'."
    )



def validate_credentials(settings: Settings) -> None:
    """Valida credenciais antes das etapas caras do pipeline."""
    provider = get_provider(settings)
    if isinstance(provider, AnthropicOAuthProvider):
        from .anthropic_oauth import get_access_token

        get_access_token(settings)
    elif isinstance(provider, OpenAIOAuthProvider):
        from .openai_oauth import get_access_token

        get_access_token(settings)


