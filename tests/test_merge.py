"""Tests for meet.merge — speaker assignment, combination, and renaming.

Contracts defended:
- assign_speakers picks the turn with the LARGEST temporal overlap.
- Tie (zero overlap everywhere) → nearest turn by interval centre.
- Empty turns list → speaker remains None; the original segment objects are
  not modified.
- combine marks every segment from `mine` with ME and returns a list sorted
  strictly by start time.
- rename_speakers applies the mapping dict and leaves unknown / None speakers
  untouched.
"""

from __future__ import annotations

import pytest

from meet.merge import assign_speakers, combine, rename_speakers
from meet.models import ME, SpeakerTurn, TranscriptSegment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def seg(start: float, end: float, text: str = "x", speaker: str | None = None) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text, speaker=speaker)


def turn(start: float, end: float, label: str) -> SpeakerTurn:
    return SpeakerTurn(start=start, end=end, label=label)


# ---------------------------------------------------------------------------
# assign_speakers
# ---------------------------------------------------------------------------

def test_assign_max_overlap_wins() -> None:
    """Turn with larger overlap should beat a turn with smaller overlap."""
    # Segment [1, 4]:  overlap with A = min(4,2)−max(1,0) = 1
    #                  overlap with B = min(4,5)−max(1,2) = 2  → B wins
    turns = [turn(0, 2, "A"), turn(2, 5, "B")]
    result = assign_speakers([seg(1, 4)], turns)
    assert result[0].speaker == "B"


def test_assign_overlap_versus_no_overlap_wins() -> None:
    """Any positive overlap beats zero overlap to another turn."""
    # Segment [0, 1]:  overlaps A by 1.0, B is non-overlapping
    turns = [turn(0, 1, "A"), turn(10, 12, "B")]
    result = assign_speakers([seg(0, 1)], turns)
    assert result[0].speaker == "A"


def test_assign_zero_overlap_nearest_centre() -> None:
    """When no turn overlaps the segment, pick the turn whose centre is closest."""
    # Segment [5, 6] → centre 5.5
    # Turn A [0, 1]  → centre 0.5, distance = 5.0
    # Turn B [3, 4]  → centre 3.5, distance = 2.0  → B is nearest
    turns = [turn(0, 1, "A"), turn(3, 4, "B")]
    result = assign_speakers([seg(5, 6)], turns)
    assert result[0].speaker == "B"


def test_assign_single_turn_always_assigned() -> None:
    """With only one turn, every segment gets that turn's label."""
    turns = [turn(10, 20, "SPEAKER_00")]
    segs = [seg(0, 1), seg(100, 101)]  # neither overlaps the turn
    result = assign_speakers(segs, turns)
    assert all(s.speaker == "SPEAKER_00" for s in result)


def test_assign_empty_turns_returns_none_speakers() -> None:
    """Empty turns → speakers remain None (contract: no mutation)."""
    segs = [seg(0, 1), seg(2, 3)]
    result = assign_speakers(segs, [])
    assert all(s.speaker is None for s in result)


def test_assign_does_not_mutate_input_segments() -> None:
    """Original TranscriptSegment objects must not be modified."""
    original = seg(0, 5, "hello")
    turns_list = [turn(0, 5, "Alice")]
    assign_speakers([original], turns_list)
    # The object we passed in must be unchanged
    assert original.speaker is None


def test_assign_does_not_mutate_input_with_empty_turns() -> None:
    """Even the empty-turns fast path must not modify inputs."""
    original = seg(0, 5, "hi", speaker=None)
    assign_speakers([original], [])
    assert original.speaker is None


def test_assign_result_length_matches_input() -> None:
    """Output list must have the same number of segments as input."""
    segs = [seg(i, i + 1) for i in range(5)]
    turns_list = [turn(0, 3, "A"), turn(3, 6, "B")]
    result = assign_speakers(segs, turns_list)
    assert len(result) == len(segs)


# ---------------------------------------------------------------------------
# combine
# ---------------------------------------------------------------------------

def test_combine_tags_mine_with_me() -> None:
    """All segments from `mine` get speaker == ME."""
    mine = [seg(0, 1, "mine1"), seg(2, 3, "mine2")]
    result = combine(mine, [])
    assert all(s.speaker == ME for s in result if s.text in {"mine1", "mine2"})


def test_combine_sorted_by_start() -> None:
    """Output is strictly sorted by start regardless of interleaving order."""
    mine = [seg(3, 4, "m")]        # sits in the middle
    others = [seg(1, 2, "o1"), seg(5, 6, "o2")]
    result = combine(mine, others)
    starts = [s.start for s in result]
    assert starts == sorted(starts)


def test_combine_does_not_alter_others_speaker() -> None:
    """Segments from `others` keep whatever speaker they already had."""
    others = [seg(0, 1, "t", speaker="Alice")]
    result = combine([], others)
    assert result[0].speaker == "Alice"


def test_combine_empty_mine_returns_sorted_others() -> None:
    """No mine → others returned sorted, untouched."""
    others = [seg(5, 6, "b"), seg(1, 2, "a")]
    result = combine([], others)
    assert [s.start for s in result] == [1, 5]


def test_combine_does_not_mutate_mine() -> None:
    """Original mine segments keep their original speaker value."""
    s = seg(0, 1, "x", speaker=None)
    combine([s], [])
    assert s.speaker is None  # untouched


# ---------------------------------------------------------------------------
# rename_speakers
# ---------------------------------------------------------------------------

def test_rename_applies_mapping() -> None:
    segs = [seg(0, 1, speaker="SPEAKER_00"), seg(1, 2, speaker="SPEAKER_01")]
    mapping = {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}
    result = rename_speakers(segs, mapping)
    assert result[0].speaker == "Alice"
    assert result[1].speaker == "Bob"


def test_rename_skips_unknown_labels() -> None:
    """Labels absent from the mapping are left unchanged."""
    segs = [seg(0, 1, speaker="SPEAKER_00"), seg(1, 2, speaker="SPEAKER_99")]
    mapping = {"SPEAKER_00": "Alice"}
    result = rename_speakers(segs, mapping)
    assert result[0].speaker == "Alice"
    assert result[1].speaker == "SPEAKER_99"


def test_rename_skips_none_speaker() -> None:
    """speaker=None must not trigger a KeyError or be renamed."""
    segs = [seg(0, 1, speaker=None)]
    result = rename_speakers(segs, {"None": "oops"})
    assert result[0].speaker is None


def test_rename_does_not_mutate_inputs() -> None:
    """Original segment objects must not be modified."""
    original = seg(0, 1, speaker="OLD")
    rename_speakers([original], {"OLD": "NEW"})
    assert original.speaker == "OLD"


def test_rename_empty_mapping_passthrough() -> None:
    """Empty mapping → all speakers unchanged."""
    segs = [seg(0, 1, speaker="X"), seg(1, 2, speaker=None)]
    result = rename_speakers(segs, {})
    assert result[0].speaker == "X"
    assert result[1].speaker is None
