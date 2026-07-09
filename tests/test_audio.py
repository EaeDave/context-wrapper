"""Tests for meet.audio.prepare — ffmpeg-backed audio extraction.

Contracts defended:
- Single audio stream → mic=None, others is mixed, both point at the same file.
- Two+ audio streams → mic and others are distinct WAV files; mixed is a
  third distinct file (amix output); all three are 16 kHz mono pcm_s16le.
- duration field is populated (positive float).

All tests require ffmpeg; they are skipped when it is absent.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from meet.audio import ensure_listen_mix, export_listen_mix, listen_mix_path, prepare

FFMPEG_MISSING = shutil.which("ffmpeg") is None
pytestmark = pytest.mark.skipif(FFMPEG_MISSING, reason="ffmpeg not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sine_wav(path: Path, freq: int = 440, duration: float = 1.0) -> None:
    """Generate a 16 kHz mono sine-wave WAV via ffmpeg lavfi source."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={duration}",
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_two_stream_mkv(path: Path, duration: float = 1.0) -> None:
    """Generate an MKV with two audio streams (440 Hz and 880 Hz sines)."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
            "-map", "0:a",
            "-map", "1:a",
            "-c:a", "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _wav_info(path: Path) -> tuple[int, int]:
    """Return (nchannels, framerate) of a WAV file."""
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getframerate()


# ---------------------------------------------------------------------------
# Single-track case
# ---------------------------------------------------------------------------

def test_prepare_single_track_mic_is_none(tmp_path: Path) -> None:
    """1-stream WAV → AudioTracks.mic is None."""
    src = tmp_path / "source.wav"
    _make_sine_wav(src)
    workdir = tmp_path / "work"

    tracks = prepare(src, workdir)
    assert tracks.mic is None


def test_prepare_single_track_others_equals_mixed(tmp_path: Path) -> None:
    """1-stream WAV → others and mixed point at the same file."""
    src = tmp_path / "source.wav"
    _make_sine_wav(src)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.others == tracks.mixed


def test_prepare_single_track_output_is_16k_mono(tmp_path: Path) -> None:
    """The emitted WAV (others == mixed) must be 16 kHz mono."""
    src = tmp_path / "source.wav"
    _make_sine_wav(src)

    tracks = prepare(src, tmp_path / "work")
    ch, rate = _wav_info(tracks.others)
    assert ch == 1
    assert rate == 16000


def test_prepare_single_track_duration_positive(tmp_path: Path) -> None:
    src = tmp_path / "source.wav"
    _make_sine_wav(src, duration=2.0)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.duration > 0.0


# ---------------------------------------------------------------------------
# Two-track case
# ---------------------------------------------------------------------------

def test_prepare_two_tracks_mic_is_not_none(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.mic is not None


def test_prepare_two_tracks_three_distinct_files(tmp_path: Path) -> None:
    """mic, others, and mixed are three different file paths."""
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    paths = {tracks.mic, tracks.others, tracks.mixed}
    assert len(paths) == 3


def test_prepare_two_tracks_all_files_exist(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.mic is not None and tracks.mic.exists()
    assert tracks.others.exists()
    assert tracks.mixed.exists()


def test_prepare_two_tracks_mic_is_16k_mono(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.mic is not None
    ch, rate = _wav_info(tracks.mic)
    assert ch == 1
    assert rate == 16000


def test_prepare_two_tracks_others_is_16k_mono(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    ch, rate = _wav_info(tracks.others)
    assert ch == 1
    assert rate == 16000


def test_prepare_two_tracks_mixed_is_16k_mono(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    ch, rate = _wav_info(tracks.mixed)
    assert ch == 1
    assert rate == 16000


def test_prepare_two_tracks_duration_positive(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src, duration=2.0)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.duration > 0.0


# ---------------------------------------------------------------------------
# export_listen_mix — arquivo de consulta humana (mic + desktop)
# ---------------------------------------------------------------------------

def test_export_listen_mix_default_path(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    out = export_listen_mix(src)
    assert out == tmp_path / "meeting.listen.m4a"
    assert out.exists()
    assert out.stat().st_size > 0


def test_export_listen_mix_custom_output(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)
    dest = tmp_path / "ouvir.m4a"

    out = export_listen_mix(src, dest)
    assert out == dest
    assert dest.exists()


def test_export_listen_mix_single_track(tmp_path: Path) -> None:
    src = tmp_path / "solo.wav"
    _make_sine_wav(src)

    out = export_listen_mix(src, tmp_path / "solo.listen.m4a")
    assert out.exists()
    assert out.stat().st_size > 0


def test_listen_mix_path_naming(tmp_path: Path) -> None:
    src = tmp_path / "call.mkv"
    assert listen_mix_path(src) == tmp_path / "call.listen.m4a"


def test_ensure_listen_mix_caches(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    first = ensure_listen_mix(src)
    mtime1 = first.stat().st_mtime
    size1 = first.stat().st_size

    second = ensure_listen_mix(src)
    assert second == first
    assert second.stat().st_mtime == mtime1
    assert second.stat().st_size == size1

    forced = ensure_listen_mix(src, force=True)
    assert forced == first
    assert forced.stat().st_size > 0
