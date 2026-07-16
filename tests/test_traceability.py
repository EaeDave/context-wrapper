"""traceability.validate_evidence — caminho real usado por store revalidation."""

from __future__ import annotations

from meet.models import TranscriptSegment
from meet.traceability import validate_evidence


def test_validate_evidence_hit() -> None:
    segs = [
        TranscriptSegment(start=10.0, end=20.0, text="Alice vai fazer o deploy", speaker="A"),
    ]
    assert validate_evidence(segs, 10.0, 20.0, "Alice vai fazer o deploy") is True


def test_validate_evidence_miss() -> None:
    segs = [
        TranscriptSegment(start=10.0, end=20.0, text="Alice vai fazer o deploy", speaker="A"),
    ]
    assert validate_evidence(segs, 10.0, 20.0, "Bob vai fazer o build") is False
