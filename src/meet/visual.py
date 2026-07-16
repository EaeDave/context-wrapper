"""Seleção econômica de frames para enriquecer a análise de reuniões."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import TranscriptSegment

_MAX_FRAMES = 24
_FALLBACK_INTERVAL_SECONDS = 120.0
_TRIGGER_OFFSETS = (-3.0, 0.0, 3.0)
_TRIGGER_RE = re.compile(
    r"\b(?:"
    r"aqui (?:na|nessa|nesta) tela|olha (?:isso|aqui)|vou mostrar|"
    r"quando (?:eu )?(?:clico|clicar|salvo|salvar|abro|abrir)|"
    r"esse (?:bot[aã]o|campo|erro|modal|relat[oó]rio|fluxo)|"
    r"essa (?:tela|mensagem|p[aá]gina|tabela)|"
    r"est[aá] aparecendo|na tela|compartilhando (?:a |minha )?tela"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VisualFrame:
    """Frame temporário associado a um instante absoluto da reunião."""

    timestamp: float
    path: Path


@dataclass(frozen=True)
class VisualObservation:
    """Descrição rastreável produzida pela LLM a partir de um frame."""

    timestamp: float
    description: str
    visible_text: tuple[str, ...] = ()
    relevance: str = "medium"


def candidate_timestamps(
    segments: list[TranscriptSegment],
    duration: float,
    *,
    max_frames: int = _MAX_FRAMES,
    fallback_interval: float = _FALLBACK_INTERVAL_SECONDS,
) -> list[float]:
    """Combina janelas de fala visual com amostragem periódica de segurança."""
    if duration <= 0 or max_frames <= 0:
        return []

    triggered: list[float] = []
    for segment in segments:
        if not _TRIGGER_RE.search(segment.text):
            continue
        anchor = max(segment.start, 0.0)
        triggered.extend(anchor + offset for offset in _TRIGGER_OFFSETS)

    fallback: list[float] = []
    if fallback_interval > 0:
        point = min(fallback_interval / 2.0, duration / 2.0)
        while point < duration:
            fallback.append(point)
            point += fallback_interval

    # Gatilhos têm prioridade; o fallback preenche o orçamento restante.
    ordered = triggered + fallback
    unique: list[float] = []
    for timestamp in ordered:
        timestamp = min(max(timestamp, 0.0), max(duration - 0.05, 0.0))
        if any(abs(timestamp - previous) < 1.0 for previous in unique):
            continue
        unique.append(timestamp)
        if len(unique) >= max_frames:
            break
    return sorted(unique)


def _visual_signature(video: Path, timestamp: float) -> bytes:
    """Retorna miniatura 32×32 cinza; suficiente para remover telas repetidas."""
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=32:32,format=gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=True, timeout=30)
    return result.stdout


def _difference(left: bytes, right: bytes) -> float:
    if not left or len(left) != len(right):
        return 1.0
    return sum(abs(a - b) for a, b in zip(left, right)) / (255 * len(left))


def _write_frame(video: Path, timestamp: float, output: Path) -> None:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale='min(1280,iw)':-2",
        "-q:v",
        "4",
        "-y",
        str(output),
    ]
    subprocess.run(command, capture_output=True, check=True, timeout=30)


def extract_relevant_frames(
    video: Path,
    segments: list[TranscriptSegment],
    duration: float,
    output_dir: Path,
    *,
    max_frames: int = _MAX_FRAMES,
    difference_threshold: float = 0.035,
) -> list[VisualFrame]:
    """Extrai frames candidatos; falha pontual nunca interrompe a reunião."""
    timestamps = candidate_timestamps(segments, duration, max_frames=max_frames)
    if not timestamps:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[VisualFrame] = []
    signatures: list[bytes] = []
    for timestamp in timestamps:
        try:
            signature = _visual_signature(video, timestamp)
            if not signature or any(
                _difference(signature, previous) < difference_threshold
                for previous in signatures
            ):
                continue
            digest = hashlib.sha1(f"{timestamp:.3f}".encode()).hexdigest()[:10]
            path = output_dir / f"frame-{digest}.jpg"
            _write_frame(video, timestamp, path)
        except (OSError, subprocess.SubprocessError):
            continue
        if path.is_file() and path.stat().st_size > 0:
            frames.append(VisualFrame(timestamp=timestamp, path=path))
            signatures.append(signature)
    return frames
