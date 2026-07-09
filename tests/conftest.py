"""Shared fixtures for the context-wrapper test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from meet.store import Store


@pytest.fixture()
def tmp_store(tmp_path: Path) -> Store:
    """A real Store backed by an in-process SQLite temp file."""
    return Store(tmp_path / "test.db")
