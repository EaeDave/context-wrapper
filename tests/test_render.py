"""Tests for meet.render — Markdown generation and filename slugging.

Contracts defended:
- to_markdown groups CONSECUTIVE same-speaker segments into one paragraph.
- Non-consecutive repetitions of the same speaker produce separate paragraphs.
- Action items include every persisted field and traceability evidence.
- Facts are grouped as decisions, requirements, constraints, and open questions.
- Visual evidence is rendered as textual context for another LLM.
- Empty collections produce explicit sentinel lines.
- meeting_filename converts accents to ASCII, spaces to hyphens, and prefixes the date.
"""

from __future__ import annotations

import pytest

from meet.models import ActionItem, MeetingFact, MeetingResult, TranscriptSegment, VisualEvidence
from meet.render import meeting_filename, to_markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(
    *,
    title: str = "Test Meeting",
    date: str = "2024-06-01",
    duration: float = 3600.0,
    summary: str = "Summary.",
    participants: list[str] | None = None,
    segments: list[TranscriptSegment] | None = None,
    action_items: list[ActionItem] | None = None,
    facts: list[MeetingFact] | None = None,
    visual_evidence: list[VisualEvidence] | None = None,
) -> MeetingResult:
    return MeetingResult(
        source="test.mkv",
        date=date,
        title=title,
        duration=duration,
        summary=summary,
        participants=participants or [],
        segments=segments or [],
        action_items=action_items or [],
        facts=facts or [],
        visual_evidence=visual_evidence or [],
    )


def _seg(start: float, end: float, text: str, speaker: str | None = None) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text, speaker=speaker)


# ---------------------------------------------------------------------------
# to_markdown — speaker grouping
# ---------------------------------------------------------------------------

def test_to_markdown_consecutive_same_speaker_grouped() -> None:
    """Two consecutive segments from Alice should appear in a single **[ts] Alice:** line."""
    segs = [
        _seg(0, 1, "Hello", "Alice"),
        _seg(1, 2, "World", "Alice"),
    ]
    md = to_markdown(_result(segments=segs))
    # Only one Alice header; combined text appears together
    alice_lines = [l for l in md.splitlines() if "Alice:" in l]
    assert len(alice_lines) == 1
    assert "Hello" in alice_lines[0]
    assert "World" in alice_lines[0]


def test_to_markdown_non_consecutive_same_speaker_split() -> None:
    """Alice → Bob → Alice should produce TWO separate Alice paragraphs."""
    segs = [
        _seg(0, 1, "Part one", "Alice"),
        _seg(1, 2, "Interruption", "Bob"),
        _seg(2, 3, "Part two", "Alice"),
    ]
    md = to_markdown(_result(segments=segs))
    alice_lines = [l for l in md.splitlines() if "Alice:" in l]
    assert len(alice_lines) == 2


def test_to_markdown_timestamp_uses_first_segment_in_group() -> None:
    """The timestamp shown is that of the FIRST segment of the group."""
    segs = [
        _seg(60, 65, "Hello", "Alice"),   # h:mm:ss → 0:01:00
        _seg(65, 70, "World", "Alice"),
    ]
    md = to_markdown(_result(segments=segs))
    assert "0:01:00" in md


def test_to_markdown_unknown_speaker_shown_as_desconhecido() -> None:
    """A segment with speaker=None renders as 'Desconhecido'."""
    segs = [_seg(0, 1, "???", speaker=None)]
    md = to_markdown(_result(segments=segs))
    assert "Desconhecido" in md


# ---------------------------------------------------------------------------
# to_markdown — action items table
# ---------------------------------------------------------------------------

def test_to_markdown_action_items_include_all_context_fields() -> None:
    frame = VisualEvidence(
        timestamp=70,
        image_path="frame.jpg",
        description="Tela de configurações aberta",
        visible_text=["Salvar", "Modelo"],
        relevance="high",
    )
    item = ActionItem(
        what="Corrigir integração",
        where="Backend",
        details="Preservar compatibilidade",
        requested_by="Cliente",
        priority="alta",
        status="aberto",
        due="2026-08-01",
        assigned_to=["me", "Igor"],
        source_start=65,
        source_end=75,
        evidence_quote="Precisamos corrigir isso",
        explicitness="explicit",
        review_status="confirmed",
        visual_evidence=[frame],
    )

    md = to_markdown(_result(action_items=[item]))

    for expected in (
        "## Action items",
        "Corrigir integração",
        "**Status:** aberto",
        "**Prioridade:** alta",
        "**Responsáveis:** me, Igor",
        "**Pedido por:** Cliente",
        "**Onde:** Backend",
        "**Detalhes:** Preservar compatibilidade",
        "**Prazo:** 2026-08-01",
        "**Trecho:** 0:01:05–0:01:15",
        "**Evidência:** “Precisamos corrigir isso”",
        "**Origem:** explicit",
        "**Revisão:** confirmed",
        "**Tela em 0:01:10:** Tela de configurações aberta",
        "**Texto visível:** Salvar · Modelo",
    ):
        assert expected in md


def test_to_markdown_empty_action_items_fallback() -> None:
    md = to_markdown(_result(action_items=[]))
    assert "_Nenhum action item identificado._" in md


def test_to_markdown_groups_every_fact_kind_with_traceability() -> None:
    facts = [
        MeetingFact("decision", "Usar fila local", 10, 12, "Vamos usar fila", "explicit", "confirmed"),
        MeetingFact("requirement", "Aceitar arquivos MKV"),
        MeetingFact("constraint", "Executar sem internet"),
        MeetingFact("open_question", "Qual modelo usar?"),
    ]

    md = to_markdown(_result(facts=facts))

    assert "## Fatos da reunião" in md
    for heading, text in (
        ("### Decisões", "Usar fila local"),
        ("### Requisitos", "Aceitar arquivos MKV"),
        ("### Restrições", "Executar sem internet"),
        ("### Questões em aberto", "Qual modelo usar?"),
    ):
        assert heading in md
        assert text in md
    assert "**Trecho:** 0:00:10–0:00:12" in md
    assert "**Evidência:** “Vamos usar fila”" in md


def test_to_markdown_empty_facts_fallback() -> None:
    assert "_Nenhum fato estruturado identificado._" in to_markdown(_result())


# ---------------------------------------------------------------------------
# to_markdown — empty segments
# ---------------------------------------------------------------------------

def test_to_markdown_empty_segments_fallback() -> None:
    """When there are no segments the sentinel line is present."""
    md = to_markdown(_result(segments=[]))
    assert "_Sem segmentos._" in md


# ---------------------------------------------------------------------------
# to_markdown — structural invariants
# ---------------------------------------------------------------------------

def test_to_markdown_contains_title() -> None:
    md = to_markdown(_result(title="Sprint Review"))
    assert "Sprint Review" in md


def test_to_markdown_contains_resumo_section() -> None:
    md = to_markdown(_result())
    assert "## Resumo" in md


def test_to_markdown_contains_transcript_section() -> None:
    md = to_markdown(_result())
    assert "## Transcript" in md


def test_to_markdown_duration_formatted_h_mm() -> None:
    """Duration of 3600s → '1:00', 3660s → '1:01'."""
    md1 = to_markdown(_result(duration=3600.0))
    assert "1:00" in md1

    md2 = to_markdown(_result(duration=3660.0))
    assert "1:01" in md2


# ---------------------------------------------------------------------------
# meeting_filename
# ---------------------------------------------------------------------------

def test_meeting_filename_basic() -> None:
    r = _result(date="2024-01-15", title="Planning Meeting")
    assert meeting_filename(r) == "2024-01-15-planning-meeting.md"


def test_meeting_filename_accents_to_ascii() -> None:
    """Accented characters (ã, é, ü …) are converted to their ASCII base."""
    r = _result(date="2024-03-10", title="Reunião de Planejamento")
    fname = meeting_filename(r)
    # 'ã' → 'a', so 'Reunião' → 'reuniao'
    assert "reuniao" in fname
    assert fname.endswith(".md")


def test_meeting_filename_uppercase_to_lowercase() -> None:
    r = _result(date="2024-06-01", title="SPRINT REVIEW")
    assert "sprint-review" in meeting_filename(r)


def test_meeting_filename_spaces_become_hyphens() -> None:
    r = _result(date="2024-06-01", title="a b c")
    assert "a-b-c" in meeting_filename(r)


def test_meeting_filename_special_chars_removed() -> None:
    """Non-alphanumeric chars like !@#$ produce a clean slug."""
    r = _result(date="2024-06-01", title="Hello! World?")
    fname = meeting_filename(r)
    assert "hello" in fname
    assert "world" in fname
    # Punctuation must not bleed into the slug
    assert "!" not in fname
    assert "?" not in fname


def test_meeting_filename_starts_with_date() -> None:
    r = _result(date="2025-12-31", title="Any Title")
    assert meeting_filename(r).startswith("2025-12-31-")


def test_meeting_filename_ends_with_md() -> None:
    r = _result(date="2025-01-01", title="Test")
    assert meeting_filename(r).endswith(".md")


def test_meeting_filename_no_leading_or_trailing_hyphens_in_slug() -> None:
    """Leading/trailing punctuation in title must not produce dangling hyphens in slug."""
    r = _result(date="2024-01-01", title="!!Hello World!!")
    fname = meeting_filename(r)
    # The slug part (after date and first hyphen) must not start/end with '-'
    slug = fname.removeprefix("2024-01-01-").removesuffix(".md")
    assert not slug.startswith("-")
    assert not slug.endswith("-")
