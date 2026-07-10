"""Fusão de transcrições e atribuição de falantes."""

from __future__ import annotations

import dataclasses

from .models import ME, SpeakerTurn, TranscriptSegment


def _best_label(start: float, end: float, turns: list[SpeakerTurn]) -> str:
    """Label do turn com maior sobreposição; sem sobreposição → mais próximo pelo centro."""
    best_label: str | None = None
    best_overlap = 0.0
    for turn in turns:
        overlap = max(0.0, min(end, turn.end) - max(start, turn.start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_label = turn.label
    if best_label is None:
        center = (start + end) / 2.0
        best_label = min(
            turns, key=lambda t: abs((t.start + t.end) / 2.0 - center)
        ).label
    return best_label


def _split_by_words(
    seg: TranscriptSegment, turns: list[SpeakerTurn]
) -> list[TranscriptSegment]:
    """Atribui falante palavra a palavra e reagrupa em sub-segmentos por falante.

    Resolve o caso em que o whisper produz um segmento longo cruzando vários
    falantes: sem isso, o segmento inteiro receberia um único label (o dominante)
    e os demais falantes sumiriam da transcrição.
    """
    runs: list[list] = []  # [label, start, end, [text, …]]
    for w in seg.words or []:
        label = _best_label(w.start, w.end, turns)
        if runs and runs[-1][0] == label:
            runs[-1][2] = w.end
            runs[-1][3].append(w.text)
        else:
            runs.append([label, w.start, w.end, [w.text]])

    out: list[TranscriptSegment] = []
    for label, start, end, texts in runs:
        text = "".join(texts).strip()
        if text:
            out.append(
                TranscriptSegment(start=start, end=end, text=text, speaker=label)
            )
    # Degenerado (palavras sem texto útil): cai no comportamento por segmento.
    if not out:
        return [
            dataclasses.replace(
                seg, speaker=_best_label(seg.start, seg.end, turns), words=None
            )
        ]
    return out


def assign_speakers(
    segments: list[TranscriptSegment],
    turns: list[SpeakerTurn],
) -> list[TranscriptSegment]:
    """Atribui falante a cada segmento.

    Com word timestamps, divide segmentos que cruzam falantes em sub-segmentos
    (atribuição por palavra + reagrupamento). Sem words, atribui um único label
    por segmento pelo maior overlap temporal (turn mais próximo se não houver).
    Lista de turns vazia → segmentos inalterados.
    """
    if not turns:
        return segments

    result: list[TranscriptSegment] = []
    for seg in segments:
        if seg.words:
            result.extend(_split_by_words(seg, turns))
        else:
            result.append(
                dataclasses.replace(
                    seg, speaker=_best_label(seg.start, seg.end, turns)
                )
            )
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
