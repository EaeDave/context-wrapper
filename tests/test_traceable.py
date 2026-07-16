"""Regression tests for traceable extraction: facts, assigned_to, review_status.

Contracts defended:
- MeetingFact is persisted and returned by get_meeting.
- ActionItem new fields (assigned_to, source_start, etc.) survive save → get.
- list_tasks personal filter: assigned_to contains "me" or is NULL; excludes pure 3rd-party.
- Project open_task_count/done_task_count use personal-list semantics (same filter).
- delete_meeting removes meeting_facts rows.
- update_meeting_extract replaces facts atomically.
- Migration idempotent: meeting_facts table created if absent; extra columns added.
- GET /api/meetings/{id} serializes action_items (all new fields) + facts list.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from meet.models import ActionItem, MeetingFact, MeetingResult, TranscriptSegment
from meet.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meeting(
    *,
    title: str = "Reunião",
    action_items: list[ActionItem] | None = None,
    facts: list[MeetingFact] | None = None,
    segments: list[TranscriptSegment] | None = None,
    project_id: int | None = None,
) -> MeetingResult:
    return MeetingResult(
        source="/tmp/test.mkv",
        date="2026-07-15",
        title=title,
        duration=3600.0,
        action_items=action_items or [],
        facts=facts or [],
        segments=segments or [],
        project_id=project_id,
    )


def _ai(what: str, assigned_to: list[str] | None = None, status: str = "aberto") -> ActionItem:
    return ActionItem(
        what=what,
        assigned_to=assigned_to,
        status=status,
    )


def _fact(kind: str = "decision", text: str = "usar Redis") -> MeetingFact:
    return MeetingFact(kind=kind, text=text)


# ---------------------------------------------------------------------------
# MeetingFact persistence
# ---------------------------------------------------------------------------


def test_facts_roundtrip(tmp_store: Store) -> None:
    """MeetingFact fields survive save → get."""
    facts = [
        MeetingFact(
            kind="decision",
            text="usar Redis",
            source_start=10.0,
            source_end=20.0,
            evidence_quote="vamos usar Redis",
            explicitness="explicit",
            review_status="confirmed",
        ),
        MeetingFact(
            kind="requirement",
            text="latência < 200ms",
            source_start=None,
            source_end=None,
            evidence_quote=None,
            explicitness="inferred",
            review_status="needs_review",
        ),
    ]
    mid = tmp_store.save_meeting(_meeting(facts=facts), Path("/tmp/out.md"))
    result = tmp_store.get_meeting(mid)
    assert result is not None
    assert len(result.facts) == 2
    dec = next(f for f in result.facts if f.kind == "decision")
    assert dec.text == "usar Redis"
    assert dec.source_start == 10.0
    assert dec.source_end == 20.0
    assert dec.evidence_quote == "vamos usar Redis"
    assert dec.explicitness == "explicit"
    assert dec.review_status == "confirmed"
    assert dec.id is not None
    req = next(f for f in result.facts if f.kind == "requirement")
    assert req.review_status == "needs_review"
    assert req.source_start is None


def test_facts_all_four_kinds(tmp_store: Store) -> None:
    """All four fact kinds are stored and retrieved."""
    facts = [
        MeetingFact(kind="decision", text="d"),
        MeetingFact(kind="requirement", text="r"),
        MeetingFact(kind="constraint", text="c"),
        MeetingFact(kind="open_question", text="q"),
    ]
    mid = tmp_store.save_meeting(_meeting(facts=facts), Path("/tmp/out.md"))
    result = tmp_store.get_meeting(mid)
    assert result is not None
    kinds = {f.kind for f in result.facts}
    assert kinds == {"decision", "requirement", "constraint", "open_question"}


def test_delete_meeting_removes_facts(tmp_store: Store, tmp_path: Path) -> None:
    """Deleting a meeting also removes its facts."""
    mid = tmp_store.save_meeting(
        _meeting(facts=[MeetingFact(kind="decision", text="x")]), Path("/tmp/out.md")
    )
    tmp_store.delete_meeting(mid, data_dir=tmp_path)
    # meeting_facts rows gone — verify via get_meeting returning None
    assert tmp_store.get_meeting(mid) is None
    # Also check directly that no orphan facts remain
    count = tmp_store._conn.execute(
        "SELECT COUNT(*) FROM meeting_facts WHERE meeting_id = ?", (mid,)
    ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# ActionItem new fields
# ---------------------------------------------------------------------------


def test_action_item_traceable_fields_roundtrip(tmp_store: Store) -> None:
    """New traceable ActionItem fields survive save → get."""
    item = ActionItem(
        what="Deploy",
        assigned_to=["me", "Alice"],
        source_start=42.0,
        source_end=55.5,
        evidence_quote="eu faço o deploy",
        explicitness="explicit",
        review_status="confirmed",
    )
    mid = tmp_store.save_meeting(_meeting(action_items=[item]), Path("/tmp/out.md"))
    result = tmp_store.get_meeting(mid)
    assert result is not None
    ai = result.action_items[0]
    assert ai.assigned_to == ["me", "Alice"]
    assert ai.source_start == 42.0
    assert ai.source_end == 55.5
    assert ai.evidence_quote == "eu faço o deploy"
    assert ai.explicitness == "explicit"
    assert ai.review_status == "confirmed"


def test_action_item_assigned_to_none_roundtrip(tmp_store: Store) -> None:
    """assigned_to=None (no owner) roundtrips correctly."""
    item = ActionItem(what="Tarefa sem dono", assigned_to=None)
    mid = tmp_store.save_meeting(_meeting(action_items=[item]), Path("/tmp/out.md"))
    result = tmp_store.get_meeting(mid)
    assert result is not None
    assert result.action_items[0].assigned_to is None


# ---------------------------------------------------------------------------
# list_tasks personal filter
# ---------------------------------------------------------------------------


def test_list_tasks_includes_me(tmp_store: Store) -> None:
    """Tasks assigned to 'me' appear in list_tasks."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("minha tarefa", assigned_to=["me"])]),
        Path("/tmp/out.md"),
    )
    tasks = tmp_store.list_tasks()
    assert any(t["what"] == "minha tarefa" for t in tasks)


def test_list_tasks_includes_me_in_list(tmp_store: Store) -> None:
    """Tasks where assigned_to is a list containing 'me' appear in list_tasks."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("tarefa compartilhada", assigned_to=["me", "Alice"])]),
        Path("/tmp/out.md"),
    )
    tasks = tmp_store.list_tasks()
    assert any(t["what"] == "tarefa compartilhada" for t in tasks)


def test_list_tasks_includes_no_owner(tmp_store: Store) -> None:
    """Tasks with no owner (assigned_to=None) appear in list_tasks."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("tarefa sem dono", assigned_to=None)]),
        Path("/tmp/out.md"),
    )
    tasks = tmp_store.list_tasks()
    assert any(t["what"] == "tarefa sem dono" for t in tasks)


def test_list_tasks_excludes_third_party(tmp_store: Store) -> None:
    """Tasks assigned exclusively to a third party are stored but excluded from list_tasks."""
    mid = tmp_store.save_meeting(
        _meeting(action_items=[
            _ai("tarefa de alice", assigned_to=["Alice"]),
            _ai("minha tarefa", assigned_to=["me"]),
        ]),
        Path("/tmp/out.md"),
    )
    tasks = tmp_store.list_tasks()
    whats = [t["what"] for t in tasks]
    assert "minha tarefa" in whats
    assert "tarefa de alice" not in whats
    # But third-party task IS stored in DB
    count = tmp_store._conn.execute(
        "SELECT COUNT(*) FROM action_items WHERE meeting_id = ? AND what = 'tarefa de alice'",
        (mid,),
    ).fetchone()[0]
    assert count == 1


def test_list_tasks_returns_new_fields(tmp_store: Store) -> None:
    """list_tasks result dicts include all new traceable fields."""
    item = ActionItem(
        what="tarefa rastreável",
        assigned_to=["me"],
        source_start=5.0,
        source_end=10.0,
        evidence_quote="citar trecho",
        explicitness="explicit",
        review_status="confirmed",
    )
    tmp_store.save_meeting(_meeting(action_items=[item]), Path("/tmp/out.md"))
    tasks = tmp_store.list_tasks()
    t = next(t for t in tasks if t["what"] == "tarefa rastreável")
    assert t["assigned_to"] == ["me"]
    assert t["source_start"] == 5.0
    assert t["source_end"] == 10.0
    assert t["evidence_quote"] == "citar trecho"
    assert t["explicitness"] == "explicit"
    assert t["review_status"] == "confirmed"


# ---------------------------------------------------------------------------
# Project task counts use personal-list semantics
# ---------------------------------------------------------------------------


def test_project_task_counts_personal_semantics(tmp_store: Store) -> None:
    """open/done_task_count exclude third-party-only tasks; same semantics as list_tasks."""
    pid = tmp_store.create_project("Proj")
    tmp_store.save_meeting(
        _meeting(
            project_id=pid,
            action_items=[
                _ai("minha tarefa", assigned_to=["me"], status="aberto"),
                _ai("minha tarefa uppercase", assigned_to=["ME"], status="aberto"),
                _ai("tarefa de alice", assigned_to=["Alice"], status="aberto"),
                _ai("sem dono", assigned_to=None, status="feito"),
            ],
        ),
        Path("/tmp/out.md"),
    )
    proj = tmp_store.get_project(pid)
    assert proj is not None
    # Lista pessoal e contadores reconhecem me case-insensitive; terceiros ficam fora.
    assert proj.open_task_count == 2
    assert proj.done_task_count == 1


# ---------------------------------------------------------------------------
# update_meeting_extract replaces facts
# ---------------------------------------------------------------------------


def test_update_meeting_extract_replaces_facts(tmp_store: Store, tmp_path: Path) -> None:
    """update_meeting_extract replaces old facts with new ones atomically."""
    mid = tmp_store.save_meeting(
        _meeting(facts=[MeetingFact(kind="decision", text="old decision")]),
        tmp_path / "out.md",
    )
    new_facts = [
        MeetingFact(kind="requirement", text="new requirement"),
        MeetingFact(kind="constraint", text="new constraint"),
    ]
    tmp_store.update_meeting_extract(mid, "new summary", [], None, new_facts)
    result = tmp_store.get_meeting(mid)
    assert result is not None
    assert result.summary == "new summary"
    assert len(result.facts) == 2
    kinds = {f.kind for f in result.facts}
    assert kinds == {"requirement", "constraint"}


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------


def test_migration_idempotent(tmp_path: Path) -> None:
    """Opening Store twice on same DB is idempotent — no duplicate columns or errors."""
    db = tmp_path / "idempotent.db"
    s1 = Store(db)
    s2 = Store(db)  # should not raise
    mid = s1.save_meeting(_meeting(), Path("/tmp/out.md"))
    assert s2.get_meeting(mid) is not None


def test_migration_adds_new_columns_to_existing_db(tmp_path: Path) -> None:
    """Existing action_items rows get NULL for new columns after migration."""
    import sqlite3
    db = tmp_path / "legacy.db"
    # Create a minimal DB without new columns
    conn = sqlite3.connect(str(db))
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
            meeting_id INTEGER NOT NULL REFERENCES meetings(id),
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
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5 (
            content,
            meeting_id UNINDEXED,
            kind UNINDEXED
        );
        INSERT INTO meetings (date, title, source, duration) VALUES ('2024-01-01', 'Old', '/tmp/x', 60.0);
        INSERT INTO action_items (meeting_id, what, priority) VALUES (1, 'old task', 'media');
    """)
    conn.close()

    # Store migration should add new columns without error
    store = Store(db)
    result = store.get_meeting(1)
    assert result is not None
    assert len(result.action_items) == 1
    ai = result.action_items[0]
    assert ai.what == "old task"
    assert ai.assigned_to is None
    assert ai.review_status == "needs_review"  # default applied in Python
    assert ai.explicitness == "inferred"


# ---------------------------------------------------------------------------
# API: GET /api/meetings/{id} shape
# ---------------------------------------------------------------------------


def test_api_meeting_detail_shape(tmp_path: Path) -> None:
    """GET /api/meetings/{id} returns action_items with all new fields and facts list."""
    from meet.config import Settings
    from meet.web.app import create_app

    settings = Settings(
        data_dir=tmp_path,
        output_dir=tmp_path,
        llm_provider="anthropic",
        anthropic_api_key="fake",
    )
    store = Store(settings.db_path)

    facts = [
        MeetingFact(
            kind="decision",
            text="usar PostgreSQL",
            source_start=1.0,
            source_end=3.0,
            evidence_quote="vamos usar PostgreSQL",
            explicitness="explicit",
            review_status="confirmed",
        )
    ]
    items = [
        ActionItem(
            what="Deploy",
            assigned_to=["me"],
            source_start=10.0,
            source_end=15.0,
            evidence_quote="eu faço o deploy",
            explicitness="explicit",
            review_status="confirmed",
        ),
        ActionItem(
            what="Task de terceiro",
            assigned_to=["Alice"],
            source_start=None,
            source_end=None,
            evidence_quote=None,
            explicitness="inferred",
            review_status="needs_review",
        ),
    ]
    meeting = MeetingResult(
        source=str(tmp_path / "test.mkv"),
        date="2026-07-15",
        title="Test Meeting",
        duration=3600.0,
        action_items=items,
        facts=facts,
        segments=[],
    )
    mid = store.save_meeting(meeting, tmp_path / "test.md")

    app = create_app()

    import unittest.mock as mock
    with mock.patch("meet.web.app._settings_store", return_value=(settings, store)):
        client = TestClient(app)
        resp = client.get(f"/api/meetings/{mid}")
        markdown_resp = client.get(f"/api/meetings/{mid}/markdown")

    assert resp.status_code == 200
    body = resp.json()

    # action_items: both stored (no personal filter at this endpoint)
    ais = body["action_items"]
    assert len(ais) == 2
    ai0 = next(a for a in ais if a["what"] == "Deploy")
    assert ai0["assigned_to"] == ["me"]
    assert ai0["source_start"] == 10.0
    assert ai0["source_end"] == 15.0
    assert ai0["evidence_quote"] == "eu faço o deploy"
    assert ai0["explicitness"] == "explicit"
    assert ai0["review_status"] == "confirmed"

    # Third-party task also in response (full list, not filtered)
    ai1 = next(a for a in ais if a["what"] == "Task de terceiro")
    assert ai1["assigned_to"] == ["Alice"]
    assert ai1["review_status"] == "needs_review"

    # facts
    assert "facts" in body
    assert len(body["facts"]) == 1
    f = body["facts"][0]
    assert f["kind"] == "decision"
    assert f["text"] == "usar PostgreSQL"
    assert f["source_start"] == 1.0
    assert f["source_end"] == 3.0
    assert f["evidence_quote"] == "vamos usar PostgreSQL"
    assert f["explicitness"] == "explicit"

    assert markdown_resp.status_code == 200
    assert markdown_resp.headers["content-type"].startswith("text/markdown")
    downloaded = markdown_resp.text
    assert "## Action items" in downloaded
    assert "**Responsáveis:** me" in downloaded
    assert "## Fatos da reunião" in downloaded
    assert "### Decisões" in downloaded
    assert "usar PostgreSQL" in downloaded
    assert "**Evidência:** “vamos usar PostgreSQL”" in downloaded
    assert "## Transcript" in downloaded
    assert f["review_status"] == "confirmed"
    assert f["id"] is not None


def test_update_turn_revalidates_action_items_and_facts_both_directions(
    tmp_store: Store, tmp_path: Path
) -> None:
    segments = [
        TranscriptSegment(0.0, 2.0, "primeira parte", speaker="me"),
        TranscriptSegment(2.0, 4.0, "segunda parte", speaker="me"),
    ]
    items = [
        ActionItem(
            "fica inválida",
            source_start=0.0,
            source_end=4.0,
            evidence_quote="primeira parte segunda parte",
            review_status="confirmed",
        ),
        ActionItem(
            "fica válida",
            source_start=0.0,
            source_end=4.0,
            evidence_quote="texto novo",
            review_status="needs_review",
        ),
    ]
    facts = [
        MeetingFact(
            "decision",
            "fica inválido",
            0.0,
            4.0,
            "primeira parte segunda parte",
            review_status="confirmed",
        ),
        MeetingFact(
            "requirement",
            "fica válido",
            0.0,
            4.0,
            "texto novo",
            review_status="needs_review",
        ),
    ]
    mid = tmp_store.save_meeting(
        _meeting(action_items=items, facts=facts, segments=segments),
        tmp_path / "trace.md",
    )
    loaded = tmp_store.get_meeting(mid)
    assert loaded is not None
    seg_ids = [segment.id for segment in loaded.segments]
    assert all(seg_id is not None for seg_id in seg_ids)

    assert tmp_store.update_turn(mid, seg_ids, "texto novo", "me") is True

    after = tmp_store.get_meeting(mid)
    assert after is not None
    assert [item.review_status for item in after.action_items] == [
        "needs_review",
        "confirmed",
    ]
    assert [fact.review_status for fact in after.facts] == [
        "needs_review",
        "confirmed",
    ]


def test_api_patch_action_item_can_clear_owner(tmp_path: Path) -> None:
    from meet.config import Settings
    from meet.web.app import create_app
    import unittest.mock as mock

    settings = Settings(data_dir=tmp_path, output_dir=tmp_path)
    store = Store(settings.db_path)
    mid = store.save_meeting(
        _meeting(action_items=[ActionItem("Minha tarefa", assigned_to=["me"])]),
        tmp_path / "patch.md",
    )
    item_id = store.get_meeting(mid).action_items[0].id
    app = create_app()

    with mock.patch("meet.web.app._settings_store", return_value=(settings, store)):
        response = TestClient(app).patch(
            f"/api/action-items/{item_id}", json={"assigned_to": None}
        )

    assert response.status_code == 200
    assert store.get_meeting(mid).action_items[0].assigned_to is None
