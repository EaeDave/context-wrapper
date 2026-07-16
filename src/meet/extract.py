"""Extração de resumo e action items via LLM."""

from __future__ import annotations

import base64
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .models import ActionItem, MeetingFact, TranscriptSegment

# Reuniões densas geram saídas maiores que o transcript sugere. Dividir cedo
# limita cada resposta e evita perder o documento inteiro por truncamento.
_MAX_TRANSCRIPT_CHARS = 12_000
_CHUNK_TRANSCRIPT_CHARS = 10_000
_CHUNK_OVERLAP_CHARS = 1_000
_MAX_OUTPUT_TOKENS = 16_384
_MAX_SINGLE_CALL_SECONDS = 10 * 60
_CHUNK_MAX_SECONDS = 8 * 60
ExtractionProgressCallback = Callable[[float | None, str], None]
_TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 429})
_MATCH_STOPWORDS = frozenset(
    {
        "a",
        "ao",
        "aos",
        "as",
        "da",
        "das",
        "de",
        "do",
        "dos",
        "e",
        "em",
        "na",
        "nas",
        "no",
        "nos",
        "o",
        "os",
        "para",
        "por",
        "uma",
        "um",
    }
)
_HTTP_RETRY_DELAYS = (1.0, 2.0)

# Prompt do sistema em PT-BR — usa __PARTICIPANTS__ como placeholder.
_SYSTEM_PROMPT = """\
Você é um assistente especialista em análise de reuniões técnicas.
Analise o transcript a seguir e retorne SOMENTE um bloco JSON válido — sem nenhum texto antes ou depois do JSON.

Preserve LITERALMENTE todos os detalhes técnicos mencionados: nomes de endpoints, URLs, \
nomes de telas, nomes de campos, nomes de tabelas, comandos, siglas e tecnologias \
exatamente como aparecem no transcript.

O JSON deve seguir exatamente este schema (sem markdown, sem comentários):
{
  "title": "<título conciso da reunião em PT-BR>",
  "summary": "<resumo executivo em PT-BR, 3-6 frases, preservando termos técnicos literais>",
  "facts": [
    {
      "kind": "<decision | requirement | constraint | open_question>",
      "text": "<texto literal da decisão/requisito/restrição/questão>",
      "source_start": "<HH:MM:SS ou null>",
      "source_end": "<HH:MM:SS ou null>",
      "evidence_quote": "<trecho literal do transcript que sustenta o fato, ou null>",
      "explicitness": "<explicit | inferred>"
    }
  ],
  "action_items": [
    {
      "what": "<o que precisa ser feito>",
      "where": "<tela, endpoint, módulo, repositório — ou null se não aplicável>",
      "details": "<detalhes técnicos literais mencionados — ou null>",
      "requested_by": "<nome de quem pediu, ou null se não identificado>",
      "assigned_to": ["me"] ou ["Alice"] ou ["me","Bob"] ou null,
      "priority": "<alta | media | baixa>",
      "source_start": "<HH:MM:SS ou null>",
      "source_end": "<HH:MM:SS ou null>",
      "evidence_quote": "<trecho literal do transcript que originou a tarefa, ou null>",
      "explicitness": "<explicit | inferred>"
    }
  ]
}

Participantes identificados: __PARTICIPANTS__

REGRAS DE RASTREABILIDADE:
- source_start/source_end: timestamps HH:MM:SS do trecho do transcript; null se não identificável.
- evidence_quote: copie literalmente o trecho mais curto que justifica o fato/tarefa; null se não houver trecho claro.
- explicitness: "explicit" quando mencionado diretamente; "inferred" quando deduzido por contexto.
- assigned_to: array JSON de responsáveis (use "me" para o dono das notas); null se indefinido. Inclua TODAS as tarefas, inclusive atribuídas a terceiros.
- Linhas marcadas como TELA são observações visuais associadas ao timestamp; use-as para detalhar resumo, fatos e tarefas, mas nunca as copie como evidence_quote da fala.
- Não invente timestamps, citações ou responsáveis.
- Cada fato ou tarefa distinta deve aparecer uma única vez; não repita itens.
"""

_CHUNK_SYSTEM_PROMPT = """\
Você analisa UM BLOCO temporal de uma reunião técnica. Retorne SOMENTE JSON válido.

Preserve literalmente nomes técnicos e classifique todos os candidatos, inclusive tarefas
atribuídas a terceiros. Resolva "eu" usando o label do falante. Copie os timestamps do
transcript para cada evidência.

Schema exato:
{
  "chunk_summary": "<síntese factual e concisa do bloco>",
  "decisions": [
    {"text": "<decisão>", "source_start": "<HH:MM:SS>", "source_end": "<HH:MM:SS>", "evidence_quote": "<trecho ou null>", "explicitness": "<explicit|inferred>"}
  ],
  "requirements": [
    {"text": "<requisito>", "source_start": "<HH:MM:SS>", "source_end": "<HH:MM:SS>", "evidence_quote": "<trecho ou null>", "explicitness": "<explicit|inferred>"}
  ],
  "constraints": [
    {"text": "<restrição>", "source_start": "<HH:MM:SS>", "source_end": "<HH:MM:SS>", "evidence_quote": "<trecho ou null>", "explicitness": "<explicit|inferred>"}
  ],
  "open_questions": [
    {"text": "<questão em aberto>", "source_start": "<HH:MM:SS>", "source_end": "<HH:MM:SS>", "evidence_quote": "<trecho ou null>", "explicitness": "<explicit|inferred>"}
  ],
  "action_items": [
    {
      "what": "<tarefa>",
      "where": "<local técnico ou null>",
      "details": "<detalhes literais ou null>",
      "requested_by": "<quem pediu ou null>",
      "assigned_to": ["me"] ou ["Alice"] ou null,
      "priority": "<alta | media | baixa>",
      "source_start": "<HH:MM:SS>",
      "source_end": "<HH:MM:SS>",
      "evidence_quote": "<trecho literal ou null>",
      "explicitness": "<explicit|inferred>"
    }
  ]
}

- Linhas marcadas como TELA são observações visuais; use-as para nomes de telas, campos, mensagens e estado do produto, sem tratá-las como citação falada.
Não invente timestamps, responsáveis ou citações.
Não repita decisões, requisitos, restrições, questões ou tarefas.
"""

_CONSOLIDATE_SYSTEM_PROMPT = """\
Você consolida análises temporais de uma única reunião técnica. Retorne SOMENTE JSON válido.

Produza visão global coerente usando a ordem dos blocos. Considere decisões, requisitos,
restrições, questões e tarefas fornecidas apenas para compor o título e o resumo. Resolva
correções posteriores pela ordem temporal.

O JSON final deve seguir exatamente este schema:
{
  "title": "<título conciso em PT-BR>",
  "summary": "<resumo executivo em PT-BR, 3-6 frases, incluindo decisões e requisitos centrais>"
}

Participantes identificados: __PARTICIPANTS__

Não reemita fatos ou tarefas. Não inclua outras chaves.
"""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


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


_VISUAL_SYSTEM_PROMPT = """\
Você analisa capturas de tela de uma reunião de demonstração de produto.
Relacione cada imagem ao timestamp informado e retorne SOMENTE JSON válido:
{"observations":[{"timestamp":"HH:MM:SS","description":"descrição objetiva do estado da interface","visible_text":["texto literal relevante"],"relevance":"high|medium|low"}]}

Priorize telas, campos, botões, erros, estados antes/depois e detalhes que esclareçam
o transcript. Ignore webcam, papel de parede, barras do sistema e imagens sem valor.
Não deduza ações, decisões ou requisitos nesta etapa. Não invente texto ilegível.
"""


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



from .model_catalog import (
    CODEX_BACKEND as _CODEX_BACKEND,
    CODEX_CLIENT_VERSION as _CODEX_CLIENT_VERSION,
    CODEX_FALLBACK_MODEL as _CODEX_FALLBACK_MODEL,
    fetch_codex_models,
)


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
                    "content": [{"type": "input_text", "text": user}],
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


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _fmt_timestamp(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _render_segment(seg: TranscriptSegment) -> str:
    speaker = seg.speaker or "Desconhecido"
    start = _fmt_timestamp(seg.start)
    end = _fmt_timestamp(seg.end)
    return f"[{start}-{end}] {speaker}: {seg.text.strip()}"


def _build_transcript(segments: list[TranscriptSegment]) -> str:
    return "\n".join(_render_segment(seg) for seg in segments)


@dataclass(frozen=True)
class TranscriptChunk:
    index: int
    start: float
    end: float
    text: str

def _split_transcript(
    segments: list[TranscriptSegment],
    *,
    max_chars: int = _CHUNK_TRANSCRIPT_CHARS,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
    max_seconds: float = _CHUNK_MAX_SECONDS,
) -> list[TranscriptChunk]:
    """Divide em turnos inteiros por tamanho e duração, mantendo overlap."""
    if max_chars <= 0:
        raise ValueError("max_chars deve ser positivo")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars deve estar entre 0 e max_chars")
    if max_seconds <= 0:
        raise ValueError("max_seconds deve ser positivo")
    if not segments:
        return []

    rendered = [_render_segment(seg) for seg in segments]
    chunks: list[TranscriptChunk] = []
    start_index = 0
    while start_index < len(segments):
        end_index = start_index
        size = 0
        while end_index < len(segments):
            addition = len(rendered[end_index]) + (1 if end_index > start_index else 0)
            exceeds_chars = size + addition > max_chars
            exceeds_duration = (
                segments[end_index].end - segments[start_index].start > max_seconds
            )
            if end_index > start_index and (exceeds_chars or exceeds_duration):
                break
            size += addition
            end_index += 1

        chunks.append(
            TranscriptChunk(
                index=len(chunks),
                start=segments[start_index].start,
                end=segments[end_index - 1].end,
                text="\n".join(rendered[start_index:end_index]),
            )
        )
        if end_index >= len(segments):
            break

        overlap_start = end_index
        overlap_size = 0
        while overlap_start > start_index + 1:
            candidate = overlap_start - 1
            addition = len(rendered[candidate]) + (1 if overlap_size else 0)
            if overlap_size + addition > overlap_chars:
                break
            overlap_size += addition
            overlap_start = candidate
        start_index = overlap_start

    return chunks


def _parse_json_response(text: str) -> dict:
    """Extrai o primeiro objeto JSON da resposta do LLM."""
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    candidate_text = fenced.group(1) if fenced else text
    start = candidate_text.find("{")
    end = candidate_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = candidate_text[start : end + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    raise ValueError(text)


def analyze_visual_frames(
    provider: LLMProvider,
    frames: list[object],
    segments: list[TranscriptSegment],
) -> list[dict]:
    """Descreve frames em lotes pequenos; provider sem visão degrada para []."""
    images = [
        ImageContent(path=Path(str(frame.path)), timestamp=float(frame.timestamp))
        for frame in frames
        if Path(str(frame.path)).is_file()
    ]
    if not images:
        return []

    observations: list[dict] = []
    try:
        for batch_start in range(0, len(images), 6):
            batch = images[batch_start : batch_start + 6]
            window_start = max(batch[0].timestamp - 15.0, 0.0)
            window_end = batch[-1].timestamp + 15.0
            transcript = _build_transcript(
                [
                    segment
                    for segment in segments
                    if segment.end >= window_start and segment.start <= window_end
                ]
            )
            response = provider.complete_with_images(
                _VISUAL_SYSTEM_PROMPT,
                f"Transcript próximo às imagens:\n{transcript or '(sem fala próxima)'}",
                batch,
            )
            data = _parse_json_response(response)
            for raw in data.get("observations") or []:
                if not isinstance(raw, dict) or not raw.get("description"):
                    continue
                timestamp = _parse_hms(raw.get("timestamp"))
                if timestamp is None:
                    continue
                observations.append(
                    {
                        "timestamp": timestamp,
                        "description": str(raw["description"]).strip(),
                        "visible_text": [
                            str(value).strip()
                            for value in (raw.get("visible_text") or [])
                            if str(value).strip()
                        ],
                        "relevance": raw.get("relevance") or "medium",
                        "image_path": str(
                            min(
                                images,
                                key=lambda image: abs(image.timestamp - timestamp),
                            ).path
                        ),
                    }
                )
    except (NotImplementedError, ValueError, RuntimeError, httpx.HTTPError):
        return []
    return observations


def _render_visual_context(observations: list[dict], start: float, end: float) -> str:
    lines: list[str] = []
    for observation in observations:
        timestamp = float(observation.get("timestamp", -1.0))
        if timestamp < start or timestamp > end:
            continue
        visible = ", ".join(observation.get("visible_text") or [])
        suffix = f" · texto visível: {visible}" if visible else ""
        lines.append(
            f"[{_fmt_timestamp(timestamp)}] TELA: {observation['description']}{suffix}"
        )
    return "\n".join(lines)


def _action_item_is_for_owner(d: dict) -> bool:
    """True se a tarefa pertence ao dono (me) ou não tem responsável definido.

    Mantido para referência; extract() não filtra mais — filtering happens in store.
    """
    assigned_to = d.get("assigned_to")
    if assigned_to is None:
        return True
    if isinstance(assigned_to, list):
        return any(
            isinstance(owner, str) and owner.strip().casefold() == "me"
            for owner in assigned_to
        )
    if not isinstance(assigned_to, str):
        return False
    normalized = assigned_to.strip().casefold()
    if re.search(r"(?:^|[^\w])me(?:$|[^\w])", normalized):
        return True
    return normalized in {
        "",
        "null",
        "none",
        "não identificado",
        "nao identificado",
        "indefinido",
        "unknown",
        "unclear",
    }


def _parse_hms(value: object) -> float | None:
    """Converte HH:MM:SS em segundos; rejeita intervalos fora do relógio."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    except (ValueError, TypeError):
        return None
    if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
        return None
    return h * 3600.0 + m * 60.0 + s


def _normalize_assigned_to(value: object) -> list[str] | None:
    """Normaliza assigned_to do LLM para list[str]|None."""
    _NONE_SENTINELS = frozenset({
        "null", "none", "não identificado", "nao identificado",
        "indefinido", "unknown", "unclear", "",
    })
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if v.casefold() in _NONE_SENTINELS:
            return None
        return [v]
    if isinstance(value, list):
        cleaned = [
            s.strip()
            for s in value
            if isinstance(s, str) and s.strip()
            and s.strip().casefold() not in _NONE_SENTINELS
        ]
        return cleaned if cleaned else None
    return None


def _validate_evidence(
    segments: list[TranscriptSegment],
    source_start: float | None,
    source_end: float | None,
    evidence_quote: str | None,
) -> bool:
    """True quando a citação normalizada aparece nos segmentos sobrepostos ao intervalo."""
    if evidence_quote is None or source_start is None or source_end is None:
        return False
    if source_end < source_start:
        return False
    overlapping = [
        seg for seg in segments
        if seg.end > source_start and seg.start < source_end
    ]
    if not overlapping:
        return False
    combined = " ".join(seg.text for seg in overlapping)
    needle = " ".join(evidence_quote.split()).casefold()
    haystack = " ".join(combined.split()).casefold()
    return needle in haystack


def _action_item_from_dict(
    d: dict,
    segments: list[TranscriptSegment] | None = None,
) -> ActionItem:
    source_start = _parse_hms(d.get("source_start"))
    source_end = _parse_hms(d.get("source_end"))
    raw_quote = d.get("evidence_quote")
    evidence_quote = (
        raw_quote.strip() if isinstance(raw_quote, str) and raw_quote.strip() else None
    )
    explicitness = d.get("explicitness") or "inferred"
    if explicitness not in ("explicit", "inferred"):
        explicitness = "inferred"
    segs = segments or []
    confirmed = _validate_evidence(segs, source_start, source_end, evidence_quote)
    return ActionItem(
        what=d.get("what") or "",
        where=d.get("where") or None,
        details=d.get("details") or None,
        requested_by=d.get("requested_by") or None,
        priority=d.get("priority") or "media",
        assigned_to=_normalize_assigned_to(d.get("assigned_to")),
        source_start=source_start,
        source_end=source_end,
        evidence_quote=evidence_quote,
        explicitness=explicitness,
        review_status="confirmed" if confirmed else "needs_review",
    )


def _fact_from_dict(
    d: dict,
    kind: str,
    segments: list[TranscriptSegment] | None = None,
) -> MeetingFact:
    source_start = _parse_hms(d.get("source_start"))
    source_end = _parse_hms(d.get("source_end"))
    raw_quote = d.get("evidence_quote")
    evidence_quote = (
        raw_quote.strip() if isinstance(raw_quote, str) and raw_quote.strip() else None
    )
    explicitness = d.get("explicitness") or "inferred"
    if explicitness not in ("explicit", "inferred"):
        explicitness = "inferred"
    segs = segments or []
    confirmed = _validate_evidence(segs, source_start, source_end, evidence_quote)
    return MeetingFact(
        kind=kind,
        text=d.get("text") or "",
        source_start=source_start,
        source_end=source_end,
        evidence_quote=evidence_quote,
        explicitness=explicitness,
        review_status="confirmed" if confirmed else "needs_review",
    )


_TRUNCATION_RETRY_PROMPT = """\


RETENTATIVA APÓS LIMITE DE SAÍDA:
- Responda novamente desde o início com um único objeto JSON completo.
- Seja conciso em summary, text, what e details.
- Use a menor evidence_quote literal que ainda sustente cada item.
- Não repita nenhum fato ou tarefa.
- Termine todas as listas e feche o objeto JSON.
"""


def _complete_json(provider: LLMProvider, system: str, user: str) -> dict:
    for attempt in range(2):
        try:
            response = provider.complete(system, user)
        except LLMOutputTruncated as exc:
            if attempt == 0:
                system += _TRUNCATION_RETRY_PROMPT
                continue
            raise RuntimeError(
                "A resposta da LLM atingiu o limite de saída duas vezes. "
                "Reduza o conteúdo analisado ou use um modelo com maior limite."
            ) from exc
        try:
            return _parse_json_response(response)
        except ValueError:
            preview = response[:500].replace("\n", " ")
            raise ValueError(
                "A LLM retornou JSON inválido "
                f"({len(response)} caracteres). Início: {preview}"
            ) from None




def _normalize_match_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _item_key(item: dict) -> tuple[str, str]:
    return _normalize_match_text(item.get("what")), _normalize_match_text(
        item.get("where")
    )


def _source_ranges_overlap(left: dict, right: dict) -> bool:
    left_start = _parse_hms(left.get("source_start"))
    left_end = _parse_hms(left.get("source_end"))
    right_start = _parse_hms(right.get("source_start"))
    right_end = _parse_hms(right.get("source_end"))
    if None in {left_start, left_end, right_start, right_end}:
        return False
    return bool(left_start <= right_end and right_start <= left_end)


def _records_overlap(left: dict, right: dict, text_field: str) -> bool:
    left_text = _normalize_match_text(left.get(text_field))
    right_text = _normalize_match_text(right.get(text_field))
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    if not _source_ranges_overlap(left, right):
        return False
    left_terms = tuple(
        term for term in left_text.split() if term not in _MATCH_STOPWORDS
    )
    right_terms = tuple(
        term for term in right_text.split() if term not in _MATCH_STOPWORDS
    )
    return bool(left_terms and left_terms == right_terms)


def _deduplicate_action_items(items: list[dict]) -> list[dict]:
    """Une tarefas repetidas pelo overlap sem perder detalhes ou origem."""
    result: list[dict] = []
    priority_rank = {"baixa": 0, "media": 1, "alta": 2}

    for item in items:
        key = _item_key(item)
        duplicate = next(
            (
                existing
                for existing in result
                if _item_key(existing) == key
                or _records_overlap(existing, item, "what")
            ),
            None,
        )
        if not key[0] or duplicate is None:
            result.append(dict(item))
            continue

        if not duplicate.get("requested_by") and item.get("requested_by"):
            duplicate["requested_by"] = item["requested_by"]

        owners: list[str] = []
        seen_owners: set[str] = set()
        for raw in (duplicate.get("assigned_to"), item.get("assigned_to")):
            values = raw if isinstance(raw, list) else [raw]
            for owner in values:
                if not isinstance(owner, str) or not owner.strip():
                    continue
                normalized = owner.strip().casefold()
                if normalized not in seen_owners:
                    owners.append(owner.strip())
                    seen_owners.add(normalized)
        if owners:
            duplicate["assigned_to"] = owners

        if not duplicate.get("evidence_quote") and item.get("evidence_quote"):
            duplicate["evidence_quote"] = item["evidence_quote"]
        if duplicate.get("explicitness") != "explicit" and item.get("explicitness") == "explicit":
            duplicate["explicitness"] = "explicit"
        existing_details = duplicate.get("details")
        new_details = item.get("details")
        if not existing_details and new_details:
            duplicate["details"] = new_details
        elif (
            isinstance(existing_details, str)
            and isinstance(new_details, str)
            and existing_details.casefold().strip() != new_details.casefold().strip()
        ):
            duplicate["details"] = f"{existing_details.strip()}\n{new_details.strip()}"

        if priority_rank.get(item.get("priority"), -1) > priority_rank.get(
            duplicate.get("priority"), -1
        ):
            duplicate["priority"] = item["priority"]
        if item.get("source_start") and (
            not duplicate.get("source_start")
            or item["source_start"] < duplicate["source_start"]
        ):
            duplicate["source_start"] = item["source_start"]
        if item.get("source_end") and (
            not duplicate.get("source_end")
            or item["source_end"] > duplicate["source_end"]
        ):
            duplicate["source_end"] = item["source_end"]

    return result


def _deduplicate_facts(items: list[dict]) -> list[dict]:
    """Une fatos repetidos pelo overlap sem colapsar fatos distintos do trecho."""
    result: list[dict] = []
    for item in items:
        duplicate = next(
            (
                existing
                for existing in result
                if existing.get("kind") == item.get("kind")
                and _records_overlap(existing, item, "text")
            ),
            None,
        )
        if duplicate is None:
            result.append(dict(item))
            continue
        if len(str(item.get("text") or "")) > len(str(duplicate.get("text") or "")):
            duplicate["text"] = item["text"]
        if not duplicate.get("evidence_quote") and item.get("evidence_quote"):
            duplicate["evidence_quote"] = item["evidence_quote"]
        if duplicate.get("explicitness") != "explicit" and item.get("explicitness") == "explicit":
            duplicate["explicitness"] = "explicit"
        if item.get("source_start") and (
            not duplicate.get("source_start")
            or item["source_start"] < duplicate["source_start"]
        ):
            duplicate["source_start"] = item["source_start"]
        if item.get("source_end") and (
            not duplicate.get("source_end")
            or item["source_end"] > duplicate["source_end"]
        ):
            duplicate["source_end"] = item["source_end"]
    return result


def _analyse_chunks(
    provider: LLMProvider,
    segments: list[TranscriptSegment],
    participants: list[str],
    on_progress: ExtractionProgressCallback | None = None,
    visual_observations: list[dict] | None = None,
) -> dict:
    chunks = _split_transcript(segments)
    analyses: list[dict] = []
    participants_text = ", ".join(participants) if participants else "não identificados"
    operation_count = len(chunks) + 1  # blocos + consolidação final

    for chunk in chunks:
        block_number = chunk.index + 1
        if on_progress is not None:
            on_progress(
                chunk.index / operation_count,
                f"Analisando bloco {block_number} de {len(chunks)}",
            )
        visual_context = _render_visual_context(
            visual_observations or [], chunk.start, chunk.end
        )
        user = (
            f"BLOCO {block_number}/{len(chunks)} · intervalo absoluto "
            f"{_fmt_timestamp(chunk.start)}-{_fmt_timestamp(chunk.end)}\n"
            f"Participantes identificados: {participants_text}\n\n{chunk.text}"
            + (f"\n\nCONTEXTO VISUAL:\n{visual_context}" if visual_context else "")
        )
        analyses.append(
            {
                "chunk_index": block_number,
                "source_start": _fmt_timestamp(chunk.start),
                "source_end": _fmt_timestamp(chunk.end),
                "analysis": _complete_json(provider, _CHUNK_SYSTEM_PROMPT, user),
            }
        )
        if on_progress is not None:
            on_progress(
                block_number / operation_count,
                f"Bloco {block_number} de {len(chunks)} analisado",
            )

    facts: list[dict] = []
    action_items: list[dict] = []
    summary_chunks: list[dict] = []
    fact_lists = {
        "decisions": "decision",
        "requirements": "requirement",
        "constraints": "constraint",
        "open_questions": "open_question",
    }
    for entry in analyses:
        analysis = entry["analysis"]
        key_points: list[str] = []
        for field, kind in fact_lists.items():
            for raw_fact in analysis.get(field) or []:
                if not isinstance(raw_fact, dict) or not raw_fact.get("text"):
                    continue
                fact = dict(raw_fact)
                fact["kind"] = kind
                facts.append(fact)
                key_points.append(str(fact["text"]))
        task_texts: list[str] = []
        for raw_item in analysis.get("action_items") or []:
            if not isinstance(raw_item, dict) or not raw_item.get("what"):
                continue
            action_items.append(raw_item)
            task_texts.append(str(raw_item["what"]))
        summary_chunks.append(
            {
                "chunk_index": entry["chunk_index"],
                "chunk_summary": analysis.get("chunk_summary") or "",
                "key_points": key_points,
                "tasks": task_texts,
            }
        )

    if on_progress is not None:
        on_progress(None, "Consolidando análises dos blocos")
    system = _CONSOLIDATE_SYSTEM_PROMPT.replace(
        "__PARTICIPANTS__", participants_text
    )
    payload = json.dumps(
        {"chunk_count": len(summary_chunks), "chunks": summary_chunks},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    data = _complete_json(provider, system, payload)
    data["facts"] = _deduplicate_facts(facts)
    data["action_items"] = _deduplicate_action_items(action_items)
    if on_progress is not None:
        on_progress(1.0, "Resumo e tarefas consolidados")
    return data


_VALID_FACT_KINDS = frozenset({"decision", "requirement", "constraint", "open_question"})


def _result_from_data(
    data: dict,
    segments: list[TranscriptSegment] | None = None,
) -> tuple[str, list[ActionItem], str, list[MeetingFact]]:
    summary: str = data.get("summary") or ""
    title: str = data.get("title") or ""
    segs = segments or []

    # All action items — no personal filter; list_tasks/API handles that
    items_raw = data.get("action_items") or []
    action_items = [
        _action_item_from_dict(item, segs)
        for item in _deduplicate_action_items(
            [item for item in items_raw if isinstance(item, dict)]
        )
    ]

    # Facts: flat list with kind field
    facts_raw = data.get("facts") or []
    facts: list[MeetingFact] = [
        _fact_from_dict(f, f.get("kind", "decision"), segs)
        for f in facts_raw
        if isinstance(f, dict) and f.get("kind") in _VALID_FACT_KINDS
    ]

    return summary, action_items, title, facts


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def extract(
    segments: list[TranscriptSegment],
    participants: list[str],
    settings: Settings,
    on_progress: ExtractionProgressCallback | None = None,
    visual_observations: list[dict] | None = None,
) -> tuple[str, list[ActionItem], str, list[MeetingFact]]:
    """Extrai reunião curta em uma chamada; reunião longa via map-reduce temporal.

    Retorna (summary, action_items, title, facts). Todos os action_items são
    retornados, inclusive de terceiros; filtragem pessoal ocorre em store.list_tasks.
    """
    provider = get_provider(settings)
    transcript = _build_transcript(segments)

    duration = max((segment.end for segment in segments), default=0.0)
    if len(transcript) <= _MAX_TRANSCRIPT_CHARS and duration <= _MAX_SINGLE_CALL_SECONDS:
        if on_progress is not None:
            on_progress(None, "Gerando resumo e tarefas com LLM")
        part_str = ", ".join(participants) if participants else "não identificados"
        system = _SYSTEM_PROMPT.replace("__PARTICIPANTS__", part_str)
        visual_context = _render_visual_context(visual_observations or [], 0.0, duration)
        user = transcript + (
            f"\n\nCONTEXTO VISUAL:\n{visual_context}" if visual_context else ""
        )
        data = _complete_json(provider, system, user)
        if on_progress is not None:
            on_progress(1.0, "Resumo e tarefas gerados")
    else:
        data = _analyse_chunks(
            provider, segments, participants, on_progress, visual_observations
        )

    return _result_from_data(data, segments)
