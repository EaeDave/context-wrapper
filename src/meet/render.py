"""Renderização de MeetingResult para Markdown e geração de nome de arquivo."""

from __future__ import annotations

import re
import unicodedata

from .models import FACT_KIND_LABELS, FACT_KINDS, MeetingResult, VisualEvidence


def _fmt_hms(seconds: float) -> str:
    """Formata segundos como h:mm:ss (sem zero-pad no h)."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _fmt_duration(seconds: float) -> str:
    """Formata duração como h:mm."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}:{m:02d}"



def _fmt_source(start: float | None, end: float | None) -> str:
    if start is None:
        return "sem timestamp"
    if end is None or end <= start:
        return _fmt_hms(start)
    return f"{_fmt_hms(start)}–{_fmt_hms(end)}"


def _append_visual_context(lines: list[str], evidence: list[VisualEvidence]) -> None:
    for frame in evidence:
        lines.append(
            f"  - **Tela em {_fmt_hms(frame.timestamp)}:** {frame.description}"
        )
        if frame.visible_text:
            lines.append(f"    - **Texto visível:** {' · '.join(frame.visible_text)}")
        lines.append(f"    - **Relevância:** {frame.relevance}")


def _append_traceability(
    lines: list[str],
    *,
    source_start: float | None,
    source_end: float | None,
    evidence_quote: str | None,
    explicitness: str,
    review_status: str,
    visual_evidence: list[VisualEvidence],
) -> None:
    lines.append(f"  - **Trecho:** {_fmt_source(source_start, source_end)}")
    if evidence_quote:
        lines.append(f"  - **Evidência:** “{evidence_quote}”")
    lines.append(f"  - **Origem:** {explicitness}")
    lines.append(f"  - **Revisão:** {review_status}")
    _append_visual_context(lines, visual_evidence)



def to_markdown(result: MeetingResult) -> str:
    """Serializa MeetingResult como Markdown estruturado."""
    lines: list[str] = []

    # --- Cabeçalho ---
    lines.append(f"# {result.title}")
    lines.append("")
    lines.append(f"**Data:** {result.date}  ")
    lines.append(f"**Duração:** {_fmt_duration(result.duration)}  ")
    if result.participants:
        lines.append(f"**Participantes:** {', '.join(result.participants)}  ")
    lines.append("")

    # --- Resumo ---
    lines.append("## Resumo")
    lines.append("")
    lines.append(result.summary if result.summary else "_Sem resumo._")
    lines.append("")

    # --- Action items ---
    lines.append("## Action items")
    lines.append("")
    if result.action_items:
        for index, item in enumerate(result.action_items, start=1):
            lines.append(f"### {index}. {item.what}")
            lines.append("")
            lines.append(f"- **Status:** {item.status}")
            lines.append(f"- **Prioridade:** {item.priority}")
            if item.assigned_to:
                lines.append(f"- **Responsáveis:** {', '.join(item.assigned_to)}")
            if item.requested_by:
                lines.append(f"- **Pedido por:** {item.requested_by}")
            if item.where:
                lines.append(f"- **Onde:** {item.where}")
            if item.details:
                lines.append(f"- **Detalhes:** {item.details}")
            if item.due:
                lines.append(f"- **Prazo:** {item.due}")
            _append_traceability(
                lines,
                source_start=item.source_start,
                source_end=item.source_end,
                evidence_quote=item.evidence_quote,
                explicitness=item.explicitness,
                review_status=item.review_status,
                visual_evidence=item.visual_evidence,
            )
            lines.append("")
    else:
        lines.append("_Nenhum action item identificado._")
        lines.append("")

    # --- Fatos estruturados ---
    lines.append("## Fatos da reunião")
    lines.append("")
    if result.facts:
        known_kinds = list(FACT_KINDS)
        extra_kinds = sorted({fact.kind for fact in result.facts} - set(known_kinds))
        for kind in [*known_kinds, *extra_kinds]:
            facts = [fact for fact in result.facts if fact.kind == kind]
            if not facts:
                continue
            lines.append(f"### {FACT_KIND_LABELS.get(kind, kind)}")
            lines.append("")
            for fact in facts:
                lines.append(f"- **{fact.text}**")
                _append_traceability(
                    lines,
                    source_start=fact.source_start,
                    source_end=fact.source_end,
                    evidence_quote=fact.evidence_quote,
                    explicitness=fact.explicitness,
                    review_status=fact.review_status,
                    visual_evidence=fact.visual_evidence,
                )
                lines.append("")
    else:
        lines.append("_Nenhum fato estruturado identificado._")
        lines.append("")

    # --- Evidências visuais ---
    if result.visual_evidence:
        lines.append("## Evidências visuais")
        lines.append("")
        _append_visual_context(lines, result.visual_evidence)
        lines.append("")

    # --- Transcript ---
    lines.append("## Transcript")
    lines.append("")
    if result.segments:
        # Agrupa segmentos consecutivos do mesmo falante num parágrafo.
        # Cada grupo: [speaker, start_time, [texto, ...]]
        groups: list[list] = []
        for seg in result.segments:
            if groups and groups[-1][0] == seg.speaker:
                groups[-1][2].append(seg.text.strip())
            else:
                groups.append([seg.speaker, seg.start, [seg.text.strip()]])

        for group in groups:
            speaker: str | None = group[0]
            start: float = group[1]
            texts: list[str] = group[2]
            label = speaker or "Desconhecido"
            ts = _fmt_hms(start)
            combined = " ".join(texts)
            lines.append(f"**[{ts}] {label}:** {combined}")
            lines.append("")
    else:
        lines.append("_Sem segmentos._")
        lines.append("")

    return "\n".join(lines)


def meeting_filename(result: MeetingResult) -> str:
    """Gera nome de arquivo YYYY-MM-DD-slug-do-titulo.md.

    Slug: ASCII lowercase, hífens, sem acentos (NFKD + encode ascii ignore).
    """
    nfkd = unicodedata.normalize("NFKD", result.title)
    ascii_str = nfkd.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_str.lower()).strip("-")
    return f"{result.date}-{slug}.md"
