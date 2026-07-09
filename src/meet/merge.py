"""Fusão de transcrições e atribuição de falantes."""

from __future__ import annotations

import dataclasses

from .models import ME, SpeakerTurn, TranscriptSegment


def assign_speakers(
    segments: list[TranscriptSegment],
    turns: list[SpeakerTurn],
) -> list[TranscriptSegment]:
    """Atribui falante a cada segmento pelo turn com maior sobreposição temporal.

    Sem sobreposição → turn mais próximo pelo centro do intervalo.
    Lista de turns vazia → speaker permanece None.
    """
    if not turns:
        return segments

    result: list[TranscriptSegment] = []
    for seg in segments:
        best_label: str | None = None
        best_overlap = 0.0

        for turn in turns:
            overlap = max(0.0, min(seg.end, turn.end) - max(seg.start, turn.start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = turn.label

        if best_overlap == 0.0:
            # Sem sobreposição: turn mais próximo por distância entre centros.
            seg_center = (seg.start + seg.end) / 2.0
            nearest = min(turns, key=lambda t: abs((t.start + t.end) / 2.0 - seg_center))
            best_label = nearest.label

        result.append(dataclasses.replace(seg, speaker=best_label))

    return result


def combine(
    mine: list[TranscriptSegment],
    others: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    """Marca transcrição própria com ME, junta com os outros e ordena por start."""
    tagged_mine = [dataclasses.replace(s, speaker=ME) for s in mine]
    merged = tagged_mine + list(others)
    return sorted(merged, key=lambda s: s.start)


def rename_speakers(
    segments: list[TranscriptSegment],
    mapping: dict[str, str],
) -> list[TranscriptSegment]:
    """Aplica dict label→nome a todos os segmentos; speakers ausentes do dict ficam inalterados."""
    result: list[TranscriptSegment] = []
    for seg in segments:
        if seg.speaker is not None and seg.speaker in mapping:
            result.append(dataclasses.replace(seg, speaker=mapping[seg.speaker]))
        else:
            result.append(seg)
    return result
