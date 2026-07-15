"""Tests for context export — scope filter and POST /api/context/export.

Contracts defended:
- GET /api/tasks?scope=personal returns tasks with no owner or 'me' as exact element.
- GET /api/tasks?scope=delegated returns tasks with an owner that does NOT contain 'me'.
- GET /api/tasks?scope=all returns all tasks regardless of owner.
- json_each element match is exact/case-insensitive: 'memo', 'Me!', 'someone' don't match 'me'.
- Default scope=personal preserves backward-compat (callers without scope still work).
- POST /api/context/export: 400 for empty task_ids.
- POST /api/context/export: 404 for any missing ID.
- POST /api/context/export: response has exact wire shape (format, filename, content, task_count, meeting_count).
- content is always a string regardless of format (markdown or json).
- IDs order is preserved in tasks output.
- include_evidence=False omits quote and timestamps from tasks and facts.
- include_summary=False omits summary from meetings.
- include_facts=False omits facts from meetings.
- include_transcript=False (default) omits transcript; True includes it.
- Markdown and JSON are semantically equivalent (same objective, task count, meeting count).
- filename is deterministic and safe (no wall-clock dependency).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from meet.models import ActionItem, MeetingFact, MeetingResult, TranscriptSegment
from meet.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meeting(
    *,
    title: str = "Test Meeting",
    date: str = "2024-06-01",
    action_items: list[ActionItem] | None = None,
    facts: list[MeetingFact] | None = None,
    segments: list[TranscriptSegment] | None = None,
    summary: str = "A short summary.",
) -> MeetingResult:
    return MeetingResult(
        source="/tmp/fake.mkv",
        date=date,
        title=title,
        duration=60.0,
        summary=summary,
        action_items=action_items or [],
        facts=facts or [],
        segments=segments or [TranscriptSegment(0.0, 1.0, "hello world", speaker="Alice")],
    )


def _ai(
    what: str = "Do something",
    *,
    status: str = "aberto",
    assigned_to: list[str] | None = None,
    source_start: float | None = None,
    evidence_quote: str | None = None,
    explicitness: str = "inferred",
    review_status: str = "needs_review",
) -> ActionItem:
    return ActionItem(
        what=what,
        priority="media",
        status=status,
        assigned_to=assigned_to,
        source_start=source_start,
        evidence_quote=evidence_quote,
        explicitness=explicitness,
        review_status=review_status,
    )


def _fact(
    text: str = "We decided X",
    *,
    kind: str = "decision",
    source_start: float | None = 5.0,
    evidence_quote: str | None = "literally said X",
    review_status: str = "confirmed",
) -> MeetingFact:
    return MeetingFact(
        kind=kind,
        text=text,
        source_start=source_start,
        evidence_quote=evidence_quote,
        review_status=review_status,
    )


@pytest.fixture()
def store_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def api_client(store_db: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    from meet.web.app import create_app
    from meet.store import Store as RealStore

    settings_mock = MagicMock()
    settings_mock.db_path = store_db
    settings_mock.data_dir = store_db.parent / "data"
    settings_mock.output_dir = store_db.parent / "output"
    settings_mock.data_dir.mkdir(parents=True, exist_ok=True)
    settings_mock.output_dir.mkdir(parents=True, exist_ok=True)

    import meet.web.app as app_module

    monkeypatch.setattr(
        app_module,
        "_settings_store",
        lambda: (settings_mock, RealStore(store_db)),
    )
    app = create_app()
    return TestClient(app, raise_server_exceptions=True), store_db


# ---------------------------------------------------------------------------
# Store: list_tasks scope filter
# ---------------------------------------------------------------------------


def test_list_tasks_personal_no_owner(tmp_store: Store) -> None:
    """Task with no assigned_to is included in personal scope."""
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_ai("NoOwner")]), Path("/tmp/a.md")
    )
    tasks = tmp_store.list_tasks("aberto", scope="personal")
    assert any(t["what"] == "NoOwner" for t in tasks)


def test_list_tasks_personal_me_element(tmp_store: Store) -> None:
    """Task assigned to ['me'] is included in personal scope."""
    mid = tmp_store.save_meeting(
        _meeting(action_items=[_ai("MeTask", assigned_to=["me"])]), Path("/tmp/b.md")
    )
    tasks = tmp_store.list_tasks("aberto", scope="personal")
    assert any(t["what"] == "MeTask" for t in tasks)


def test_list_tasks_personal_me_case_insensitive(tmp_store: Store) -> None:
    """'Me' and 'ME' (case variants) are matched as 'me'."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("UpperMe", assigned_to=["Me"])]), Path("/tmp/c.md")
    )
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("AllCapsMe", assigned_to=["ME"])]), Path("/tmp/d.md")
    )
    tasks = tmp_store.list_tasks("aberto", scope="personal")
    whats = {t["what"] for t in tasks}
    assert "UpperMe" in whats
    assert "AllCapsMe" in whats


def test_list_tasks_personal_excludes_others_only(tmp_store: Store) -> None:
    """Task assigned only to other people is excluded from personal scope."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("OtherTask", assigned_to=["alice", "bob"])]),
        Path("/tmp/e.md"),
    )
    tasks = tmp_store.list_tasks("aberto", scope="personal")
    assert not any(t["what"] == "OtherTask" for t in tasks)


def test_list_tasks_personal_no_substring_match(tmp_store: Store) -> None:
    """'memo', 'Me!', 'someone' do NOT trigger 'me' match (exact element only)."""
    tmp_store.save_meeting(
        _meeting(
            action_items=[
                _ai("MemoTask", assigned_to=["memo"]),
                _ai("MeBang", assigned_to=["Me!"]),
                _ai("Someone", assigned_to=["someone"]),
            ]
        ),
        Path("/tmp/f.md"),
    )
    tasks = tmp_store.list_tasks("aberto", scope="personal")
    whats = {t["what"] for t in tasks}
    assert "MemoTask" not in whats
    assert "MeBang" not in whats
    assert "Someone" not in whats


def test_list_tasks_personal_me_and_others(tmp_store: Store) -> None:
    """Task assigned to ['me', 'alice'] is included in personal scope."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("MeAndAlice", assigned_to=["me", "alice"])]),
        Path("/tmp/g.md"),
    )
    tasks = tmp_store.list_tasks("aberto", scope="personal")
    assert any(t["what"] == "MeAndAlice" for t in tasks)


def test_list_tasks_delegated_excludes_no_owner(tmp_store: Store) -> None:
    """Delegated scope excludes tasks with no owner."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("NoOwnerTask")]), Path("/tmp/h.md")
    )
    tasks = tmp_store.list_tasks("aberto", scope="delegated")
    assert not any(t["what"] == "NoOwnerTask" for t in tasks)


def test_list_tasks_delegated_excludes_me(tmp_store: Store) -> None:
    """Delegated scope excludes tasks assigned to me."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("MyTask", assigned_to=["me"])]), Path("/tmp/i.md")
    )
    tasks = tmp_store.list_tasks("aberto", scope="delegated")
    assert not any(t["what"] == "MyTask" for t in tasks)


def test_list_tasks_delegated_includes_others(tmp_store: Store) -> None:
    """Delegated scope includes tasks assigned to others only."""
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("AliceTask", assigned_to=["alice"])]),
        Path("/tmp/j.md"),
    )
    tasks = tmp_store.list_tasks("aberto", scope="delegated")
    assert any(t["what"] == "AliceTask" for t in tasks)


def test_list_tasks_all_includes_everything(tmp_store: Store) -> None:
    """scope=all returns tasks regardless of owner."""
    tmp_store.save_meeting(
        _meeting(
            action_items=[
                _ai("NoOwner"),
                _ai("MeOwner", assigned_to=["me"]),
                _ai("OtherOwner", assigned_to=["bob"]),
            ]
        ),
        Path("/tmp/k.md"),
    )
    tasks = tmp_store.list_tasks("aberto", scope="all")
    whats = {t["what"] for t in tasks}
    assert {"NoOwner", "MeOwner", "OtherOwner"} <= whats


def test_list_tasks_default_scope_is_personal(tmp_store: Store) -> None:
    """Calling list_tasks without scope= defaults to personal behaviour."""
    tmp_store.save_meeting(
        _meeting(
            action_items=[
                _ai("Mine", assigned_to=["me"]),
                _ai("Theirs", assigned_to=["carol"]),
            ]
        ),
        Path("/tmp/l.md"),
    )
    tasks = tmp_store.list_tasks("aberto")
    whats = {t["what"] for t in tasks}
    assert "Mine" in whats
    assert "Theirs" not in whats


# ---------------------------------------------------------------------------
# API: GET /api/tasks scope parameter
# ---------------------------------------------------------------------------


def test_api_tasks_scope_personal(api_client) -> None:
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[
                _ai("PersonalTask", assigned_to=["me"]),
                _ai("DelegatedTask", assigned_to=["dave"]),
            ]
        ),
        Path("/tmp/api_scope.md"),
    )
    r = tc.get("/api/tasks?scope=personal")
    assert r.status_code == 200
    whats = {t["what"] for t in r.json()}
    assert "PersonalTask" in whats
    assert "DelegatedTask" not in whats


def test_api_tasks_scope_delegated(api_client) -> None:
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[
                _ai("Mine", assigned_to=["me"]),
                _ai("Delegated", assigned_to=["eve"]),
                _ai("Unowned"),
            ]
        ),
        Path("/tmp/api_del.md"),
    )
    r = tc.get("/api/tasks?scope=delegated")
    assert r.status_code == 200
    whats = {t["what"] for t in r.json()}
    assert "Delegated" in whats
    assert "Mine" not in whats
    assert "Unowned" not in whats


def test_api_tasks_scope_all(api_client) -> None:
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[
                _ai("Mine", assigned_to=["me"]),
                _ai("Delegated", assigned_to=["frank"]),
                _ai("Unowned"),
            ]
        ),
        Path("/tmp/api_all.md"),
    )
    r = tc.get("/api/tasks?scope=all")
    assert r.status_code == 200
    whats = {t["what"] for t in r.json()}
    assert {"Mine", "Delegated", "Unowned"} <= whats


def test_api_tasks_scope_default_is_personal(api_client) -> None:
    """?scope omitted → personal (backward compatible)."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[
                _ai("MyTask"),  # no owner → personal
                _ai("TheirTask", assigned_to=["grace"]),
            ]
        ),
        Path("/tmp/api_def.md"),
    )
    r = tc.get("/api/tasks")
    assert r.status_code == 200
    whats = {t["what"] for t in r.json()}
    assert "MyTask" in whats
    assert "TheirTask" not in whats


def test_api_tasks_invalid_scope_400(api_client) -> None:
    tc, _ = api_client
    r = tc.get("/api/tasks?scope=unknown")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# API: POST /api/context/export — wire shape and validation
# ---------------------------------------------------------------------------


def test_context_export_empty_ids_400(api_client) -> None:
    tc, _ = api_client
    r = tc.post("/api/context/export", json={"task_ids": []})
    assert r.status_code == 400


def test_context_export_missing_id_404(api_client) -> None:
    tc, _ = api_client
    r = tc.post("/api/context/export", json={"task_ids": [99999]})
    assert r.status_code == 404


def test_context_export_mixed_missing_id_404(api_client) -> None:
    """Even if some IDs exist, a single missing one triggers 404."""
    tc, db = api_client
    s = Store(db)
    mid = s.save_meeting(_meeting(action_items=[_ai("Real")]), Path("/tmp/me.md"))
    tasks = s.list_tasks("aberto", scope="all")
    real_id = tasks[0]["id"]
    r = tc.post("/api/context/export", json={"task_ids": [real_id, 99999]})
    assert r.status_code == 404


def test_context_export_markdown_wire_shape(api_client) -> None:
    """Response has exact keys: format, filename, content, task_count, meeting_count."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(_meeting(action_items=[_ai("Task A")]), Path("/tmp/wire.md"))
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "format": "markdown"},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"format", "filename", "content", "task_count", "meeting_count"}
    assert body["format"] == "markdown"
    assert body["task_count"] == 1
    assert body["meeting_count"] == 1
    assert isinstance(body["content"], str)
    assert isinstance(body["filename"], str)
    assert body["filename"].endswith(".md")


def test_context_export_json_wire_shape(api_client) -> None:
    """format=json: content is a string (pre-serialized JSON)."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(_meeting(action_items=[_ai("Task B")]), Path("/tmp/jshape.md"))
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "format": "json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "json"
    assert isinstance(body["content"], str)
    assert body["filename"].endswith(".json")
    # content must be parseable JSON
    parsed = json.loads(body["content"])
    assert "tasks" in parsed
    assert "meetings" in parsed


def test_context_export_id_order_preserved(api_client) -> None:
    """Tasks appear in the exact order of task_ids, not insertion order."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(action_items=[_ai("First"), _ai("Second"), _ai("Third")]),
        Path("/tmp/order.md"),
    )
    all_tasks = s.list_tasks("aberto", scope="all")
    # all_tasks sorted by priority/date — grab IDs and reverse them
    ids = [t["id"] for t in all_tasks]
    reversed_ids = list(reversed(ids))

    r = tc.post("/api/context/export", json={"task_ids": reversed_ids, "format": "json"})
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    returned_ids = [t["id"] for t in parsed["tasks"]]
    assert returned_ids == reversed_ids


def test_context_export_include_evidence_false_omits_quote(api_client) -> None:
    """include_evidence=False removes evidence_quote and source_start from tasks."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[
                _ai("EvidTask", source_start=10.0, evidence_quote="said it")
            ]
        ),
        Path("/tmp/ev.md"),
    )
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "format": "json", "include_evidence": False},
    )
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    task = parsed["tasks"][0]
    assert "evidence_quote" not in task
    assert "source_start" not in task


def test_context_export_include_evidence_false_omits_fact_quote(api_client) -> None:
    """include_evidence=False removes evidence fields from facts too."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[_ai("TaskWithFact")],
            facts=[_fact("We decided X", kind="decision", evidence_quote="literally X")],
        ),
        Path("/tmp/evfact.md"),
    )
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={
            "task_ids": [task_id],
            "format": "json",
            "include_evidence": False,
            "include_facts": True,
        },
    )
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    mtg = parsed["meetings"][0]
    decisions = mtg["facts"].get("decision", [])
    assert decisions, "Expected at least one decision fact"
    fact = decisions[0]
    assert "evidence_quote" not in fact
    assert "source_start" not in fact


def test_context_export_include_facts_false(api_client) -> None:
    """include_facts=False produces facts={} in meetings output."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[_ai("T")],
            facts=[_fact("D", kind="decision")],
        ),
        Path("/tmp/nofact.md"),
    )
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "format": "json", "include_facts": False},
    )
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    assert parsed["meetings"][0]["facts"] == {}


def test_context_export_include_summary_false(api_client) -> None:
    """include_summary=False omits summary from meeting output."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(action_items=[_ai("T")], summary="Very important summary"),
        Path("/tmp/nosum.md"),
    )
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "format": "json", "include_summary": False},
    )
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    assert "summary" not in parsed["meetings"][0]


def test_context_export_include_transcript_false_default(api_client) -> None:
    """Transcript not included by default."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[_ai("T")],
            segments=[TranscriptSegment(0.0, 1.0, "secret words", speaker="Bob")],
        ),
        Path("/tmp/notransc.md"),
    )
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post("/api/context/export", json={"task_ids": [task_id], "format": "json"})
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    assert "transcript" not in parsed["meetings"][0]


def test_context_export_include_transcript_true(api_client) -> None:
    """include_transcript=True includes transcript segments."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[_ai("T")],
            segments=[TranscriptSegment(0.0, 1.0, "spoken text", speaker="Carol")],
        ),
        Path("/tmp/ytransc.md"),
    )
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "format": "json", "include_transcript": True},
    )
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    mtg = parsed["meetings"][0]
    assert "transcript" in mtg
    assert any(seg["text"] == "spoken text" for seg in mtg["transcript"])


def test_context_export_markdown_json_semantic_equivalence(api_client) -> None:
    """Markdown and JSON exports contain the same task count and meeting count."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(
        _meeting(
            action_items=[_ai("TaskX"), _ai("TaskY")],
            facts=[_fact("D", kind="decision")],
        ),
        Path("/tmp/equiv.md"),
    )
    all_tasks = s.list_tasks("aberto", scope="all")
    ids = [t["id"] for t in all_tasks]

    r_md = tc.post(
        "/api/context/export",
        json={"task_ids": ids, "objective": "test", "format": "markdown"},
    )
    r_js = tc.post(
        "/api/context/export",
        json={"task_ids": ids, "objective": "test", "format": "json"},
    )
    assert r_md.status_code == 200
    assert r_js.status_code == 200

    md_body = r_md.json()
    js_body = r_js.json()

    assert md_body["task_count"] == js_body["task_count"]
    assert md_body["meeting_count"] == js_body["meeting_count"]
    assert md_body["task_count"] == len(ids)

    parsed_json = json.loads(js_body["content"])
    assert len(parsed_json["tasks"]) == len(ids)
    assert len(parsed_json["meetings"]) == 1

    # Both contain the task text
    for t in all_tasks:
        assert t["what"] in md_body["content"]
        assert any(j["what"] == t["what"] for j in parsed_json["tasks"])


def test_context_export_filename_deterministic(api_client) -> None:
    """Same task_ids produce identical filename across calls."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(_meeting(action_items=[_ai("DetTask")]), Path("/tmp/det.md"))
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r1 = tc.post("/api/context/export", json={"task_ids": [task_id]})
    r2 = tc.post("/api/context/export", json={"task_ids": [task_id]})
    assert r1.json()["filename"] == r2.json()["filename"]


def test_context_export_objective_in_markdown(api_client) -> None:
    """objective string appears in markdown content."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(_meeting(action_items=[_ai("ObjTask")]), Path("/tmp/obj.md"))
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "objective": "Ship the feature", "format": "markdown"},
    )
    assert r.status_code == 200
    assert "Ship the feature" in r.json()["content"]


def test_context_export_objective_in_json(api_client) -> None:
    """objective string appears in JSON content."""
    tc, db = api_client
    s = Store(db)
    s.save_meeting(_meeting(action_items=[_ai("ObjJ")]), Path("/tmp/objj.md"))
    task_id = s.list_tasks("aberto", scope="all")[0]["id"]

    r = tc.post(
        "/api/context/export",
        json={"task_ids": [task_id], "objective": "Fix the bug", "format": "json"},
    )
    assert r.status_code == 200
    parsed = json.loads(r.json()["content"])
    assert parsed["objective"] == "Fix the bug"
