"""Helpers de rastreabilidade (evidência vs transcript) — sem dependência de LLM."""

from __future__ import annotations

from .models import TranscriptSegment


def validate_evidence(
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
        seg
        for seg in segments
        if seg.end > source_start and seg.start < source_end
    ]
    if not overlapping:
        return False
    combined = " ".join(seg.text for seg in overlapping)
    needle = " ".join(evidence_quote.split()).casefold()
    haystack = " ".join(combined.split()).casefold()
    return needle in haystack
