"""Timeouts em ffmpeg/ffprobe não devem deixar processo órfão eterno."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import meet.audio as audio


def test_ffprobe_timeout_raises_clear_error() -> None:
    with patch("meet.audio.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)):
        with pytest.raises(RuntimeError, match="ffprobe timeout"):
            audio.probe_audio_streams(Path("/tmp/x.mkv"))


def test_run_ffmpeg_timeout_kills_process() -> None:
    """Simula stdout que nunca fecha; deadline curto mata o proc."""

    class FakeStdout:
        def __iter__(self):
            while True:
                yield "out_time_us=0\n"

    proc = MagicMock()
    proc.stdout = FakeStdout()
    proc.poll.return_value = None
    proc.wait.return_value = -9

    with (
        patch("meet.audio.subprocess.Popen", return_value=proc),
        patch("meet.audio.tempfile.TemporaryFile") as tf,
    ):
        tf.return_value.__enter__.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="ffmpeg timeout"):
            audio._run_ffmpeg(["-i", "x", "y"], duration=1.0, timeout=0.05)
    proc.kill.assert_called()
