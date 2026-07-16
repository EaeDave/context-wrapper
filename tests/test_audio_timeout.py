"""Timeouts em ffmpeg/ffprobe não devem deixar processo órfão eterno."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import meet.audio as audio


def test_ffprobe_timeout_raises_clear_error() -> None:
    with patch(
        "meet.audio.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=30),
    ):
        with pytest.raises(RuntimeError, match="ffprobe timeout"):
            audio.probe_audio_streams(Path("/tmp/x.mkv"))


def _fake_popen_with_pipe(
    *,
    pipe_r: int,
    poll_side_effect=None,
    wait_return: int = -9,
) -> MagicMock:
    proc = MagicMock()
    stdout = MagicMock()
    stdout.fileno.return_value = pipe_r
    proc.stdout = stdout
    if poll_side_effect is None:
        proc.poll.return_value = None
    else:
        proc.poll.side_effect = poll_side_effect
    proc.wait.return_value = wait_return
    return proc


def test_run_ffmpeg_timeout_kills_when_progress_stalls_silent() -> None:
    """ffmpeg mudo (sem linhas de progress) deve estourar o deadline via select.

    O bug antigo: ``for line in stdout`` bloqueava e o check de deadline
    nunca rodava se o processo não emitisse progresso.
    """
    pipe_r, pipe_w = os.pipe()
    # Não escreve nada em pipe_w → select só retorna por timeout de poll
    proc = _fake_popen_with_pipe(pipe_r=pipe_r)

    with (
        patch("meet.audio.subprocess.Popen", return_value=proc),
        patch("meet.audio.tempfile.TemporaryFile") as tf,
        patch("meet.audio.os.set_blocking"),
    ):
        tf.return_value.__enter__.return_value = MagicMock()
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="ffmpeg timeout"):
            audio._run_ffmpeg(["-i", "x", "y"], duration=1.0, timeout=0.15)
        elapsed = time.monotonic() - t0

    os.close(pipe_r)
    try:
        os.close(pipe_w)
    except OSError:
        pass

    proc.kill.assert_called()
    # Deve respeitar o deadline (~0.15s), não esperar um hang longo
    assert elapsed < 1.0, f"timeout demorou demais: {elapsed:.2f}s"


def test_run_ffmpeg_timeout_kills_when_progress_floods() -> None:
    """Progress contínuo também respeita o deadline."""
    pipe_r, pipe_w = os.pipe()
    os.set_blocking(pipe_w, False)

    def _flood() -> None:
        payload = b"out_time_us=0\n"
        # Enche o pipe o quanto der; select verá dados até o deadline
        try:
            while True:
                os.write(pipe_w, payload * 64)
        except (BlockingIOError, BrokenPipeError, OSError):
            pass

    import threading

    writer = threading.Thread(target=_flood, daemon=True)
    writer.start()

    proc = _fake_popen_with_pipe(pipe_r=pipe_r)

    with (
        patch("meet.audio.subprocess.Popen", return_value=proc),
        patch("meet.audio.tempfile.TemporaryFile") as tf,
    ):
        tf.return_value.__enter__.return_value = MagicMock()
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="ffmpeg timeout"):
            audio._run_ffmpeg(["-i", "x", "y"], duration=1.0, timeout=0.15)
        elapsed = time.monotonic() - t0

    try:
        os.close(pipe_w)
    except OSError:
        pass
    try:
        os.close(pipe_r)
    except OSError:
        pass

    proc.kill.assert_called()
    assert elapsed < 1.0, f"timeout demorou demais: {elapsed:.2f}s"


def test_run_ffmpeg_reports_progress_and_exits_clean() -> None:
    """Progress parseado e returncode 0 → sucesso."""
    pipe_r, pipe_w = os.pipe()
    os.write(pipe_w, b"out_time_us=500000\nprogress=end\n")
    os.close(pipe_w)

    # poll: None enquanto lê, depois 0 (terminou)
    poll_vals = [None, None, 0, 0, 0]
    proc = _fake_popen_with_pipe(
        pipe_r=pipe_r,
        poll_side_effect=lambda: poll_vals.pop(0) if poll_vals else 0,
        wait_return=0,
    )

    seen: list[float] = []
    with (
        patch("meet.audio.subprocess.Popen", return_value=proc),
        patch("meet.audio.tempfile.TemporaryFile") as tf,
    ):
        tf.return_value.__enter__.return_value = MagicMock()
        audio._run_ffmpeg(
            ["-i", "x", "y"],
            duration=1.0,
            timeout=2.0,
            on_progress=seen.append,
        )

    try:
        os.close(pipe_r)
    except OSError:
        pass

    assert any(p == 0.5 for p in seen)
    assert seen[-1] == 1.0
