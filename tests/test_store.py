"""Tests for meet.store.Store — SQLite persistence with FTS5.

Contracts defended:
- save_meeting / get_meeting roundtrip: all MeetingResult fields survive.
- Segments in get_meeting are ordered by start (ascending).
- get_meeting returns None for missing ids.
- list_meetings returns (id, date, title) tuples, ordered newest-first.
- search() finds text from segments (kind='segment') and action items
  (kind='action_item'), with the correct kind in each hit.
- update_speaker renames in segments and re-indexes FTS without duplicating
  existing FTS rows.
- Voices: upsert / get / all_voices / delete roundtrip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meet.models import ActionItem, MeetingResult, TranscriptSegment
from meet import media
from meet.store import Store


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _meeting(
    *,
    title: str = "Reunião",
    date: str = "2024-01-15",
    source: str = "test.mkv",
    duration: float = 3600.0,
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
# save / get roundtrip
# ---------------------------------------------------------------------------

def test_save_returns_int_id(tmp_store: Store) -> None:
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/out.md"))
    assert isinstance(mid, int)
    assert mid > 0


def test_get_meeting_fields_roundtrip(tmp_store: Store) -> None:
    """Core scalar fields survive save → get."""
    m = _meeting(
        title="Planning",
        date="2024-03-01",
        source="recording.mkv",
        duration=1800.0,
        summary="We decided stuff.",
    )
    mid = tmp_store.save_meeting(m, Path("/tmp/planning.md"))
    got = tmp_store.get_meeting(mid)

    assert got is not None
    assert got.title == "Planning"
    assert got.date == "2024-03-01"
    assert got.source == "recording.mkv"
    assert got.duration == pytest.approx(1800.0)
    assert got.summary == "We decided stuff."


def test_get_meeting_segments_roundtrip(tmp_store: Store) -> None:
    """Segment fields (start, end, speaker, text) survive save → get."""
    segs = [
        TranscriptSegment(start=0.0, end=1.0, text="Olá", speaker="Alice"),
        TranscriptSegment(start=2.0, end=3.5, text="Tudo bem", speaker="Bob"),
    ]
    mid = tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))
    got = tmp_store.get_meeting(mid)

    assert got is not None
    assert len(got.segments) == 2
    assert got.segments[0].text == "Olá"
    assert got.segments[0].speaker == "Alice"
    assert got.segments[1].speaker == "Bob"


def test_get_meeting_segments_ordered_by_start(tmp_store: Store) -> None:
    """Segments returned from get_meeting are sorted by start, not insertion order."""
    segs = [
        TranscriptSegment(start=10.0, end=11.0, text="late"),
        TranscriptSegment(start=1.0,  end=2.0,  text="early"),
        TranscriptSegment(start=5.0,  end=6.0,  text="mid"),
    ]
    mid = tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))
    got = tmp_store.get_meeting(mid)

    assert got is not None
    starts = [s.start for s in got.segments]
    assert starts == sorted(starts)
    assert got.segments[0].text == "early"


def test_get_meeting_action_items_roundtrip(tmp_store: Store) -> None:
    """Action item fields survive save → get."""
    items = [
        ActionItem(what="Deploy API", where="/v1", details="Use TLS", requested_by="Alice", priority="alta"),
        ActionItem(what="Fix docs"),
    ]
    mid = tmp_store.save_meeting(_meeting(action_items=items), Path("/tmp/x.md"))
    got = tmp_store.get_meeting(mid)

    assert got is not None
    assert len(got.action_items) == 2
    ai0 = got.action_items[0]
    assert ai0.what == "Deploy API"
    assert ai0.where == "/v1"
    assert ai0.details == "Use TLS"
    assert ai0.requested_by == "Alice"
    assert ai0.priority == "alta"
    ai1 = got.action_items[1]
    assert ai1.what == "Fix docs"
    assert ai1.where is None
    assert ai1.priority == "media"  # store preserves whatever was saved


def test_get_meeting_md_path_attribute(tmp_store: Store) -> None:
    """get_meeting attaches .md_path as a Path attribute on the result."""
    md = Path("/home/user/reunioes/2024-01-15-planning.md")
    mid = tmp_store.save_meeting(_meeting(), md)
    got = tmp_store.get_meeting(mid)
    assert got is not None
    assert got.md_path == md  # type: ignore[attr-defined]


def test_get_meeting_missing_returns_none(tmp_store: Store) -> None:
    got = tmp_store.get_meeting(99999)
    assert got is None


# ---------------------------------------------------------------------------
# list_meetings
# ---------------------------------------------------------------------------

def test_list_meetings_empty(tmp_store: Store) -> None:
    assert tmp_store.list_meetings() == []


def test_list_meetings_returns_tuples(tmp_store: Store) -> None:
    mid = tmp_store.save_meeting(_meeting(title="First"), Path("/tmp/a.md"))
    rows = tmp_store.list_meetings()
    assert len(rows) == 1
    assert rows[0][0] == mid
    assert rows[0][2] == "First"


def test_list_meetings_ordered_newest_first(tmp_store: Store) -> None:
    """Meetings ordered by date DESC, id DESC."""
    tmp_store.save_meeting(_meeting(date="2024-01-01", title="Old"), Path("/tmp/a.md"))
    tmp_store.save_meeting(_meeting(date="2024-06-01", title="New"), Path("/tmp/b.md"))
    rows = tmp_store.list_meetings()
    assert rows[0][1] == "2024-06-01"
    assert rows[1][1] == "2024-01-01"


# ---------------------------------------------------------------------------
# search (FTS5)
# ---------------------------------------------------------------------------

def test_search_finds_segment_text(tmp_store: Store) -> None:
    """A word present in a segment's text is found by search()."""
    segs = [TranscriptSegment(start=0, end=1, text="python programming is cool")]
    mid = tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))

    results = tmp_store.search("python")
    assert len(results) >= 1
    meeting_ids = [r["meeting_id"] for r in results]
    assert mid in meeting_ids


def test_search_finds_action_item_text(tmp_store: Store) -> None:
    """A word present in an action item is found by search()."""
    items = [ActionItem(what="deploy endpoint infrastructure")]
    mid = tmp_store.save_meeting(_meeting(action_items=items), Path("/tmp/x.md"))

    results = tmp_store.search("infrastructure")
    assert len(results) >= 1
    assert mid in [r["meeting_id"] for r in results]


def test_search_segment_kind_correct(tmp_store: Store) -> None:
    """Hits from segment text have kind='segment'."""
    segs = [TranscriptSegment(start=0, end=1, text="uniquesegwordxyz")]
    tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))

    results = tmp_store.search("uniquesegwordxyz")
    kinds = {r["kind"] for r in results}
    assert "segment" in kinds


def test_search_action_item_kind_correct(tmp_store: Store) -> None:
    """Hits from action item text have kind='action_item'."""
    items = [ActionItem(what="uniqueactionwordxyz")]
    tmp_store.save_meeting(_meeting(action_items=items), Path("/tmp/x.md"))

    results = tmp_store.search("uniqueactionwordxyz")
    kinds = {r["kind"] for r in results}
    assert "action_item" in kinds


def test_search_missing_term_returns_empty(tmp_store: Store) -> None:
    """A query that matches nothing returns an empty list (not an error)."""
    segs = [TranscriptSegment(start=0, end=1, text="hello world")]
    tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))

    results = tmp_store.search("zzznomatchzzz")
    assert results == []


def test_search_result_contains_expected_keys(tmp_store: Store) -> None:
    """Each search result dict has meeting_id, date, title, kind, snippet."""
    segs = [TranscriptSegment(start=0, end=1, text="uniquekeywordcheck")]
    tmp_store.save_meeting(_meeting(date="2024-05-10", title="KeyCheck", segments=segs), Path("/x.md"))

    results = tmp_store.search("uniquekeywordcheck")
    assert len(results) >= 1
    r = results[0]
    assert "meeting_id" in r
    assert "date" in r
    assert "title" in r
    assert "kind" in r
    assert "snippet" in r


# ---------------------------------------------------------------------------
# update_speaker
# ---------------------------------------------------------------------------

def test_update_speaker_renames_segments(tmp_store: Store) -> None:
    """update_speaker changes the speaker field in every matching segment."""
    segs = [
        TranscriptSegment(start=0, end=1, text="hi", speaker="SPEAKER_00"),
        TranscriptSegment(start=1, end=2, text="bye", speaker="SPEAKER_00"),
        TranscriptSegment(start=2, end=3, text="ok", speaker="SPEAKER_01"),
    ]
    mid = tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))

    tmp_store.update_speaker(mid, "SPEAKER_00", "Alice")
    got = tmp_store.get_meeting(mid)
    assert got is not None

    speakers = {s.speaker for s in got.segments}
    assert "Alice" in speakers
    assert "SPEAKER_00" not in speakers
    assert "SPEAKER_01" in speakers  # unrelated speaker unchanged


def test_update_speaker_fts_reindexed_no_duplicate(tmp_store: Store) -> None:
    """After update_speaker, searching for segment text returns exactly one hit
    (FTS rows were deleted and re-inserted, not doubled)."""
    unique_word = "abrakadabra99"
    segs = [TranscriptSegment(start=0, end=1, text=unique_word, speaker="SPEAKER_00")]
    mid = tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))

    # Two renames: each triggers a FTS delete+reinsert
    tmp_store.update_speaker(mid, "SPEAKER_00", "Alice")
    tmp_store.update_speaker(mid, "Alice", "Bob")

    results = tmp_store.search(unique_word)
    assert len(results) == 1  # not duplicated


def test_update_speaker_fts_still_searchable(tmp_store: Store) -> None:
    """Segment text remains searchable after a speaker rename."""
    segs = [TranscriptSegment(start=0, end=1, text="searchabletoken", speaker="OLD")]
    mid = tmp_store.save_meeting(_meeting(segments=segs), Path("/tmp/x.md"))

    tmp_store.update_speaker(mid, "OLD", "NEW")

    results = tmp_store.search("searchabletoken")
    assert any(r["meeting_id"] == mid for r in results)


# ---------------------------------------------------------------------------
# Voices: upsert / get / all_voices / delete
# ---------------------------------------------------------------------------

def test_voices_upsert_and_get(tmp_store: Store) -> None:
    blob = b"\x00\x01\x02\x03"
    tmp_store.upsert_voice("Alice", blob)
    assert tmp_store.get_voice("Alice") == blob


def test_voices_upsert_replaces_existing(tmp_store: Store) -> None:
    tmp_store.upsert_voice("Alice", b"old")
    tmp_store.upsert_voice("Alice", b"new")
    assert tmp_store.get_voice("Alice") == b"new"


def test_voices_all_voices_returns_dict(tmp_store: Store) -> None:
    tmp_store.upsert_voice("Alice", b"a")
    tmp_store.upsert_voice("Bob",   b"b")
    all_v = tmp_store.all_voices()
    assert all_v == {"Alice": b"a", "Bob": b"b"}


def test_voices_all_voices_empty(tmp_store: Store) -> None:
    assert tmp_store.all_voices() == {}


def test_voices_get_missing_returns_none(tmp_store: Store) -> None:
    assert tmp_store.get_voice("nobody") is None


def test_voices_delete(tmp_store: Store) -> None:
    tmp_store.upsert_voice("Alice", b"data")
    tmp_store.delete_voice("Alice")
    assert tmp_store.get_voice("Alice") is None


def test_voices_delete_nonexistent_is_noop(tmp_store: Store) -> None:
    """Deleting a name that doesn't exist must not raise."""
    tmp_store.delete_voice("ghost")  # should not raise


def test_voices_isolation_between_meetings(tmp_store: Store) -> None:
    """Voices are independent of meetings — enrolling a voice does not affect
    segment or meeting data."""
    mid = tmp_store.save_meeting(_meeting(), Path("/tmp/x.md"))
    tmp_store.upsert_voice("Alice", b"emb")

    got = tmp_store.get_meeting(mid)
    assert got is not None  # meeting still retrievable
    assert tmp_store.get_voice("Alice") == b"emb"  # voice still correct


# ---------------------------------------------------------------------------
# Media / CRUD
# ---------------------------------------------------------------------------

def test_list_meeting_rows_media_status(tmp_store: Store, tmp_path: Path) -> None:
    video = tmp_path / "call.mkv"
    video.write_bytes(b"fake")
    mid = tmp_store.save_meeting(_meeting(source=str(video)), Path("/tmp/a.md"))
    rows = tmp_store.list_meeting_rows()
    assert any(r.id == mid and r.media_ok for r in rows)

    video.unlink()
    rows2 = tmp_store.list_meeting_rows()
    row = next(r for r in rows2 if r.id == mid)
    assert row.media_ok is False


def test_update_title(tmp_store: Store) -> None:
    mid = tmp_store.save_meeting(_meeting(title="Old"), Path("/tmp/a.md"))
    assert tmp_store.update_title(mid, "New title")
    got = tmp_store.get_meeting(mid)
    assert got is not None
    assert got.title == "New title"


def test_adopt_media_and_delete(tmp_store: Store, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    origin = tmp_path / "obs.mkv"
    origin.write_bytes(b"video-bytes")
    mid = tmp_store.save_meeting(_meeting(source=str(origin)), Path("/tmp/a.md"))

    dest = tmp_store.adopt_media(mid, data_dir, origin)
    assert dest.is_file()
    assert dest.read_bytes() == b"video-bytes"
    got = tmp_store.get_meeting(mid)
    assert got is not None
    assert got.media_managed is True  # type: ignore[attr-defined]
    assert got.source == str(dest)
    assert origin.is_file()  # origem preservada

    assert tmp_store.delete_meeting(mid, data_dir=data_dir)
    assert tmp_store.get_meeting(mid) is None
    assert not (data_dir / "media" / str(mid)).exists()


def test_import_original_uses_reflink_and_preserves_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mkv"
    payload = b"reflink-data" * 128
    source.write_bytes(payload)
    source.chmod(0o640)
    os.utime(source, (1_700_000_000, 1_700_000_000))
    progress: list[float] = []

    def clone(dst_fd: int, request: int, src_fd: int) -> None:
        assert request == media._FICLONE
        os.write(dst_fd, os.pread(src_fd, len(payload), 0))

    monkeypatch.setattr(media.fcntl, "ioctl", clone)

    dest = media.import_original(tmp_path / "data", 7, source, progress.append)

    assert dest.read_bytes() == payload
    assert source.read_bytes() == payload
    assert dest.stat().st_mode & 0o777 == source.stat().st_mode & 0o777
    assert dest.stat().st_mtime_ns == source.stat().st_mtime_ns
    assert progress == [1.0]


def test_import_original_cleans_partial_reflink_before_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mkv"
    payload = b"fallback-data" * 100_000
    source.write_bytes(payload)
    removed: list[Path] = []
    unlink = Path.unlink
    progress: list[float] = []

    def unsupported(dst_fd: int, request: int, src_fd: int) -> None:
        os.write(dst_fd, b"partial-clone")
        raise OSError(95, "Operation not supported")

    def track_unlink(path: Path, *, missing_ok: bool = False) -> None:
        removed.append(path)
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(media.fcntl, "ioctl", unsupported)
    monkeypatch.setattr(Path, "unlink", track_unlink)

    dest = media.import_original(tmp_path / "data", 8, source, progress.append)

    assert removed == [dest]
    assert dest.read_bytes() == payload
    assert source.read_bytes() == payload
    assert progress[-1] == 1.0


def test_import_original_source_is_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    source = data_dir / "media" / "9" / "original.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"already-managed")
    progress: list[float] = []

    def unexpected_ioctl(*args: object) -> None:
        pytest.fail("reflink must not run when source is destination")

    monkeypatch.setattr(media.fcntl, "ioctl", unexpected_ioctl)

    assert media.import_original(data_dir, 9, source, progress.append) == source
    assert source.read_bytes() == b"already-managed"
    assert progress == [1.0]


def test_delete_missing_returns_false(tmp_store: Store, tmp_path: Path) -> None:
    assert tmp_store.delete_meeting(99999, data_dir=tmp_path) is False


def test_delete_meetings_bulk(tmp_store: Store, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    a = tmp_store.save_meeting(_meeting(title="A"), Path("/tmp/a.md"))
    b = tmp_store.save_meeting(_meeting(title="B"), Path("/tmp/b.md"))
    c = tmp_store.save_meeting(_meeting(title="C"), Path("/tmp/c.md"))
    n = tmp_store.delete_meetings([a, b, 99999], data_dir=data_dir)
    assert n == 2
    assert tmp_store.get_meeting(a) is None
    assert tmp_store.get_meeting(b) is None
    assert tmp_store.get_meeting(c) is not None
