"""Contratos de dados compartilhados por todo o pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Label reservado para a voz do próprio usuário (track de microfone do OBS).
ME = "me"


@dataclass
class TranscriptSegment:
    """Um trecho de fala transcrito."""

    start: float
    end: float
    text: str
    # None antes da diarização; depois "SPEAKER_00" ou nome resolvido ("Chefe", ME).
    speaker: str | None = None


@dataclass
class SpeakerTurn:
    """Intervalo em que um falante esteve ativo, segundo a diarização."""

    start: float
    end: float
    label: str  # ex.: "SPEAKER_00"


@dataclass
class AudioTracks:
    """Wavs 16 kHz mono extraídos do arquivo de entrada.

    - mic: track do microfone do usuário (None se a gravação tem 1 track só).
    - others: participantes remotos (== mixed quando não há track separada).
    - mixed: mixdown completo, usado como fallback.
    """

    mic: Path | None
    others: Path
    mixed: Path
    duration: float


@dataclass
class ActionItem:
    """Tarefa acionável extraída da reunião."""

    what: str
    where: str | None = None  # tela, endpoint, módulo, repositório...
    details: str | None = None  # detalhes técnicos literais mencionados
    requested_by: str | None = None
    priority: str = "media"  # "alta" | "media" | "baixa"


@dataclass
class MeetingResult:
    """Resultado consolidado de uma reunião processada."""

    source: str
    date: str  # ISO YYYY-MM-DD
    title: str
    duration: float
    participants: list[str] = field(default_factory=list)
    summary: str = ""
    action_items: list[ActionItem] = field(default_factory=list)
    segments: list[TranscriptSegment] = field(default_factory=list)
