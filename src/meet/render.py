"""Renderização de MeetingResult para Markdown e geração de nome de arquivo."""

from __future__ import annotations

import re
import unicodedata

from .models import MeetingResult


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


def _esc_pipe(text: str) -> str:
    """Escapa barras verticais para não quebrar tabelas Markdown."""
    return text.replace("|", "\\|")


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
        lines.append("| O quê | Onde | Detalhes | Pedido por | Prioridade |")
        lines.append("|-------|------|----------|------------|------------|")
        for item in result.action_items:
            what = _esc_pipe(item.what or "")
            where = _esc_pipe(item.where or "")
            details = _esc_pipe(item.details or "")
            req = _esc_pipe(item.requested_by or "")
            prio = _esc_pipe(item.priority or "media")
            lines.append(f"| {what} | {where} | {details} | {req} | {prio} |")
    else:
        lines.append("_Nenhum action item identificado._")
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
