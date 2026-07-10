"""Testes Tier 1 — action items rastreáveis, edição transcript, reprocess/reextract.

Contratos defendidos:
- Migração: banco sem colunas status/due → colunas adicionadas com defaults corretos.
- CRUD action item: add→update→list_tasks filtra→delete; FTS reindexado.
- update_turn speaker-only: preserva nº de segmentos.
- update_turn com text: colapsa N→1, start/end corretos.
- replace_meeting_content: segments/action_items trocados; source/date preservados.
- update_meeting_extract: summary trocado; title preservado.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from meet.models import ActionItem, MeetingResult, TranscriptSegment
from meet.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(start: float, end: float, text: str, speaker: str = "A") -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text, speaker=speaker)


def _item(what: str, priority: str = "media") -> ActionItem:
    return ActionItem(what=what, priority=priority)


def _meeting(
    *,
    title: str = "Reunião",
    date: str = "2024-03-10",
    source: str = "/tmp/test.mkv",
    duration: float = 1800.0,
    summary: str = "Resumo.",
    segments: list[TranscriptSegment] | None = None,
    action_items: list[ActionItem] | None = None,
) -> MeetingResult:
    return MeetingResult(
        source=source,
        date=date,
        title=title,
        duration=duration,
        summary=summary,
        segments=segments or [],
        action_items=action_items or [],
    )


# ---------------------------------------------------------------------------
# 1. Migração: banco SEM colunas status/due → Store adiciona via ALTER TABLE
# ---------------------------------------------------------------------------


def test_migration_adds_status_due_to_existing_db(tmp_path: Path) -> None:
    """Abre banco legado (sem status/due) e verifica que a migração adiciona as colunas."""
    db_path = tmp_path / "legacy.db"

    # Cria schema sem as colunas status/due (simula banco antigo)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meetings (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            date     TEXT NOT NULL,
            title    TEXT NOT NULL,
            source   TEXT NOT NULL,
            duration REAL NOT NULL,
            summary  TEXT NOT NULL DEFAULT '',
            md_path  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS action_items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id   INTEGER NOT NULL REFERENCES meetings(id),
            what         TEXT NOT NULL,
            where_       TEXT,
            details      TEXT,
            requested_by TEXT,
            priority     TEXT NOT NULL DEFAULT 'media'
        );
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL,
            start REAL NOT NULL,
            end REAL NOT NULL,
            speaker TEXT,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS voices (name TEXT PRIMARY KEY, embedding BLOB NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5 (
            content, meeting_id UNINDEXED, kind UNINDEXED
        );
        INSERT INTO meetings (date, title, source, duration, summary, md_path)
        VALUES ('2024-01-01', 'Legado', '/tmp/x.mkv', 900, 'S', '');
        INSERT INTO action_items (meeting_id, what, priority)
        VALUES (1, 'Fazer algo', 'alta');
    """)
    conn.close()

    # Abre via Store — deve rodar _migrate e adicionar colunas
    store = Store(db_path)

    cols = {
        r[1]
        for r in store._conn.execute("PRAGMA table_info(action_items)").fetchall()
    }
    assert "status" in cols, "coluna status deve existir após migração"
    assert "due" in cols, "coluna due deve existir após migração"

    # Registro antigo deve ter default 'aberto'
    row = store._conn.execute(
        "SELECT status, due FROM action_items WHERE id = 1"
    ).fetchone()
    assert row[0] == "aberto"
    assert row[1] is None


# ---------------------------------------------------------------------------
# 2. CRUD action item + list_tasks + FTS reindex
# ---------------------------------------------------------------------------


def test_add_action_item_returns_id(tmp_store: Store, tmp_path: Path) -> None:
    mid = tmp_store.save_meeting(
        _meeting(action_items=[]), tmp_path / "r.md"
    )
    new_id = tmp_store.add_action_item(mid, _item("Implementar login"))
    assert isinstance(new_id, int) and new_id > 0


def test_update_action_item_status_and_due(tmp_store: Store, tmp_path: Path) -> None:
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_item("Corrigir bug")]), tmp_path / "r.md"
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    item_id = result.action_items[0].id
    assert item_id is not None

    ok = tmp_store.update_action_item(item_id, {"status": "feito", "due": "2024-12-31"})
    assert ok is True

    result2 = tmp_store.get_meeting(mid)
    assert result2 is not None
    ai = result2.action_items[0]
    assert ai.status == "feito"
    assert ai.due == "2024-12-31"


def test_update_action_item_maps_where(tmp_store: Store, tmp_path: Path) -> None:
    """Campo 'where' no dict (JSON) deve mapear para coluna 'where_'."""
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_item("Task")]), tmp_path / "r.md"
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    item_id = result.action_items[0].id
    assert item_id is not None

    tmp_store.update_action_item(item_id, {"where": "frontend/src"})
    result2 = tmp_store.get_meeting(mid)
    assert result2 is not None
    assert result2.action_items[0].where == "frontend/src"


def test_list_tasks_filters_by_status(tmp_store: Store, tmp_path: Path) -> None:
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_item("A"), _item("B")]), tmp_path / "r.md"
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    id_a = result.action_items[0].id
    assert id_a is not None
    tmp_store.update_action_item(id_a, {"status": "feito"})

    abertos = tmp_store.list_tasks("aberto")
    feitos = tmp_store.list_tasks("feito")
    todos = tmp_store.list_tasks("todos")

    assert len(abertos) == 1
    assert abertos[0]["what"] == "B"
    assert len(feitos) == 1
    assert feitos[0]["what"] == "A"
    assert len(todos) == 2


def test_list_tasks_has_meeting_info(tmp_store: Store, tmp_path: Path) -> None:
    mid = tmp_store.save_meeting(
        _meeting(title="Reunião Alpha", action_items=[_item("Algo")]),
        tmp_path / "r.md",
    )
    tasks = tmp_store.list_tasks("aberto")
    assert len(tasks) == 1
    t = tasks[0]
    assert t["meeting_id"] == mid
    assert t["meeting_title"] == "Reunião Alpha"
    assert "date" in t
    assert "priority" in t


def test_delete_action_item(tmp_store: Store, tmp_path: Path) -> None:
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_item("X"), _item("Y")]), tmp_path / "r.md"
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    item_id = result.action_items[0].id
    assert item_id is not None

    ok = tmp_store.delete_action_item(item_id)
    assert ok is True

    result2 = tmp_store.get_meeting(mid)
    assert result2 is not None
    assert len(result2.action_items) == 1

    # Tentar deletar de novo → False
    assert tmp_store.delete_action_item(item_id) is False


def test_action_item_fts_reindex_after_update(tmp_store: Store, tmp_path: Path) -> None:
    """Após update de 'what', FTS deve encontrar o novo texto."""
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_item("Texto antigo")]), tmp_path / "r.md"
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    item_id = result.action_items[0].id
    assert item_id is not None

    tmp_store.update_action_item(item_id, {"what": "Texto novo revisado"})

    hits = tmp_store.search("revisado")
    assert any(h["meeting_id"] == mid for h in hits), "FTS deve encontrar novo texto"

    old_hits = tmp_store.search("antigo")
    # O texto antigo não deve mais aparecer como action_item desta reunião
    ai_hits = [h for h in old_hits if h["meeting_id"] == mid and h["kind"] == "action_item"]
    assert len(ai_hits) == 0, "FTS não deve retornar texto antigo após reindex"


# ---------------------------------------------------------------------------
# 3. update_turn
# ---------------------------------------------------------------------------


def test_update_turn_speaker_only_preserves_segment_count(
    tmp_store: Store, tmp_path: Path
) -> None:
    """Mudança só de speaker não deve deletar segmentos."""
    segs = [_seg(0.0, 1.0, "Olá"), _seg(1.0, 2.0, "Mundo")]
    mid = tmp_store.save_meeting(
        _meeting(segments=segs), tmp_path / "r.md"
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    seg_ids = [s.id for s in result.segments if s.id is not None]
    assert len(seg_ids) == 2

    ok = tmp_store.update_turn(mid, seg_ids, text=None, speaker="Bob")
    assert ok is True

    result2 = tmp_store.get_meeting(mid)
    assert result2 is not None
    assert len(result2.segments) == 2, "speaker-only não deve colapsar segmentos"
    assert all(s.speaker == "Bob" for s in result2.segments)


def test_update_turn_text_collapses_segments(tmp_store: Store, tmp_path: Path) -> None:
    """Text fornecido colapsa múltiplos segmentos em 1 com start/end corretos."""
    segs = [
        _seg(0.0, 1.0, "Primeiro", "A"),
        _seg(1.0, 2.5, "Segundo", "A"),
        _seg(2.5, 4.0, "Terceiro", "A"),
    ]
    mid = tmp_store.save_meeting(
        _meeting(segments=segs), tmp_path / "r.md"
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    seg_ids = [s.id for s in result.segments if s.id is not None]
    assert len(seg_ids) == 3

    ok = tmp_store.update_turn(mid, seg_ids, text="Texto colapsado", speaker=None)
    assert ok is True

    result2 = tmp_store.get_meeting(mid)
    assert result2 is not None
    assert len(result2.segments) == 1, "deve colapsar 3 → 1"
    collapsed = result2.segments[0]
    assert collapsed.text == "Texto colapsado"
    assert collapsed.start == pytest.approx(0.0)
    assert collapsed.end == pytest.approx(4.0)
    assert collapsed.speaker == "A"  # speaker original preservado quando não fornecido


def test_update_turn_text_with_speaker_override(tmp_store: Store, tmp_path: Path) -> None:
    segs = [_seg(0.0, 1.0, "Foo", "Alice"), _seg(1.0, 2.0, "Bar", "Alice")]
    mid = tmp_store.save_meeting(_meeting(segments=segs), tmp_path / "r.md")
    result = tmp_store.get_meeting(mid)
    assert result is not None
    seg_ids = [s.id for s in result.segments if s.id is not None]

    tmp_store.update_turn(mid, seg_ids, text="Merged", speaker="Bob")
    result2 = tmp_store.get_meeting(mid)
    assert result2 is not None
    assert result2.segments[0].speaker == "Bob"
    assert result2.segments[0].text == "Merged"


def test_update_turn_returns_false_for_wrong_meeting(
    tmp_store: Store, tmp_path: Path
) -> None:
    segs = [_seg(0.0, 1.0, "Txt")]
    mid = tmp_store.save_meeting(_meeting(segments=segs), tmp_path / "r.md")
    result = tmp_store.get_meeting(mid)
    assert result is not None
    seg_ids = [s.id for s in result.segments if s.id is not None]

    # meeting_id errado → False
    ok = tmp_store.update_turn(mid + 999, seg_ids, text=None, speaker="X")
    assert ok is False


# ---------------------------------------------------------------------------
# 4. replace_meeting_content
# ---------------------------------------------------------------------------


def test_replace_meeting_content_replaces_segments_and_items(
    tmp_store: Store, tmp_path: Path
) -> None:
    original = _meeting(
        title="Original",
        date="2024-01-01",
        source="/tmp/orig.mkv",
        segments=[_seg(0.0, 1.0, "Antigo")],
        action_items=[_item("Tarefa antiga")],
    )
    mid = tmp_store.save_meeting(original, tmp_path / "r.md")

    new_result = MeetingResult(
        source="irrelevante",  # source preservado do banco
        date="não importa",
        title="Novo título",
        duration=999.0,
        participants=["X"],
        summary="Novo resumo",
        action_items=[_item("Nova tarefa")],
        segments=[_seg(0.0, 2.0, "Novo seg 1"), _seg(2.0, 4.0, "Novo seg 2")],
    )

    tmp_store.replace_meeting_content(mid, new_result)

    after = tmp_store.get_meeting(mid)
    assert after is not None

    # Segments trocados
    assert len(after.segments) == 2
    assert after.segments[0].text == "Novo seg 1"

    # Action items trocados
    assert len(after.action_items) == 1
    assert after.action_items[0].what == "Nova tarefa"

    # source e date preservados (replace_meeting_content NÃO altera)
    assert after.source == "/tmp/orig.mkv"
    assert after.date == "2024-01-01"

    # Title e summary atualizados
    assert after.title == "Novo título"
    assert after.summary == "Novo resumo"


def test_replace_meeting_content_is_atomic_fts(tmp_store: Store, tmp_path: Path) -> None:
    """FTS deve encontrar novo conteúdo, não o antigo, após replace."""
    mid = tmp_store.save_meeting(
        _meeting(
            segments=[_seg(0.0, 1.0, "conteudo_antigo")],
            action_items=[_item("item_antigo")],
        ),
        tmp_path / "r.md",
    )

    new = MeetingResult(
        source="x",
        date="2024-01-01",
        title="T",
        duration=1.0,
        segments=[_seg(0.0, 1.0, "conteudo_novo")],
        action_items=[_item("item_novo")],
    )
    tmp_store.replace_meeting_content(mid, new)

    assert not any(h["meeting_id"] == mid for h in tmp_store.search("conteudo_antigo"))
    assert any(h["meeting_id"] == mid for h in tmp_store.search("conteudo_novo"))


# ---------------------------------------------------------------------------
# 5. update_meeting_extract
# ---------------------------------------------------------------------------


def test_update_meeting_extract_updates_summary_and_items(
    tmp_store: Store, tmp_path: Path
) -> None:
    original = _meeting(
        title="Título Existente",
        summary="Resumo velho",
        action_items=[_item("Velha tarefa")],
        segments=[_seg(0.0, 1.0, "Texto")],
    )
    mid = tmp_store.save_meeting(original, tmp_path / "r.md")

    new_items = [_item("Nova tarefa alpha"), _item("Nova tarefa beta")]
    tmp_store.update_meeting_extract(mid, "Novo resumo excelente", new_items, None)

    after = tmp_store.get_meeting(mid)
    assert after is not None

    # Summary trocado
    assert after.summary == "Novo resumo excelente"

    # Action items trocados
    assert len(after.action_items) == 2
    whats = {ai.what for ai in after.action_items}
    assert whats == {"Nova tarefa alpha", "Nova tarefa beta"}

    # Title preservado (NÃO sobrescrito)
    assert after.title == "Título Existente"

    # Segments intactos
    assert len(after.segments) == 1


def test_update_meeting_extract_fts_reindex(tmp_store: Store, tmp_path: Path) -> None:
    """FTS deve refletir novos action items após update_meeting_extract."""
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_item("tarefa_removida")], segments=[_seg(0.0, 1.0, "seg")]),
        tmp_path / "r.md",
    )

    tmp_store.update_meeting_extract(mid, "S", [_item("tarefa_nova_fts")], None)

    assert not any(
        h["meeting_id"] == mid and h["kind"] == "action_item"
        for h in tmp_store.search("tarefa_removida")
    )
    assert any(
        h["meeting_id"] == mid and h["kind"] == "action_item"
        for h in tmp_store.search("tarefa_nova_fts")
    )


# ---------------------------------------------------------------------------
# 6. get_meeting devolve id nos segmentos e action_items
# ---------------------------------------------------------------------------


def test_get_meeting_returns_segment_ids(tmp_store: Store, tmp_path: Path) -> None:
    mid = tmp_store.save_meeting(
        _meeting(segments=[_seg(0.0, 1.0, "A"), _seg(1.0, 2.0, "B")]),
        tmp_path / "r.md",
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    for s in result.segments:
        assert s.id is not None and s.id > 0


def test_get_meeting_returns_action_item_ids(tmp_store: Store, tmp_path: Path) -> None:
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_item("T1"), _item("T2")]),
        tmp_path / "r.md",
    )
    result = tmp_store.get_meeting(mid)
    assert result is not None
    for ai in result.action_items:
        assert ai.id is not None and ai.id > 0
    # ids únicos
    ids = [ai.id for ai in result.action_items]
    assert len(ids) == len(set(ids))
