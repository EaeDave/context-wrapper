"""Tests for meet.voicebank — cosine-based speaker resolution and enrollment.

Contracts defended:
- resolve: cosine ≥ threshold → resolved to known name.
- resolve: cosine < threshold → label kept unchanged.
- resolve: at exact threshold boundary (cosine == threshold) → resolves.
- resolve: empty voice bank → all labels kept.
- resolve: negated embedding (cosine = -1) never matches at threshold ≥ 0.
- enroll: new name → stored as float32 blob.
- enroll: re-enroll → stored as incremental average (old + new) / 2.
- float32 blob roundtrip: np.ndarray ↔ bytes without data loss.
"""

from __future__ import annotations

import numpy as np
import pytest

from meet.voicebank import enroll, resolve


# ---------------------------------------------------------------------------
# Minimal fake Store (matches the two methods voicebank.py uses)
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self) -> None:
        self._voices: dict[str, bytes] = {}

    def all_voices(self) -> dict[str, bytes]:
        return dict(self._voices)

    def upsert_voice(self, name: str, blob: bytes) -> None:
        self._voices[name] = blob


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n != 0.0 else v


def _to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


# ---------------------------------------------------------------------------
# resolve — cosine threshold behaviour
# ---------------------------------------------------------------------------

def test_resolve_above_threshold_returns_known_name() -> None:
    """A vector identical to a known voice (cosine=1.0) resolves to its name."""
    store = FakeStore()
    v = np.array([1.0, 0.0], dtype=np.float32)
    store._voices["Alice"] = _to_blob(v)

    result = resolve({"S00": v}, store, threshold=0.5)
    assert result["S00"] == "Alice"


def test_resolve_below_threshold_keeps_label() -> None:
    """Orthogonal vectors (cosine=0.0) must not resolve when threshold > 0."""
    store = FakeStore()
    v_known = np.array([1.0, 0.0], dtype=np.float32)
    v_query = np.array([0.0, 1.0], dtype=np.float32)  # cosine = 0.0
    store._voices["Alice"] = _to_blob(v_known)

    result = resolve({"S00": v_query}, store, threshold=0.5)
    assert result["S00"] == "S00"  # kept label, not resolved


def test_resolve_at_exact_threshold_resolves() -> None:
    """cosine == threshold satisfies the >= condition; must resolve."""
    # Use identical unit vectors → cosine = 1.0 exactly.
    # Set threshold = 1.0 to exercise the boundary.
    store = FakeStore()
    v = np.array([1.0, 0.0], dtype=np.float32)
    store._voices["Bob"] = _to_blob(v)

    result = resolve({"S00": v}, store, threshold=1.0)
    assert result["S00"] == "Bob"


def test_resolve_empty_bank_keeps_all_labels() -> None:
    """No voices enrolled → every label maps to itself."""
    store = FakeStore()
    v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    result = resolve({"S00": v, "S01": v}, store, threshold=0.0)
    assert result == {"S00": "S00", "S01": "S01"}


def test_resolve_negated_embedding_does_not_match() -> None:
    """A negated vector has cosine = -1.0 against the original; threshold=0 keeps it."""
    store = FakeStore()
    v = np.array([1.0, 0.0], dtype=np.float32)
    store._voices["Alice"] = _to_blob(v)

    v_neg = np.array([-1.0, 0.0], dtype=np.float32)
    # cosine([1,0], [-1,0]) = -1.0 < 0.0 = threshold → must not match
    result = resolve({"S00": v_neg}, store, threshold=0.0)
    assert result["S00"] == "S00"


def test_resolve_best_match_wins_among_multiple_voices() -> None:
    """Among several voices, the one with the highest cosine similarity wins."""
    store = FakeStore()
    # v_a is close to v_query (cosine ≈ 0.98); v_b is distant (cosine ≈ 0.0)
    v_a = np.array([0.98, 0.2], dtype=np.float32)
    v_b = np.array([0.0, 1.0], dtype=np.float32)
    v_query = np.array([1.0, 0.0], dtype=np.float32)

    store._voices["Alice"] = _to_blob(v_a)
    store._voices["Bob"] = _to_blob(v_b)

    result = resolve({"S00": v_query}, store, threshold=0.5)
    assert result["S00"] == "Alice"


# ---------------------------------------------------------------------------
# enroll
# ---------------------------------------------------------------------------

def test_enroll_new_voice_stored_as_float32() -> None:
    """First enrollment persists the embedding as float32 bytes."""
    store = FakeStore()
    v = np.array([1.0, 2.0, 3.0], dtype=np.float64)  # pass float64 deliberately

    enroll("Alice", v, store)

    blob = store._voices["Alice"]
    recovered = _from_blob(blob)
    # Should be stored as float32 regardless of input dtype
    np.testing.assert_array_almost_equal(recovered, np.array([1.0, 2.0, 3.0], dtype=np.float32))


def test_enroll_incremental_average_on_reenroll() -> None:
    """Re-enrolling produces (existing + new) / 2 — incremental mean."""
    store = FakeStore()
    v1 = np.array([2.0, 4.0], dtype=np.float32)
    v2 = np.array([6.0, 0.0], dtype=np.float32)

    enroll("Alice", v1, store)
    enroll("Alice", v2, store)

    expected = (v1 + v2) / 2.0  # [4.0, 2.0]
    recovered = _from_blob(store._voices["Alice"])
    np.testing.assert_array_almost_equal(recovered, expected)


def test_enroll_multiple_names_independent() -> None:
    """Enrolling two different names does not interfere with each other."""
    store = FakeStore()
    va = np.array([1.0, 0.0], dtype=np.float32)
    vb = np.array([0.0, 1.0], dtype=np.float32)

    enroll("Alice", va, store)
    enroll("Bob", vb, store)

    np.testing.assert_array_equal(_from_blob(store._voices["Alice"]), va)
    np.testing.assert_array_equal(_from_blob(store._voices["Bob"]), vb)


# ---------------------------------------------------------------------------
# float32 blob roundtrip
# ---------------------------------------------------------------------------

def test_float32_blob_roundtrip_via_enroll_get() -> None:
    """Arbitrary float32 values survive enroll → all_voices → _from_blob unchanged."""
    store = FakeStore()
    original = np.array([0.1, -0.5, 3.14159, -999.0, 0.0], dtype=np.float32)

    enroll("TestSpeaker", original, store)
    blob = store.all_voices()["TestSpeaker"]
    recovered = _from_blob(blob)

    # Exact bit-level equality — no loss expected for float32→bytes→float32
    np.testing.assert_array_equal(recovered, original)


def test_float32_blob_roundtrip_preserves_dtype() -> None:
    """The recovered array uses float32 regardless of the original dtype."""
    store = FakeStore()
    v_f64 = np.array([1.0, 2.0], dtype=np.float64)
    enroll("X", v_f64, store)
    blob = store.all_voices()["X"]
    recovered = _from_blob(blob)
    assert recovered.dtype == np.float32
