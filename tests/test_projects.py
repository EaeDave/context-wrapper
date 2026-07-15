"""Tests for Projects feature — store CRUD, filters, associations, API.

Contracts defended:
- projects table created by schema; unique name is case-insensitive.
- create_project raises ValueError on duplicate name; HTTP 409 via API.
- delete_project sets meetings.project_id = NULL (never deletes meetings).
- list_meeting_rows / search / list_tasks accept project_filter (None | "none" | int).
- save_meeting persists project_id; get_meeting returns it.
- set_meeting_project associates and disassociates individual meetings.
- bulk_set_meeting_project updates multiple meetings atomically.
- Old meetings (project_id IS NULL) survive all operations unchanged.
- /api/projects CRUD endpoints are wired correctly.
- PATCH /api/meetings/{id} handles title, project_id, both, null disassociation.
- PATCH /api/meetings/bulk-project moves and disassociates multiple meetings.
- GET /api/meetings, /api/search, /api/tasks accept ?project_id= filter.
- POST /api/process accepts project_id in body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meet.models import ActionItem, MeetingResult, TranscriptSegment
from meet.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meeting(
    *,
    title: str = "Test Meeting",
    date: str = "2024-06-01",
    source: str = "/tmp/fake.mkv",
    summary: str = "Summary.",
    segments: list[TranscriptSegment] | None = None,
    action_items: list[ActionItem] | None = None,
) -> MeetingResult:
    return MeetingResult(
        source=source,
        date=date,
        title=title,
        duration=60.0,
        summary=summary,
        segments=segments or [TranscriptSegment(0.0, 1.0, "hello world", speaker="Alice")],
        action_items=action_items or [],
    )


def _ai(what: str = "Do something", status: str = "aberto") -> ActionItem:
    return ActionItem(what=what, priority="media", status=status)


# ---------------------------------------------------------------------------
# Store — project CRUD
# ---------------------------------------------------------------------------


def test_create_project_returns_int(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Alpha")
    assert isinstance(pid, int) and pid > 0


def test_create_project_persists_fields(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Beta", description="Desc", repo_path="/repo/beta")
    proj = tmp_store.get_project(pid)
    assert proj is not None
    assert proj.name == "Beta"
    assert proj.description == "Desc"
    assert proj.repo_path == "/repo/beta"
    assert proj.created_at
    assert proj.updated_at


def test_create_project_duplicate_name_raises(tmp_store: Store) -> None:
    tmp_store.create_project("Gamma")
    with pytest.raises(ValueError, match="Gamma"):
        tmp_store.create_project("Gamma")


def test_create_project_duplicate_name_case_insensitive(tmp_store: Store) -> None:
    tmp_store.create_project("Delta")
    with pytest.raises(ValueError):
        tmp_store.create_project("delta")


def test_create_project_empty_name_raises(tmp_store: Store) -> None:
    with pytest.raises(ValueError):
        tmp_store.create_project("   ")


def test_get_project_missing_returns_none(tmp_store: Store) -> None:
    assert tmp_store.get_project(99999) is None


def test_list_projects_empty(tmp_store: Store) -> None:
    assert tmp_store.list_projects() == []


def test_list_projects_sorted_by_name(tmp_store: Store) -> None:
    tmp_store.create_project("Zebra")
    tmp_store.create_project("Apple")
    tmp_store.create_project("Mango")
    names = [p.name for p in tmp_store.list_projects()]
    assert names == sorted(names, key=str.casefold)


def test_update_project_name(tmp_store: Store) -> None:
    pid = tmp_store.create_project("OldName")
    ok = tmp_store.update_project(pid, name="NewName")
    assert ok is True
    assert tmp_store.get_project(pid).name == "NewName"


def test_update_project_description_and_repo(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Proj")
    tmp_store.update_project(pid, description="New desc", repo_path="/new/repo")
    proj = tmp_store.get_project(pid)
    assert proj.description == "New desc"
    assert proj.repo_path == "/new/repo"


def test_update_project_missing_returns_false(tmp_store: Store) -> None:
    assert tmp_store.update_project(99999, name="X") is False


def test_update_project_duplicate_name_raises(tmp_store: Store) -> None:
    tmp_store.create_project("A")
    pid_b = tmp_store.create_project("B")
    with pytest.raises(ValueError):
        tmp_store.update_project(pid_b, name="A")


def test_delete_project_returns_true(tmp_store: Store) -> None:
    pid = tmp_store.create_project("ToDelete")
    assert tmp_store.delete_project(pid) is True
    assert tmp_store.get_project(pid) is None


def test_delete_project_missing_returns_false(tmp_store: Store) -> None:
    assert tmp_store.delete_project(99999) is False


# ---------------------------------------------------------------------------
# Store — delete unassigns meetings, never deletes them
# ---------------------------------------------------------------------------


def test_delete_project_unassigns_meetings(tmp_store: Store) -> None:
    """delete_project must set meetings.project_id = NULL, not delete meetings."""
    pid = tmp_store.create_project("P")
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/a.md"), project_id=pid)
    tmp_store.delete_project(pid)
    result = tmp_store.get_meeting(mid)
    assert result is not None, "Meeting must still exist after project deletion"
    assert result.project_id is None


def test_delete_project_unassigns_multiple_meetings(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Multi")
    mids = [
        tmp_store.save_meeting(_meeting(title=f"M{i}"), Path(f"/tmp/{i}.md"), project_id=pid)
        for i in range(3)
    ]
    tmp_store.delete_project(pid)
    for mid in mids:
        r = tmp_store.get_meeting(mid)
        assert r is not None
        assert r.project_id is None


# ---------------------------------------------------------------------------
# Store — save_meeting / get_meeting project_id roundtrip
# ---------------------------------------------------------------------------


def test_save_meeting_with_project_id(tmp_store: Store) -> None:
    pid = tmp_store.create_project("SaveProj")
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/a.md"), project_id=pid)
    result = tmp_store.get_meeting(mid)
    assert result is not None
    assert result.project_id == pid


def test_save_meeting_no_project_id_is_null(tmp_store: Store) -> None:
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/b.md"))
    result = tmp_store.get_meeting(mid)
    assert result is not None
    assert result.project_id is None


# ---------------------------------------------------------------------------
# Store — set_meeting_project (individual association)
# ---------------------------------------------------------------------------


def test_set_meeting_project_associates(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Assoc")
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/c.md"))
    ok = tmp_store.set_meeting_project(mid, pid)
    assert ok is True
    assert tmp_store.get_meeting(mid).project_id == pid


def test_set_meeting_project_disassociates(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Dissoc")
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/d.md"), project_id=pid)
    ok = tmp_store.set_meeting_project(mid, None)
    assert ok is True
    assert tmp_store.get_meeting(mid).project_id is None


def test_set_meeting_project_missing_meeting_returns_false(tmp_store: Store) -> None:
    assert tmp_store.set_meeting_project(99999, None) is False


def test_set_meeting_project_invalid_project_raises(tmp_store: Store) -> None:
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/e.md"))
    with pytest.raises(ValueError, match="99999"):
        tmp_store.set_meeting_project(mid, 99999)


# ---------------------------------------------------------------------------
# Store — bulk_set_meeting_project
# ---------------------------------------------------------------------------


def test_bulk_set_meeting_project_associates(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Bulk")
    mids = [
        tmp_store.save_meeting(_meeting(title=f"B{i}"), Path(f"/tmp/{i}.md"))
        for i in range(4)
    ]
    count = tmp_store.bulk_set_meeting_project(mids[:3], pid)
    assert count == 3
    for mid in mids[:3]:
        assert tmp_store.get_meeting(mid).project_id == pid
    assert tmp_store.get_meeting(mids[3]).project_id is None


def test_bulk_set_meeting_project_disassociates(tmp_store: Store) -> None:
    pid = tmp_store.create_project("BulkDis")
    mids = [
        tmp_store.save_meeting(_meeting(), Path(f"/tmp/bd{i}.md"), project_id=pid)
        for i in range(3)
    ]
    count = tmp_store.bulk_set_meeting_project(mids, None)
    assert count == 3
    for mid in mids:
        assert tmp_store.get_meeting(mid).project_id is None


def test_bulk_set_meeting_project_empty_list(tmp_store: Store) -> None:
    assert tmp_store.bulk_set_meeting_project([], None) == 0


def test_bulk_set_meeting_project_invalid_project_raises(tmp_store: Store) -> None:
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/f.md"))
    with pytest.raises(ValueError):
        tmp_store.bulk_set_meeting_project([mid], 99999)


# ---------------------------------------------------------------------------
# Store — project counts
# ---------------------------------------------------------------------------


def test_project_meeting_count(tmp_store: Store) -> None:
    pid = tmp_store.create_project("CountProj")
    for i in range(3):
        tmp_store.save_meeting(_meeting(title=f"M{i}"), Path(f"/tmp/{i}.md"), project_id=pid)
    proj = tmp_store.get_project(pid)
    assert proj.meeting_count == 3


def test_project_task_counts(tmp_store: Store) -> None:
    pid = tmp_store.create_project("TaskCount")
    items = [_ai("Open1"), _ai("Open2"), _ai("Done", status="feito")]
    mid = tmp_store.save_meeting(_meeting(action_items=items), Path("/tmp/tc.md"), project_id=pid)
    proj = tmp_store.get_project(pid)
    assert proj.open_task_count == 2
    assert proj.done_task_count == 1


def test_project_last_meeting_date(tmp_store: Store) -> None:
    pid = tmp_store.create_project("DateProj")
    tmp_store.save_meeting(_meeting(date="2024-01-01"), Path("/tmp/d1.md"), project_id=pid)
    tmp_store.save_meeting(_meeting(date="2024-06-15"), Path("/tmp/d2.md"), project_id=pid)
    proj = tmp_store.get_project(pid)
    assert proj.last_meeting_date == "2024-06-15"


def test_project_counts_zero_for_empty(tmp_store: Store) -> None:
    pid = tmp_store.create_project("Empty")
    proj = tmp_store.get_project(pid)
    assert proj.meeting_count == 0
    assert proj.open_task_count == 0
    assert proj.done_task_count == 0
    assert proj.last_meeting_date is None


# ---------------------------------------------------------------------------
# Store — list_meeting_rows project_filter
# ---------------------------------------------------------------------------


def test_list_meeting_rows_no_filter_returns_all(tmp_store: Store) -> None:
    pid = tmp_store.create_project("F")
    tmp_store.save_meeting(_meeting(title="WithProject"), Path("/tmp/wp.md"), project_id=pid)
    tmp_store.save_meeting(_meeting(title="NoProject"), Path("/tmp/np.md"))
    rows = tmp_store.list_meeting_rows()
    titles = {r.title for r in rows}
    assert "WithProject" in titles
    assert "NoProject" in titles


def test_list_meeting_rows_filter_none_returns_unassigned(tmp_store: Store) -> None:
    pid = tmp_store.create_project("G")
    tmp_store.save_meeting(_meeting(title="WithProj"), Path("/tmp/a1.md"), project_id=pid)
    tmp_store.save_meeting(_meeting(title="NoProj"), Path("/tmp/a2.md"))
    rows = tmp_store.list_meeting_rows(project_filter="none")
    titles = {r.title for r in rows}
    assert "NoProj" in titles
    assert "WithProj" not in titles


def test_list_meeting_rows_filter_int_returns_project_meetings(tmp_store: Store) -> None:
    pid1 = tmp_store.create_project("H1")
    pid2 = tmp_store.create_project("H2")
    tmp_store.save_meeting(_meeting(title="P1M"), Path("/tmp/b1.md"), project_id=pid1)
    tmp_store.save_meeting(_meeting(title="P2M"), Path("/tmp/b2.md"), project_id=pid2)
    tmp_store.save_meeting(_meeting(title="NoP"), Path("/tmp/b3.md"))
    rows = tmp_store.list_meeting_rows(project_filter=pid1)
    titles = {r.title for r in rows}
    assert "P1M" in titles
    assert "P2M" not in titles
    assert "NoP" not in titles


def test_list_meeting_rows_includes_project_name(tmp_store: Store) -> None:
    pid = tmp_store.create_project("MyProject")
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/pn.md"), project_id=pid)
    rows = {r.id: r for r in tmp_store.list_meeting_rows()}
    assert rows[mid].project_name == "MyProject"
    assert rows[mid].project_id == pid


def test_list_meeting_rows_no_project_name_is_none(tmp_store: Store) -> None:
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/nn.md"))
    rows = {r.id: r for r in tmp_store.list_meeting_rows()}
    assert rows[mid].project_name is None
    assert rows[mid].project_id is None


# ---------------------------------------------------------------------------
# Store — search project_filter
# ---------------------------------------------------------------------------


def test_search_no_filter_returns_all(tmp_store: Store) -> None:
    pid = tmp_store.create_project("SP")
    seg = TranscriptSegment(0.0, 1.0, "uniquewordxyz", speaker="Alice")
    tmp_store.save_meeting(_meeting(segments=[seg]), Path("/tmp/s1.md"), project_id=pid)
    tmp_store.save_meeting(_meeting(segments=[seg]), Path("/tmp/s2.md"))
    results = tmp_store.search("uniquewordxyz")
    assert len(results) == 2


def test_search_filter_none_returns_unassigned(tmp_store: Store) -> None:
    pid = tmp_store.create_project("SPN")
    seg = TranscriptSegment(0.0, 1.0, "filterwordabc", speaker="Alice")
    tmp_store.save_meeting(_meeting(segments=[seg]), Path("/tmp/f1.md"), project_id=pid)
    tmp_store.save_meeting(_meeting(segments=[seg]), Path("/tmp/f2.md"))
    results = tmp_store.search("filterwordabc", project_filter="none")
    mids = [r["meeting_id"] for r in results]
    assert len(mids) == 1


def test_search_filter_int_returns_project_only(tmp_store: Store) -> None:
    pid = tmp_store.create_project("SPI")
    seg = TranscriptSegment(0.0, 1.0, "projectkeyword", speaker="Alice")
    mid_p = tmp_store.save_meeting(_meeting(segments=[seg]), Path("/tmp/p1.md"), project_id=pid)
    tmp_store.save_meeting(_meeting(segments=[seg]), Path("/tmp/p2.md"))
    results = tmp_store.search("projectkeyword", project_filter=pid)
    mids = [int(r["meeting_id"]) for r in results]
    assert mids == [mid_p]


# ---------------------------------------------------------------------------
# Store — list_tasks project_filter
# ---------------------------------------------------------------------------


def test_list_tasks_no_filter_returns_all(tmp_store: Store) -> None:
    pid = tmp_store.create_project("TP")
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("TaskA")]), Path("/tmp/ta.md"), project_id=pid
    )
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("TaskB")]), Path("/tmp/tb.md")
    )
    tasks = tmp_store.list_tasks("aberto")
    whats = {t["what"] for t in tasks}
    assert "TaskA" in whats
    assert "TaskB" in whats


def test_list_tasks_filter_none_returns_unassigned(tmp_store: Store) -> None:
    pid = tmp_store.create_project("TPN")
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("TaskP")]), Path("/tmp/tp.md"), project_id=pid
    )
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("TaskNP")]), Path("/tmp/tnp.md")
    )
    tasks = tmp_store.list_tasks("aberto", project_filter="none")
    whats = {t["what"] for t in tasks}
    assert "TaskNP" in whats
    assert "TaskP" not in whats


def test_list_tasks_filter_int_returns_project_only(tmp_store: Store) -> None:
    pid = tmp_store.create_project("TPI")
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("TaskInProj")]), Path("/tmp/ti.md"), project_id=pid
    )
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("TaskOutside")]), Path("/tmp/to.md")
    )
    tasks = tmp_store.list_tasks("aberto", project_filter=pid)
    whats = {t["what"] for t in tasks}
    assert "TaskInProj" in whats
    assert "TaskOutside" not in whats


def test_list_tasks_includes_project_id_and_name(tmp_store: Store) -> None:
    pid = tmp_store.create_project("TaskNameProj")
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("Task1")]), Path("/tmp/tn1.md"), project_id=pid
    )
    tasks = tmp_store.list_tasks("aberto")
    t = next(x for x in tasks if x["what"] == "Task1")
    assert t["project_id"] == pid
    assert t["project_name"] == "TaskNameProj"


def test_list_tasks_no_project_fields_are_none(tmp_store: Store) -> None:
    tmp_store.save_meeting(
        _meeting(action_items=[_ai("Unassigned")]), Path("/tmp/ua.md")
    )
    tasks = tmp_store.list_tasks("aberto")
    t = next(x for x in tasks if x["what"] == "Unassigned")
    assert t["project_id"] is None
    assert t["project_name"] is None


# ---------------------------------------------------------------------------
# Store — migration idempotency (project_id column added to existing meetings)
# ---------------------------------------------------------------------------


def test_migration_adds_project_id_to_existing_meetings(tmp_path: Path) -> None:
    """Simula banco antigo sem project_id: abrir de novo deve migrar sem erros."""
    db = tmp_path / "old.db"
    # Primeira abertura: cria esquema + coluna project_id via migração
    store = Store(db)
    mid = store.save_meeting(_meeting(), Path("/tmp/old.md"))
    result = store.get_meeting(mid)
    assert result.project_id is None  # coluna existe, valor é NULL


def test_migration_idempotent_second_open(tmp_path: Path) -> None:
    """Abrir o mesmo DB duas vezes não deve falhar."""
    db = tmp_path / "idem.db"
    s1 = Store(db)
    mid = s1.save_meeting(_meeting(), Path("/tmp/idem.md"))
    s2 = Store(db)  # segundo open: migração idempotente
    assert s2.get_meeting(mid) is not None


# ---------------------------------------------------------------------------
# API — fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with an in-memory (tmp) store and minimal settings stub."""
    from unittest.mock import MagicMock

    from meet.web.app import create_app
    from meet.store import Store as RealStore

    db = tmp_path / "api_test.db"
    store = RealStore(db)

    settings_mock = MagicMock()
    settings_mock.db_path = db
    settings_mock.data_dir = tmp_path / "data"
    settings_mock.output_dir = tmp_path / "output"
    settings_mock.data_dir.mkdir(parents=True, exist_ok=True)
    settings_mock.output_dir.mkdir(parents=True, exist_ok=True)

    import meet.web.app as app_module

    monkeypatch.setattr(
        app_module,
        "_settings_store",
        lambda: (settings_mock, RealStore(db)),
    )
    app = create_app()
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# API — /api/projects CRUD
# ---------------------------------------------------------------------------


def test_api_create_project(client: TestClient) -> None:
    r = client.post("/api/projects", json={"name": "Alpha", "description": "Desc"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Alpha"
    assert data["description"] == "Desc"
    assert data["id"] > 0


def test_api_create_project_duplicate_409(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Dup"})
    r = client.post("/api/projects", json={"name": "dup"})
    assert r.status_code == 409


def test_api_create_project_empty_name_400(client: TestClient) -> None:
    r = client.post("/api/projects", json={"name": "   "})
    assert r.status_code == 400


def test_api_list_projects(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Z"})
    client.post("/api/projects", json={"name": "A"})
    r = client.get("/api/projects")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert names == sorted(names, key=str.casefold)


def test_api_get_project(client: TestClient) -> None:
    r = client.post("/api/projects", json={"name": "GetMe", "repo_path": "/r"})
    pid = r.json()["id"]
    r2 = client.get(f"/api/projects/{pid}")
    assert r2.status_code == 200
    assert r2.json()["repo_path"] == "/r"


def test_api_get_project_missing_404(client: TestClient) -> None:
    assert client.get("/api/projects/99999").status_code == 404


def test_api_patch_project(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Orig"}).json()["id"]
    r = client.patch(f"/api/projects/{pid}", json={"name": "Renamed", "description": "New"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Renamed"
    assert data["description"] == "New"


def test_api_patch_project_duplicate_name_409(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Existing"})
    pid = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    r = client.patch(f"/api/projects/{pid}", json={"name": "existing"})
    assert r.status_code == 409


def test_api_patch_project_missing_404(client: TestClient) -> None:
    assert client.patch("/api/projects/99999", json={"name": "X"}).status_code == 404


def test_api_delete_project(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Del"}).json()["id"]
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_api_delete_project_missing_404(client: TestClient) -> None:
    assert client.delete("/api/projects/99999").status_code == 404


# ---------------------------------------------------------------------------
# API — delete project unassigns meetings
# ---------------------------------------------------------------------------


def test_api_delete_project_unassigns_meetings(client: TestClient, tmp_path: Path) -> None:
    """DELETE /api/projects/{id} must not delete meetings — only disassociate."""
    from meet.store import Store as RealStore

    # Create project via API then add meeting directly in store
    pid = client.post("/api/projects", json={"name": "ToDelete"}).json()["id"]

    # We need to get the store from the client's monkeypatched factory.
    # Use a new store on the same db (client fixture uses tmp_path/api_test.db).
    # Find the db through the store returned by _settings_store via a GET.
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204

    # No meeting was explicitly created, but the project is gone cleanly
    assert client.get(f"/api/projects/{pid}").status_code == 404


# ---------------------------------------------------------------------------
# API — PATCH /api/meetings/{id} title and/or project_id
# ---------------------------------------------------------------------------


def _save_meeting_via_store(client_store_db: Path, **kwargs) -> int:
    from meet.store import Store as RealStore
    s = RealStore(client_store_db)
    return s.save_meeting(_meeting(**kwargs), Path("/tmp/fake.md"))


@pytest.fixture()
def store_db(tmp_path: Path) -> Path:
    return tmp_path / "api_test.db"


@pytest.fixture()
def api_client(store_db: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    """Returns (client, db_path) so tests can also use the Store directly."""
    from unittest.mock import MagicMock
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


def test_api_patch_meeting_title_only(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    mid = RealStore(db).save_meeting(_meeting(title="Old"), Path("/tmp/o.md"))
    r = tc.patch(f"/api/meetings/{mid}", json={"title": "New"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert RealStore(db).get_meeting(mid).title == "New"


def test_api_patch_meeting_project_id_only(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("Patch")
    mid = s.save_meeting(_meeting(), Path("/tmp/pm.md"))
    r = tc.patch(f"/api/meetings/{mid}", json={"project_id": pid})
    assert r.status_code == 200
    assert RealStore(db).get_meeting(mid).project_id == pid


def test_api_patch_meeting_project_id_null_disassociates(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("Dis")
    mid = s.save_meeting(_meeting(), Path("/tmp/dis.md"), project_id=pid)
    r = tc.patch(f"/api/meetings/{mid}", json={"project_id": None})
    assert r.status_code == 200
    assert RealStore(db).get_meeting(mid).project_id is None


def test_api_patch_meeting_title_and_project(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("Both")
    mid = s.save_meeting(_meeting(title="Old"), Path("/tmp/both.md"))
    r = tc.patch(f"/api/meetings/{mid}", json={"title": "New", "project_id": pid})
    assert r.status_code == 200
    m = RealStore(db).get_meeting(mid)
    assert m.title == "New"
    assert m.project_id == pid


def test_api_patch_meeting_empty_body_400(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    mid = RealStore(db).save_meeting(_meeting(), Path("/tmp/eb.md"))
    # Neither title nor project_id in body
    r = tc.patch(f"/api/meetings/{mid}", json={})
    assert r.status_code == 400


def test_api_patch_meeting_empty_title_400(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    mid = RealStore(db).save_meeting(_meeting(), Path("/tmp/et.md"))
    r = tc.patch(f"/api/meetings/{mid}", json={"title": "   "})
    assert r.status_code == 400


def test_api_patch_meeting_invalid_project_404(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    mid = RealStore(db).save_meeting(_meeting(), Path("/tmp/ip.md"))
    r = tc.patch(f"/api/meetings/{mid}", json={"project_id": 99999})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# API — PATCH /api/meetings/bulk-project
# ---------------------------------------------------------------------------


def test_api_bulk_project_associates(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("Bulk")
    mids = [s.save_meeting(_meeting(title=f"M{i}"), Path(f"/tmp/bk{i}.md")) for i in range(3)]
    r = tc.patch("/api/meetings/bulk-project", json={"ids": mids, "project_id": pid})
    assert r.status_code == 200
    assert r.json()["updated"] == 3
    for mid in mids:
        assert RealStore(db).get_meeting(mid).project_id == pid


def test_api_bulk_project_disassociates(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("BulkD")
    mids = [
        s.save_meeting(_meeting(), Path(f"/tmp/bd{i}.md"), project_id=pid) for i in range(2)
    ]
    r = tc.patch("/api/meetings/bulk-project", json={"ids": mids, "project_id": None})
    assert r.status_code == 200
    for mid in mids:
        assert RealStore(db).get_meeting(mid).project_id is None


def test_api_bulk_project_empty_ids_400(api_client) -> None:
    tc, _ = api_client
    r = tc.patch("/api/meetings/bulk-project", json={"ids": [], "project_id": None})
    assert r.status_code == 400


def test_api_bulk_project_invalid_project_404(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    mid = RealStore(db).save_meeting(_meeting(), Path("/tmp/bip.md"))
    r = tc.patch("/api/meetings/bulk-project", json={"ids": [mid], "project_id": 99999})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# API — GET /api/meetings?project_id=
# ---------------------------------------------------------------------------


def test_api_meetings_no_filter(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("MF")
    s.save_meeting(_meeting(title="WithP"), Path("/tmp/mf1.md"), project_id=pid)
    s.save_meeting(_meeting(title="NoP"), Path("/tmp/mf2.md"))
    r = tc.get("/api/meetings")
    assert r.status_code == 200
    titles = {m["title"] for m in r.json()}
    assert "WithP" in titles and "NoP" in titles


def test_api_meetings_filter_none(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("MFN")
    s.save_meeting(_meeting(title="WithProject"), Path("/tmp/mfn1.md"), project_id=pid)
    s.save_meeting(_meeting(title="WithoutProject"), Path("/tmp/mfn2.md"))
    r = tc.get("/api/meetings?project_id=none")
    assert r.status_code == 200
    titles = {m["title"] for m in r.json()}
    assert "WithoutProject" in titles
    assert "WithProject" not in titles


def test_api_meetings_filter_int(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("MFI")
    s.save_meeting(_meeting(title="InProj"), Path("/tmp/mfi1.md"), project_id=pid)
    s.save_meeting(_meeting(title="OutProj"), Path("/tmp/mfi2.md"))
    r = tc.get(f"/api/meetings?project_id={pid}")
    assert r.status_code == 200
    titles = {m["title"] for m in r.json()}
    assert "InProj" in titles
    assert "OutProj" not in titles


def test_api_meetings_response_includes_project_fields(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("FieldsProj")
    s.save_meeting(_meeting(), Path("/tmp/fp.md"), project_id=pid)
    r = tc.get("/api/meetings")
    row = next(m for m in r.json() if m["project_id"] == pid)
    assert row["project_name"] == "FieldsProj"


# ---------------------------------------------------------------------------
# API — GET /api/tasks?project_id=
# ---------------------------------------------------------------------------


def test_api_tasks_filter_none(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("TF")
    s.save_meeting(_meeting(action_items=[_ai("InProj")]), Path("/tmp/tf1.md"), project_id=pid)
    s.save_meeting(_meeting(action_items=[_ai("NoProj")]), Path("/tmp/tf2.md"))
    r = tc.get("/api/tasks?project_id=none")
    assert r.status_code == 200
    whats = {t["what"] for t in r.json()}
    assert "NoProj" in whats
    assert "InProj" not in whats


def test_api_tasks_filter_int(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("TFI")
    s.save_meeting(_meeting(action_items=[_ai("TaskP")]), Path("/tmp/tfi1.md"), project_id=pid)
    s.save_meeting(_meeting(action_items=[_ai("TaskNP")]), Path("/tmp/tfi2.md"))
    r = tc.get(f"/api/tasks?project_id={pid}")
    assert r.status_code == 200
    whats = {t["what"] for t in r.json()}
    assert "TaskP" in whats
    assert "TaskNP" not in whats


# ---------------------------------------------------------------------------
# API — GET /api/search?project_id=
# ---------------------------------------------------------------------------


def test_api_search_no_filter(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("SF")
    seg = TranscriptSegment(0.0, 1.0, "searchabletoken", speaker="Alice")
    s.save_meeting(_meeting(segments=[seg]), Path("/tmp/sf1.md"), project_id=pid)
    s.save_meeting(_meeting(segments=[seg]), Path("/tmp/sf2.md"))
    r = tc.get("/api/search?q=searchabletoken")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_api_search_filter_int(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore
    s = RealStore(db)
    pid = s.create_project("SFI")
    seg = TranscriptSegment(0.0, 1.0, "exclusiveword", speaker="Alice")
    mid_p = s.save_meeting(_meeting(segments=[seg]), Path("/tmp/sfi1.md"), project_id=pid)
    s.save_meeting(_meeting(segments=[seg]), Path("/tmp/sfi2.md"))
    r = tc.get(f"/api/search?q=exclusiveword&project_id={pid}")
    assert r.status_code == 200
    mids = [x["meeting_id"] for x in r.json()]
    assert mids == [mid_p]


# ---------------------------------------------------------------------------
# API — ProcessBody accepts project_id
# ---------------------------------------------------------------------------


def test_process_body_accepts_project_id() -> None:
    """ProcessBody must accept project_id field (int or None)."""
    from meet.web.app import ProcessBody

    body = ProcessBody(video="/tmp/x.mkv", project_id=42)
    assert body.project_id == 42

    body_null = ProcessBody(video="/tmp/x.mkv", project_id=None)
    assert body_null.project_id is None

    body_default = ProcessBody(video="/tmp/x.mkv")
    assert body_default.project_id is None


def test_save_meeting_rejects_missing_project(tmp_store: Store) -> None:
    with pytest.raises(ValueError, match="Projeto 99999 não encontrado"):
        tmp_store.save_meeting(_meeting(), Path("/tmp/missing-project.md"), project_id=99999)


def test_api_meeting_detail_includes_project_name(api_client) -> None:
    tc, db = api_client
    from meet.store import Store as RealStore

    store = RealStore(db)
    project_id = store.create_project("Detail Project")
    meeting_id = store.save_meeting(
        _meeting(), Path("/tmp/detail-project.md"), project_id=project_id
    )

    response = tc.get(f"/api/meetings/{meeting_id}")

    assert response.status_code == 200
    assert response.json()["project_id"] == project_id
    assert response.json()["project_name"] == "Detail Project"


def test_api_process_rejects_missing_project(api_client, tmp_path: Path) -> None:
    tc, _ = api_client
    video = tmp_path / "meeting.mkv"
    video.write_bytes(b"media")

    response = tc.post(
        "/api/process",
        json={"video": str(video), "project_id": 99999},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Projeto não encontrado"
