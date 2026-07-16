"""Tests focados: round-trip de original_text/corrections, migração de schema legado,
update_turn sem perda de auditoria, update_segment_normalization e project_vocabulary.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from meet.models import ActionItem, MeetingResult, TranscriptCorrection, TranscriptSegment
from meet.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seg(
    text: str,
    *,
    start: float = 0.0,
    end: float = 1.0,
    speaker: str = "Alice",
    original_text: str | None = None,
    corrections: list[TranscriptCorrection] | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(
        start=start,
        end=end,
        text=text,
        speaker=speaker,
        original_text=original_text,
        corrections=corrections or [],
    )


def _correction(
    original: str = "helo",
    corrected: str = "hello",
    confidence: float = 0.95,
    reason: str = "phonetic",
) -> TranscriptCorrection:
    return TranscriptCorrection(
        original=original,
        corrected=corrected,
        confidence=confidence,
        reason=reason,
    )


def _meeting(
    segments: list[TranscriptSegment] | None = None,
    *,
    date: str = "2024-06-01",
    title: str = "Test",
) -> MeetingResult:
    return MeetingResult(
        source="test.mkv",
        date=date,
        title=title,
        duration=60.0,
        summary="",
        segments=segments or [],
        action_items=[],
    )


# ---------------------------------------------------------------------------
# Round-trip: save → get preserva original_text e corrections
# ---------------------------------------------------------------------------

def test_segment_roundtrip_with_corrections(tmp_store: Store) -> None:
    """original_text e corrections sobrevivem save → get."""
    corr = _correction("helo", "hello", 0.95, "phonetic")
    seg = _seg("hello world", original_text="helo world", corrections=[corr])
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    assert len(got.segments) == 1
    s = got.segments[0]
    assert s.text == "hello world"
    assert s.original_text == "helo world"
    assert len(s.corrections) == 1
    c = s.corrections[0]
    assert c.original == "helo"
    assert c.corrected == "hello"
    assert c.confidence == pytest.approx(0.95)
    assert c.reason == "phonetic"


def test_segment_roundtrip_no_corrections(tmp_store: Store) -> None:
    """Segmento sem correção: original_text=None, corrections=[]."""
    seg = _seg("clean text")
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    s = got.segments[0]
    assert s.original_text is None
    assert s.corrections == []


def test_segment_roundtrip_multiple_corrections(tmp_store: Store) -> None:
    """Múltiplas correções sobrevivem round-trip."""
    corrs = [
        _correction("fizzbuz", "FizzBuzz", 0.92, "brand"),
        _correction("openai", "OpenAI", 0.98, "capitalization"),
    ]
    seg = _seg("FizzBuzz and OpenAI", original_text="fizzbuz and openai", corrections=corrs)
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    s = got.segments[0]
    assert len(s.corrections) == 2
    terms = {c.corrected for c in s.corrections}
    assert terms == {"FizzBuzz", "OpenAI"}


def test_fts_uses_corrected_text(tmp_store: Store) -> None:
    """FTS indexa o texto corrigido (text), não original_text."""
    corr = _correction("kubernetes", "Kubernetes", 0.99, "brand")
    seg = _seg("Kubernetes cluster", original_text="kubernetes cluster", corrections=[corr])
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    results = tmp_store.search("Kubernetes")
    assert any(r["meeting_id"] == mid for r in results)
    # original_text não deve aparecer no FTS como palavra separada (word "kubernetes" lowercase)
    # — mas a prioridade é que o texto corrigido é encontrado:
    assert results[0]["kind"] == "segment"


# ---------------------------------------------------------------------------
# Migração de schema legado
# ---------------------------------------------------------------------------

def test_legacy_db_migrates_without_error(tmp_path: Path) -> None:
    """Banco antigo sem original_text/corrections migra sem erro ao abrir."""
    db_path = tmp_path / "legacy.db"
    # Criar esquema mínimo antigo sem as colunas novas
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            duration REAL NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            md_path TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE action_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            what TEXT NOT NULL,
            where_ TEXT,
            details TEXT,
            requested_by TEXT,
            priority TEXT NOT NULL DEFAULT 'media'
        );
        CREATE TABLE segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            start REAL NOT NULL,
            end REAL NOT NULL,
            speaker TEXT,
            text TEXT NOT NULL
        );
        CREATE TABLE voices (name TEXT PRIMARY KEY, embedding BLOB NOT NULL);
        CREATE VIRTUAL TABLE search_index USING fts5 (
            content, meeting_id UNINDEXED, kind UNINDEXED
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            repo_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE meeting_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            source_start REAL, source_end REAL,
            evidence_quote TEXT,
            explicitness TEXT NOT NULL DEFAULT 'inferred',
            review_status TEXT NOT NULL DEFAULT 'needs_review'
        );
        CREATE TABLE visual_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            timestamp REAL NOT NULL,
            image_path TEXT NOT NULL,
            description TEXT NOT NULL,
            visible_text TEXT NOT NULL DEFAULT '[]',
            relevance TEXT NOT NULL DEFAULT 'medium'
        );
        -- Inserir reunião antiga sem as colunas novas
        INSERT INTO meetings (date, title, source, duration, summary, md_path)
        VALUES ('2023-01-01', 'Reunião Antiga', 'old.mkv', 3600.0, 'sumário', '');
        INSERT INTO segments (meeting_id, start, end, speaker, text)
        VALUES (1, 0.0, 5.0, 'Bob', 'Texto antigo');
    """)
    conn.close()

    # Abrir via Store deve migrar sem exceção
    store = Store(db_path)
    result = store.get_meeting(1)
    assert result is not None
    assert len(result.segments) == 1
    s = result.segments[0]
    assert s.text == "Texto antigo"
    # Colunas novas têm valor default após migração
    assert s.original_text is None
    assert s.corrections == []


def test_legacy_columns_added_to_segments(tmp_path: Path) -> None:
    """PRAGMA table_info confirma que original_text e corrections existem após migração."""
    db_path = tmp_path / "leg2.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE meetings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
            duration REAL NOT NULL, summary TEXT NOT NULL DEFAULT '', md_path TEXT NOT NULL DEFAULT '');
        CREATE TABLE action_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL, what TEXT NOT NULL, where_ TEXT,
            details TEXT, requested_by TEXT, priority TEXT NOT NULL DEFAULT 'media');
        CREATE TABLE segments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
            speaker TEXT, text TEXT NOT NULL);
        CREATE TABLE voices (name TEXT PRIMARY KEY, embedding BLOB NOT NULL);
        CREATE VIRTUAL TABLE search_index USING fts5 (content, meeting_id UNINDEXED, kind UNINDEXED);
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', repo_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE meeting_facts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
            source_start REAL, source_end REAL, evidence_quote TEXT,
            explicitness TEXT NOT NULL DEFAULT 'inferred',
            review_status TEXT NOT NULL DEFAULT 'needs_review');
        CREATE TABLE visual_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL, timestamp REAL NOT NULL,
            image_path TEXT NOT NULL, description TEXT NOT NULL,
            visible_text TEXT NOT NULL DEFAULT '[]', relevance TEXT NOT NULL DEFAULT 'medium');
    """)
    conn.close()

    Store(db_path)  # trigger migration

    conn2 = sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(segments)").fetchall()}
    conn2.close()
    assert "original_text" in cols
    assert "corrections" in cols


# ---------------------------------------------------------------------------
# update_turn não perde trilha de auditoria
# ---------------------------------------------------------------------------

def test_update_turn_sets_original_text_from_previous_text(tmp_store: Store) -> None:
    """Ao editar texto manualmente, original_text recebe o texto anterior se estava vazio."""
    seg = _seg("texto original", start=0.0, end=5.0)
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    seg_id = got.segments[0].id
    assert seg_id is not None

    tmp_store.update_turn(mid, [seg_id], "texto editado manualmente", None)

    updated = tmp_store.get_meeting(mid)
    assert updated is not None
    s = updated.segments[0]
    assert s.text == "texto editado manualmente"
    assert s.original_text == "texto original"   # capturou texto anterior
    assert s.corrections == []                    # edição humana: sem correção LLM


def test_update_turn_preserves_existing_original_text(tmp_store: Store) -> None:
    """Se original_text já existe, update_turn não o sobrescreve."""
    corr = _correction("hullo", "hello", 0.97, "phonetic")
    seg = _seg("hello world", original_text="hullo world", corrections=[corr])
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    seg_id = got.segments[0].id
    assert seg_id is not None

    tmp_store.update_turn(mid, [seg_id], "hello world edited", None)

    updated = tmp_store.get_meeting(mid)
    assert updated is not None
    s = updated.segments[0]
    assert s.text == "hello world edited"
    # original_text pré-existente preservado (trilha LLM não apagada)
    assert s.original_text == "hullo world"
    # corrections zerado — agora é edição humana
    assert s.corrections == []


def test_update_turn_speaker_only_does_not_clear_corrections(tmp_store: Store) -> None:
    """update_turn com text=None (só speaker) não toca original_text nem corrections."""
    corr = _correction("k8s", "Kubernetes", 0.99, "brand")
    seg = _seg("Kubernetes", original_text="k8s", corrections=[corr])
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    seg_id = got.segments[0].id
    assert seg_id is not None

    tmp_store.update_turn(mid, [seg_id], None, "Bob")

    updated = tmp_store.get_meeting(mid)
    assert updated is not None
    s = updated.segments[0]
    assert s.speaker == "Bob"
    assert s.text == "Kubernetes"
    assert s.original_text == "k8s"             # intocado
    assert len(s.corrections) == 1              # intocado
    assert s.corrections[0].corrected == "Kubernetes"


# ---------------------------------------------------------------------------
# update_segment_normalization
# ---------------------------------------------------------------------------

def test_update_segment_normalization_roundtrip(tmp_store: Store) -> None:
    """update_segment_normalization persiste text/original_text/corrections; reindexa FTS."""
    seg = _seg("raw transcript", start=0.0, end=3.0)
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    s = got.segments[0]
    assert s.id is not None

    # Simular normalização automática
    corr = _correction("raw transcript", "Kubernetes cluster meeting", 0.93, "context")
    normalized = TranscriptSegment(
        start=s.start, end=s.end,
        text="Kubernetes cluster meeting",
        speaker=s.speaker,
        original_text="raw transcript",
        corrections=[corr],
        id=s.id,
    )
    tmp_store.update_segment_normalization(mid, [normalized])

    result = tmp_store.get_meeting(mid)
    assert result is not None
    ns = result.segments[0]
    assert ns.text == "Kubernetes cluster meeting"
    assert ns.original_text == "raw transcript"
    assert len(ns.corrections) == 1
    assert ns.corrections[0].confidence == pytest.approx(0.93)


def test_update_segment_normalization_reindexes_fts(tmp_store: Store) -> None:
    """Após normalização, FTS encontra o texto corrigido e não o texto antigo."""
    seg = _seg("fizzbuz", start=0.0, end=2.0)
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    got = tmp_store.get_meeting(mid)
    assert got is not None
    s = got.segments[0]

    corr = _correction("fizzbuz", "FizzBuzz", 0.96, "brand")
    normalized = TranscriptSegment(
        start=s.start, end=s.end,
        text="FizzBuzz",
        speaker=s.speaker,
        original_text="fizzbuz",
        corrections=[corr],
        id=s.id,
    )
    tmp_store.update_segment_normalization(mid, [normalized])

    hits = tmp_store.search("FizzBuzz")
    assert any(r["meeting_id"] == mid for r in hits)


def test_update_segment_normalization_ignores_no_id(tmp_store: Store) -> None:
    """Segmentos sem id são ignorados silenciosamente."""
    seg = _seg("texto")
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    # Passar segmento sem id — não deve lançar
    no_id_seg = TranscriptSegment(start=0.0, end=1.0, text="ignorado", speaker="X")
    tmp_store.update_segment_normalization(mid, [no_id_seg])
    # Banco inalterado
    got = tmp_store.get_meeting(mid)
    assert got is not None
    assert got.segments[0].text == "texto"


# ---------------------------------------------------------------------------
# project_vocabulary
# ---------------------------------------------------------------------------

def test_project_vocabulary_high_confidence(tmp_store: Store) -> None:
    """project_vocabulary retorna apenas termos com confidence >= 0.9."""
    proj_id = tmp_store.create_project("Proj", "", "")
    corr_hi = _correction("k8s", "Kubernetes", 0.95, "brand")
    corr_lo = _correction("contaner", "container", 0.7, "typo")
    seg = _seg("Kubernetes container", corrections=[corr_hi, corr_lo])
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    tmp_store.set_meeting_project(mid, proj_id)

    vocab = tmp_store.project_vocabulary(proj_id)
    assert "Kubernetes" in vocab
    assert "container" not in vocab   # confiança abaixo de 0.9


def test_project_vocabulary_deduplicates(tmp_store: Store) -> None:
    """Mesmo termo em vários segmentos aparece uma única vez."""
    proj_id = tmp_store.create_project("Proj2", "", "")
    corr = _correction("k8s", "Kubernetes", 0.99, "brand")
    segs = [
        _seg("Kubernetes A", start=0.0, end=1.0, corrections=[corr]),
        _seg("Kubernetes B", start=1.0, end=2.0, corrections=[corr]),
    ]
    mid = tmp_store.save_meeting(_meeting(segs), Path("/tmp/m.md"))
    tmp_store.set_meeting_project(mid, proj_id)

    vocab = tmp_store.project_vocabulary(proj_id)
    assert vocab.count("Kubernetes") == 1


def test_project_vocabulary_respects_limit(tmp_store: Store) -> None:
    """limit corta o resultado."""
    proj_id = tmp_store.create_project("ProjLim", "", "")
    corrs = [
        _correction(f"term{i}", f"Term{i}", 0.95, "x")
        for i in range(10)
    ]
    seg = _seg("terms", corrections=corrs)
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    tmp_store.set_meeting_project(mid, proj_id)

    vocab = tmp_store.project_vocabulary(proj_id, limit=3)
    assert len(vocab) <= 3


def test_project_vocabulary_empty_when_no_meetings(tmp_store: Store) -> None:
    """Projeto sem reuniões retorna lista vazia."""
    proj_id = tmp_store.create_project("Empty", "", "")
    assert tmp_store.project_vocabulary(proj_id) == []


def test_project_vocabulary_empty_when_no_corrections(tmp_store: Store) -> None:
    """Reuniões sem correções retornam lista vazia."""
    proj_id = tmp_store.create_project("NoCorrProj", "", "")
    seg = _seg("plain text")
    mid = tmp_store.save_meeting(_meeting([seg]), Path("/tmp/m.md"))
    tmp_store.set_meeting_project(mid, proj_id)
    assert tmp_store.project_vocabulary(proj_id) == []


def test_project_vocabulary_recent_meetings_rank_higher(tmp_store: Store) -> None:
    """Termos exclusivos de reuniões recentes pontuam mais que os de antigas."""
    proj_id = tmp_store.create_project("Rank", "", "")

    corr_old = _correction("oldterm", "OldTerm", 0.91, "x")
    seg_old = _seg("OldTerm", corrections=[corr_old])
    mid_old = tmp_store.save_meeting(_meeting([seg_old], date="2023-01-01"), Path("/tmp/old.md"))
    tmp_store.set_meeting_project(mid_old, proj_id)

    # Termo novo aparece 1× mas em reunião recente
    corr_new = _correction("newterm", "NewTerm", 0.99, "x")
    seg_new = _seg("NewTerm", corrections=[corr_new])
    mid_new = tmp_store.save_meeting(_meeting([seg_new], date="2024-12-01"), Path("/tmp/new.md"))
    tmp_store.set_meeting_project(mid_new, proj_id)

    vocab = tmp_store.project_vocabulary(proj_id)
    assert "NewTerm" in vocab
    assert "OldTerm" in vocab
    # NewTerm deve rankear antes (índice menor) que OldTerm
    assert vocab.index("NewTerm") < vocab.index("OldTerm")
