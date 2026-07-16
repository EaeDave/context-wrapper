"""Extração de resumo e action items via LLM."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .llm_providers import (
    AnthropicOAuthProvider,
    AnthropicProvider,
    ClaudeCodeProvider,
    ImageContent,
    LLMOutputTruncated,
    LLMProvider,
    OllamaProvider,
    OpenAIOAuthProvider,
    OpenAIProvider,
    get_provider,
    validate_credentials,
)
from .models import ActionItem, MeetingFact, TranscriptCorrection, TranscriptSegment
from .traceability import validate_evidence as _validate_evidence

# Re-export público (testes e pipeline importam de meet.extract).
__all__ = [
    "AnthropicOAuthProvider",
    "AnthropicProvider",
    "ClaudeCodeProvider",
    "ImageContent",
    "LLMOutputTruncated",
    "LLMProvider",
    "OllamaProvider",
    "OpenAIOAuthProvider",
    "OpenAIProvider",
    "extract",
    "get_provider",
    "validate_credentials",
]

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


_TRANSCRIPT_VOCABULARY_SYSTEM = """\
Identifique termos canônicos técnicos ou de domínio que aparecem LITERALMENTE
nestes segmentos: siglas, identificadores, produtos, métodos, formatos e termos
operacionais específicos. Não corrija nada e não invente termos.
Retorne SOMENTE JSON válido: {"terms":["DUN14","USRMoveColetor"]}.
Inclua no máximo 30 termos que seriam úteis para reconhecer erros fonéticos em
outros trechos da mesma reunião.
"""


_TRANSCRIPT_NORMALIZATION_SYSTEM = """\
Você corrige somente erros fonéticos evidentes de ASR em transcrições técnicas.
Não reescreva estilo, gramática ou pontuação. Não resuma, complete, remova ou
reordene fala. Preserve números e intenção. Use termos recorrentes na própria
reunião e no contexto fornecido. Em dúvida, não corrija.

Retorne SOMENTE JSON válido:
{"corrections":[{"index":0,"original":"trecho literal exato","corrected":"termo correto","confidence":0.98,"reason":"evidência curta"}]}
Inclua apenas confiança >= 0.90. `original` deve existir literalmente no segmento.
"""


def _normalization_context(
    project_name: str | None,
    project_description: str | None,
    learned_terms: list[str],
) -> str:
    lines: list[str] = []
    if project_name:
        lines.append(f"Projeto: {project_name}")
    if project_description:
        lines.append(f"Descrição: {project_description}")
    if learned_terms:
        lines.append("Termos aprendidos em reuniões anteriores: " + ", ".join(learned_terms))
    return "\n".join(lines) or "Sem contexto anterior; use somente a própria reunião."


def _normalization_batches(
    segments: list[TranscriptSegment], max_chars: int = 10_000
) -> list[list[tuple[int, TranscriptSegment]]]:
    batches: list[list[tuple[int, TranscriptSegment]]] = []
    current: list[tuple[int, TranscriptSegment]] = []
    size = 0
    for index, segment in enumerate(segments):
        addition = len(segment.text) + 32
        if current and size + addition > max_chars:
            batches.append(current)
            current = []
            size = 0
        current.append((index, segment))
        size += addition
    if current:
        batches.append(current)
    return batches


def _replace_literal_once(text: str, original: str, corrected: str) -> str | None:
    start = text.find(original)
    if start < 0 or text.find(original, start + len(original)) >= 0:
        return None
    return text[:start] + corrected + text[start + len(original) :]


def _discover_meeting_terms(
    provider: LLMProvider,
    batches: list[list[tuple[int, TranscriptSegment]]],
) -> list[str]:
    """Une vocabulário literal descoberto em todos os blocos da reunião."""
    transcript = "\n".join(segment.text for batch in batches for _, segment in batch)
    transcript_folded = transcript.casefold()
    terms: list[str] = []
    seen: set[str] = set()
    for batch in batches:
        payload = [segment.text for _, segment in batch]
        try:
            data = _parse_json_response(
                provider.complete(
                    _TRANSCRIPT_VOCABULARY_SYSTEM,
                    json.dumps(payload, ensure_ascii=False),
                )
            )
        except (ValueError, RuntimeError, httpx.HTTPError):
            continue
        for raw in data.get("terms") or []:
            term = str(raw).strip()
            key = term.casefold()
            if (
                not term
                or len(term) > 80
                or key in seen
                or key not in transcript_folded
            ):
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= 80:
                return terms
    return terms


def normalize_transcript(
    segments: list[TranscriptSegment],
    settings: Settings,
    *,
    project_name: str | None = None,
    project_description: str | None = None,
    learned_terms: list[str] | None = None,
    provider: LLMProvider | None = None,
    on_progress: ExtractionProgressCallback | None = None,
) -> list[TranscriptSegment]:
    """Corrige erros fonéticos de alta confiança; falha preserva o ASR bruto."""
    if not segments:
        return segments
    provider = provider or get_provider(settings)
    batches = _normalization_batches(segments)
    meeting_terms = _discover_meeting_terms(provider, batches)
    all_terms = list(learned_terms or [])
    known = {term.casefold() for term in all_terms}
    all_terms.extend(
        term for term in meeting_terms if term.casefold() not in known
    )
    context = _normalization_context(
        project_name, project_description, all_terms
    )
    corrected_segments = [
        TranscriptSegment(
            start=segment.start,
            end=segment.end,
            text=segment.text,
            speaker=segment.speaker,
            words=segment.words,
            original_text=segment.original_text,
            corrections=list(segment.corrections),
            id=segment.id,
        )
        for segment in segments
    ]

    for batch_number, batch in enumerate(batches, start=1):
        payload = [
            {"index": index, "speaker": segment.speaker, "text": segment.text}
            for index, segment in batch
        ]
        prompt = (
            f"Contexto automático:\n{context}\n\n"
            "Segmentos desta reunião:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            data = _parse_json_response(
                provider.complete(_TRANSCRIPT_NORMALIZATION_SYSTEM, prompt)
            )
        except (ValueError, RuntimeError, httpx.HTTPError):
            continue
        allowed_indexes = {index for index, _segment in batch}
        for raw in data.get("corrections") or []:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw["index"])
                confidence = float(raw["confidence"])
            except (KeyError, TypeError, ValueError):
                continue
            original = str(raw.get("original") or "").strip()
            corrected = str(raw.get("corrected") or "").strip()
            reason = str(raw.get("reason") or "Contexto da reunião").strip()
            if (
                index not in allowed_indexes
                or confidence < 0.90
                or not original
                or not corrected
                or original.casefold() == corrected.casefold()
                or re.findall(r"\d+", original) != re.findall(r"\d+", corrected)
                or len(corrected) > max(4 * len(original), len(original) + 40)
            ):
                continue
            segment = corrected_segments[index]
            replaced = _replace_literal_once(segment.text, original, corrected)
            if replaced is None:
                continue
            if segment.original_text is None:
                segment.original_text = segment.text
            segment.text = replaced
            segment.corrections.append(
                TranscriptCorrection(original, corrected, confidence, reason)
            )
        if on_progress is not None:
            on_progress(
                batch_number / len(batches),
                f"Revisando transcrição · bloco {batch_number} de {len(batches)}",
            )
    return corrected_segments


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
