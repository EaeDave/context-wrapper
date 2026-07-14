"""Tests for meet.audio.prepare — ffmpeg-backed audio extraction.

Contracts defended:
- Single audio stream → mic=None, others is mixed, both point at the same file.
- Two+ audio streams → one ffmpeg extraction emits distinct mic/others WAVs;
  mixed aliases others and no redundant third WAV is created.
- Outputs are 16 kHz mono pcm_s16le and duration is positive.

All tests require ffmpeg; they are skipped when it is absent.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

import meet.audio as audio
from meet.audio import (
    PREVIEW_FULL,
    PREVIEW_WEB,
    ensure_listen_mix,
    ensure_listen_preview,
    export_listen_mix,
    export_listen_preview,
    listen_mix_path,
    listen_preview_path,
    prepare,
    probe_video_size,
    probe_video_streams,
)

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


def _wav_info(path: Path) -> tuple[int, int, int]:
    """Return (channels, framerate, sample width) of a WAV file."""
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getframerate(), w.getsampwidth()


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
    channels, rate, sample_width = _wav_info(tracks.others)
    assert (channels, rate, sample_width) == (1, 16000, 2)


def test_prepare_single_track_duration_positive(tmp_path: Path) -> None:
    src = tmp_path / "source.wav"
    _make_sine_wav(src, duration=2.0)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.duration > 0.0



def test_prepare_reporta_progresso_monotono(tmp_path: Path) -> None:
    src = tmp_path / "source.wav"
    _make_sine_wav(src, duration=2.0)
    updates: list[float] = []

    prepare(src, tmp_path / "work", on_progress=updates.append)

    assert updates
    assert updates == sorted(updates)
    assert updates[-1] == 1.0

# ---------------------------------------------------------------------------
# Two-track case
# ---------------------------------------------------------------------------

def test_prepare_two_tracks_mic_is_not_none(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    assert tracks.mic is not None


def test_prepare_two_tracks_emits_only_mic_and_others(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)
    workdir = tmp_path / "work"

    tracks = prepare(src, workdir)

    assert tracks.mic != tracks.others
    assert tracks.mixed == tracks.others
    assert {path.name for path in workdir.iterdir()} == {"mic.wav", "others.wav"}


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
    channels, rate, sample_width = _wav_info(tracks.mic)
    assert (channels, rate, sample_width) == (1, 16000, 2)


def test_prepare_two_tracks_others_is_16k_mono(tmp_path: Path) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src)

    tracks = prepare(src, tmp_path / "work")
    channels, rate, sample_width = _wav_info(tracks.others)
    assert (channels, rate, sample_width) == (1, 16000, 2)


def test_prepare_two_tracks_runs_one_ffmpeg_with_monotonic_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "meeting.mkv"
    _make_two_stream_mkv(src, duration=2.0)
    updates: list[float] = []
    calls = 0
    original_run_ffmpeg = audio._run_ffmpeg

    def counting_run_ffmpeg(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        original_run_ffmpeg(*args, **kwargs)

    monkeypatch.setattr(audio, "_run_ffmpeg", counting_run_ffmpeg)
    prepare(src, tmp_path / "work", on_progress=updates.append)

    assert calls == 1
    assert updates
    assert updates == sorted(updates)
    assert updates[-1] == 1.0


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


def _make_video_two_audio_mkv(path: Path, duration: float = 1.0) -> None:
    """MKV com 1 vídeo H.264 + 2 áudios (simula OBS dual-track)."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={duration}:r=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
            "-map", "0:v",
            "-map", "1:a",
            "-map", "2:a",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_export_listen_preview_has_video_and_one_audio(tmp_path: Path) -> None:
    src = tmp_path / "obs.mkv"
    _make_video_two_audio_mkv(src)
    assert probe_video_streams(src) == 1

    out = export_listen_preview(src, quality=PREVIEW_WEB)
    assert out == tmp_path / "obs.listen.mp4"
    assert out.exists()
    assert probe_video_streams(out) == 1
    from meet.audio import probe_audio_streams

    assert probe_audio_streams(out) == 1
    # Browser-safe: Main profile (not High@L5.1 copy)
    import json
    import subprocess

    info = json.loads(
        subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0", str(out),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    v = info["streams"][0]
    assert v["codec_name"] == "h264"
    assert v.get("profile") in ("Main", "Constrained Baseline", "Baseline", "High")
    assert int(v.get("width", 9999)) <= 1280


def test_export_listen_preview_full_keeps_resolution(tmp_path: Path) -> None:
    src = tmp_path / "obs.mkv"
    _make_video_two_audio_mkv(src)  # 320x240 test fixture
    sw, sh = probe_video_size(src)
    out = export_listen_preview(src, quality=PREVIEW_FULL, max_width=0)
    assert out == tmp_path / "obs.listen.full.mp4"
    ow, oh = probe_video_size(out)
    assert ow == sw and oh == sh


def test_ensure_listen_preview_caches(tmp_path: Path) -> None:
    src = tmp_path / "obs.mkv"
    _make_video_two_audio_mkv(src)
    first = ensure_listen_preview(src, quality=PREVIEW_WEB)
    mtime = first.stat().st_mtime
    second = ensure_listen_preview(src, quality=PREVIEW_WEB)
    assert second == first
    assert second.stat().st_mtime == mtime


def test_ensure_listen_preview_audio_only_falls_back(tmp_path: Path) -> None:
    src = tmp_path / "audio-only.mkv"
    _make_two_stream_mkv(src)
    out = ensure_listen_preview(src)
    assert out.suffix == ".m4a"
    assert out.exists()


def test_listen_preview_path_naming(tmp_path: Path) -> None:
    assert listen_preview_path(tmp_path / "x.mkv") == tmp_path / "x.listen.mp4"
    assert (
        listen_preview_path(tmp_path / "x.mkv", PREVIEW_FULL)
        == tmp_path / "x.listen.full.mp4"
    )
