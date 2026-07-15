"""Extração de resumo e action items via LLM."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .models import ActionItem, TranscriptSegment

# Reuniões que cabem neste limite mantêm a chamada única existente.
_MAX_TRANSCRIPT_CHARS = 100_000
_CHUNK_TRANSCRIPT_CHARS = 40_000
_CHUNK_OVERLAP_CHARS = 4_000
ExtractionProgressCallback = Callable[[float | None, str], None]

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
  "action_items": [
    {
      "what": "<o que precisa ser feito>",
      "where": "<tela, endpoint, módulo, repositório — ou null se não aplicável>",
      "details": "<detalhes técnicos literais mencionados — ou null>",
      "requested_by": "<nome de quem pediu, ou null se não identificado>",
      "assigned_to": "<me se o dono destas notas participa da execução; nome/label de terceiro; ou null se não há responsável claro>",
      "priority": "<alta | media | baixa>"
    }
  ]
}

Participantes identificados: __PARTICIPANTS__

REGRA OBRIGATÓRIA DE RESPONSABILIDADE DOS ACTION ITEMS:
- O participante com label literal "me" é o dono destas notas.
- A lista é pessoal: inclua somente tarefas atribuídas a "me", tarefas compartilhadas que
  incluam "me", ou tarefas acionáveis cujo responsável realmente ficou indefinido.
- NÃO inclua tarefa atribuída explicitamente a outra pessoa ou equipe sem participação de
  "me". Isso vale mesmo que a tarefa seja importante ou tenha sido discutida em detalhes.
- Use "assigned_to": "me" quando "me" participa da execução. Use null somente quando o
  contexto não define responsável. Se identificar responsabilidade exclusiva de terceiro,
  use seu nome/label para classificar, mas omita esse item de action_items.
- "requested_by" é quem pediu a tarefa, não quem vai executá-la. Um terceiro pode pedir uma
  tarefa para "me"; nesse caso ela deve ser incluída.
- Resolva pronomes pelo falante e pelo contexto: se "me" diz "eu faço", a tarefa é de "me";
  se outro participante diz "eu faço", a tarefa é desse participante e deve ser omitida;
  se alguém atribui explicitamente algo a "você" referindo-se a "me", inclua.
- Se "me" não aparecer no transcript, não invente sua identidade. Ainda omita atribuições
  inequívocas a terceiros e mantenha como null somente o que de fato ficou sem dono claro.
"""

_CHUNK_SYSTEM_PROMPT = """\
Você analisa UM BLOCO temporal de uma reunião técnica. Retorne SOMENTE JSON válido.

Preserve literalmente nomes técnicos e classifique todos os candidatos, inclusive tarefas
atribuídas a terceiros: a consolidação global decidirá o que entra na lista pessoal. Resolva
"eu" usando o label do falante. Copie os timestamps do transcript para cada evidência.

Schema exato:
{
  "chunk_summary": "<síntese factual e concisa do bloco>",
  "decisions": [
    {"what": "<decisão>", "source_start": "<HH:MM:SS>", "source_end": "<HH:MM:SS>"}
  ],
  "requirements": [
    {"what": "<requisito>", "source_start": "<HH:MM:SS>", "source_end": "<HH:MM:SS>"}
  ],
  "action_items": [
    {
      "what": "<tarefa>",
      "where": "<local técnico ou null>",
      "details": "<detalhes literais ou null>",
      "requested_by": "<quem pediu ou null>",
      "assigned_to": "<me, nome/label de terceiro, lista de responsáveis, ou null>",
      "priority": "<alta | media | baixa>",
      "source_start": "<HH:MM:SS>",
      "source_end": "<HH:MM:SS>"
    }
  ]
}

Use null em assigned_to somente quando o bloco realmente não define responsável. Não invente
decisões, requisitos, tarefas, responsáveis ou timestamps.
"""

_CONSOLIDATE_SYSTEM_PROMPT = """\
Você consolida análises temporais de uma única reunião técnica. Retorne SOMENTE JSON válido.

Produza visão global coerente, preservando detalhes técnicos, decisões e requisitos de TODOS os
blocos. Resolva correções ou reatribuições posteriores pela ordem dos blocos. Remova duplicatas
sem perder detalhes e mantenha os timestamps de origem recebidos.

O JSON final deve seguir exatamente este schema:
{
  "title": "<título conciso em PT-BR>",
  "summary": "<resumo executivo em PT-BR, 3-6 frases, incluindo decisões e requisitos centrais>",
  "action_items": [
    {
      "what": "<o que precisa ser feito>",
      "where": "<tela, endpoint, módulo, repositório ou null>",
      "details": "<detalhes técnicos literais ou null>",
      "requested_by": "<quem pediu ou null>",
      "assigned_to": "<me, lista incluindo me, terceiro, ou null>",
      "priority": "<alta | media | baixa>",
      "source_start": "<HH:MM:SS>",
      "source_end": "<HH:MM:SS>"
    }
  ]
}

Participantes identificados: __PARTICIPANTS__

REGRA OBRIGATÓRIA DA LISTA PESSOAL:
- Inclua tarefas atribuídas a "me", compartilhadas que incluam "me" e tarefas realmente sem
  responsável claro.
- Exclua tarefas atribuídas explicitamente somente a terceiros.
- requested_by identifica quem pediu, nunca quem executará.
- Se "me" não existir entre os participantes, não invente sua identidade: mantenha somente
  tarefas sem dono claro e exclua atribuições inequívocas a terceiros.
"""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str: ...


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model or "claude-sonnet-5"

    def complete(self, system: str, user: str) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self._api_key)
        msg = client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text  # type: ignore[union-attr]


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
        return resp.choices[0].message.content or ""


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
        return resp.json()["message"]["content"]


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
            "max_tokens": 8192,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user}],
        }
        with httpx.Client(timeout=300) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
            )
            _check_response(resp)
        data = resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        if not parts:
            blocks = [b.get("type") for b in data.get("content", [])]
            raise RuntimeError(
                f"Resposta sem bloco de texto (stop_reason={data.get('stop_reason')}, "
                f"blocks={blocks})."
            )
        return "".join(parts)



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
        if not settings.openai_api_key:
            raise ValueError(
                "openai_api_key não configurado. "
                "Defina a variável de ambiente OPENAI_API_KEY "
                "ou adicione ao ~/.config/meet/config.toml."
            )
        return OpenAIProvider(settings.openai_api_key, model)

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
) -> list[TranscriptChunk]:
    """Divide em turnos inteiros; overlap repete contexto sem perder segmentos."""
    if max_chars <= 0:
        raise ValueError("max_chars deve ser positivo")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars deve estar entre 0 e max_chars")
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
            if end_index > start_index and size + addition > max_chars:
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
    """Extrai o primeiro objeto JSON da resposta do LLM.

    Tenta: bloco ```json ... ```, depois primeiro { até último }.
    Levanta ValueError com o texto bruto se não conseguir.
    """
    # Verifica se há fence ```json ou ```
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
def _action_item_is_for_owner(d: dict) -> bool:
    """Mantém tarefas do dono ou sem responsável; exclui terceiros explícitos."""
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


def _action_item_from_dict(d: dict) -> ActionItem:
    return ActionItem(
        what=d.get("what") or "",
        where=d.get("where") or None,
        details=d.get("details") or None,
        requested_by=d.get("requested_by") or None,
        priority=d.get("priority") or "media",
    )


def _complete_json(provider: LLMProvider, system: str, user: str) -> dict:
    response = provider.complete(system, user)
    try:
        return _parse_json_response(response)
    except ValueError:
        raise ValueError(response) from None


def _item_key(item: dict) -> tuple[str, str]:
    def normalize(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.casefold().split())

    return normalize(item.get("what")), normalize(item.get("where"))


def _deduplicate_action_items(items: list[dict]) -> list[dict]:
    """Une duplicatas exatas sem perder detalhes, urgência ou intervalo de origem."""
    result: list[dict] = []
    positions: dict[tuple[str, str], int] = {}
    priority_rank = {"baixa": 0, "media": 1, "alta": 2}

    for item in items:
        key = _item_key(item)
        if not key[0] or key not in positions:
            positions[key] = len(result)
            result.append(dict(item))
            continue

        existing = result[positions[key]]
        for field in ("requested_by", "assigned_to"):
            if not existing.get(field) and item.get(field):
                existing[field] = item[field]

        existing_details = existing.get("details")
        new_details = item.get("details")
        if not existing_details and new_details:
            existing["details"] = new_details
        elif (
            isinstance(existing_details, str)
            and isinstance(new_details, str)
            and existing_details.casefold().strip() != new_details.casefold().strip()
        ):
            existing["details"] = f"{existing_details.strip()}\n{new_details.strip()}"

        existing_priority = existing.get("priority")
        new_priority = item.get("priority")
        if priority_rank.get(new_priority, -1) > priority_rank.get(existing_priority, -1):
            existing["priority"] = new_priority

        new_start = item.get("source_start")
        if new_start and (
            not existing.get("source_start") or new_start < existing["source_start"]
        ):
            existing["source_start"] = new_start
        new_end = item.get("source_end")
        if new_end and (
            not existing.get("source_end") or new_end > existing["source_end"]
        ):
            existing["source_end"] = new_end

    return result


def _analyse_chunks(
    provider: LLMProvider,
    segments: list[TranscriptSegment],
    participants: list[str],
    on_progress: ExtractionProgressCallback | None = None,
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
        user = (
            f"BLOCO {block_number}/{len(chunks)} · intervalo absoluto "
            f"{_fmt_timestamp(chunk.start)}-{_fmt_timestamp(chunk.end)}\n"
            f"Participantes identificados: {participants_text}\n\n{chunk.text}"
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

    if on_progress is not None:
        on_progress(None, "Consolidando análises dos blocos")
    system = _CONSOLIDATE_SYSTEM_PROMPT.replace(
        "__PARTICIPANTS__", participants_text
    )
    payload = json.dumps(
        {"chunk_count": len(analyses), "chunks": analyses},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    data = _complete_json(provider, system, payload)
    if on_progress is not None:
        on_progress(1.0, "Resumo e tarefas consolidados")
    return data


def _result_from_data(data: dict) -> tuple[str, list[ActionItem], str]:
    summary: str = data.get("summary") or ""
    title: str = data.get("title") or ""
    items_raw = data.get("action_items") or []
    personal_items = [
        item
        for item in items_raw
        if isinstance(item, dict) and _action_item_is_for_owner(item)
    ]
    action_items = [
        _action_item_from_dict(item)
        for item in _deduplicate_action_items(personal_items)
    ]
    return summary, action_items, title


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def extract(
    segments: list[TranscriptSegment],
    participants: list[str],
    settings: Settings,
    on_progress: ExtractionProgressCallback | None = None,
) -> tuple[str, list[ActionItem], str]:
    """Extrai reunião curta em uma chamada; reunião longa via map-reduce temporal."""
    provider = get_provider(settings)
    transcript = _build_transcript(segments)

    if len(transcript) <= _MAX_TRANSCRIPT_CHARS:
        if on_progress is not None:
            on_progress(None, "Gerando resumo e tarefas com LLM")
        part_str = ", ".join(participants) if participants else "não identificados"
        system = _SYSTEM_PROMPT.replace("__PARTICIPANTS__", part_str)
        data = _complete_json(provider, system, transcript)
        if on_progress is not None:
            on_progress(1.0, "Resumo e tarefas gerados")
    else:
        data = _analyse_chunks(provider, segments, participants, on_progress)

    return _result_from_data(data)
