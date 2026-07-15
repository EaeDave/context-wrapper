"""Contratos de dados compartilhados por todo o pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Label reservado para a voz do próprio usuário (track de microfone do OBS).
ME = "me"


@dataclass
class Word:
    """Palavra com timestamps (faster-whisper word_timestamps)."""

    start: float
    end: float
    text: str


@dataclass
class TranscriptSegment:
    """Um trecho de fala transcrito."""

    start: float
    end: float
    text: str
    # None antes da diarização; depois "SPEAKER_00" ou nome resolvido ("Chefe", ME).
    speaker: str | None = None
    # Palavras com timestamps (transiente: usado só p/ atribuir falante por palavra
    # no merge; não é persistido no banco).
    words: list[Word] | None = None
    # id do banco (transiente — preenchido ao ler via get_meeting p/ a UI referenciar turnos).
    id: int | None = None


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
    where: str | None = None          # tela, endpoint, módulo, repositório...
    details: str | None = None        # detalhes técnicos literais mencionados
    requested_by: str | None = None
    priority: str = "media"           # "alta" | "media" | "baixa"
    id: int | None = None             # preenchido ao ler do banco
    status: str = "aberto"            # "aberto" | "feito"
    due: str | None = None            # "YYYY-MM-DD" ou None
    # Campos rastreáveis (opcionais; defaults retrocompatíveis)
    assigned_to: list[str] | None = None
    source_start: float | None = None
    source_end: float | None = None
    evidence_quote: str | None = None
    explicitness: str = "inferred"    # "explicit" | "inferred"
    review_status: str = "needs_review"  # "confirmed" | "needs_review"


@dataclass
class MeetingFact:
    """Fato estruturado extraído da reunião."""

    kind: str  # "decision" | "requirement" | "constraint" | "open_question"
    text: str
    source_start: float | None = None
    source_end: float | None = None
    evidence_quote: str | None = None
    explicitness: str = "inferred"       # "explicit" | "inferred"
    review_status: str = "needs_review"  # "confirmed" | "needs_review"
    id: int | None = None


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
    facts: list[MeetingFact] = field(default_factory=list)
    segments: list[TranscriptSegment] = field(default_factory=list)
    # nome_resolvido → score de cosseno; só entradas casadas (label≠nome)
    speaker_matches: dict[str, float] = field(default_factory=dict)
    project_id: int | None = None
