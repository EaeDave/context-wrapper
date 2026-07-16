"""Focal tests: WhisperModel lifecycle — load/release separados do WAV.

Contratos verificados:
  A. transcribe_wav usa modelo existente e preserva params exatos.
  B. transcribe() garante release em sucesso e em erro.
  C. Multi-track no pipeline: load_model 1x, release_model 1x antes do diarize.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from meet.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings()


class _FakeSeg:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = f" {text}"
        self.words = []


def _make_model_mock(duration: float = 10.0) -> MagicMock:
    info = MagicMock()
    info.duration = duration
    m = MagicMock()
    m.transcribe.return_value = ([_FakeSeg(0.0, duration, "ok")], info)
    return m


def _make_tracks(mic: bool = True) -> MagicMock:
    t = MagicMock()
    t.mic = Path("mic.wav") if mic else None
    t.others = Path("others.wav")
    t.mixed = Path("mixed.wav")
    t.duration = 30.0
    return t


# ---------------------------------------------------------------------------
# A. transcribe_wav — usa modelo existente, parâmetros idênticos
# ---------------------------------------------------------------------------


def test_transcribe_wav_returns_segments() -> None:
    """transcribe_wav converte segmentos do modelo em TranscriptSegment."""
    from meet.transcribe import transcribe_wav

    model = _make_model_mock()
    segs = transcribe_wav(model, Path("x.wav"), _settings())

    assert len(segs) == 1
    assert segs[0].text == "ok"
    assert segs[0].speaker is None


def test_transcribe_wav_passes_exact_params() -> None:
    """transcribe_wav repassa language, vad_filter e word_timestamps sem alteração."""
    from meet.transcribe import transcribe_wav

    s = _settings()
    model = _make_model_mock()
    transcribe_wav(model, Path("x.wav"), s)

    model.transcribe.assert_called_once_with(
        "x.wav",
        language=s.language,
        vad_filter=True,
        word_timestamps=True,
    )


def test_transcribe_wav_repassa_hotwords_quando_aprendidas() -> None:
    """Vocabulário aprendido é detalhe automático, não configuração do usuário."""
    from meet.transcribe import transcribe_wav

    s = _settings()
    model = _make_model_mock()
    transcribe_wav(model, Path("x.wav"), s, hotwords=["DUN14", "USRMoveColetor"])

    model.transcribe.assert_called_once_with(
        "x.wav",
        language=s.language,
        vad_filter=True,
        word_timestamps=True,
        hotwords="DUN14, USRMoveColetor",
    )


def test_transcribe_wav_fires_progress_callback() -> None:
    """on_progress é chamado com (fraction, seconds) para cada segmento e ao fim."""
    from meet.transcribe import transcribe_wav

    model = _make_model_mock(duration=10.0)
    calls: list[tuple[float, float]] = []
    transcribe_wav(model, Path("x.wav"), _settings(), on_progress=lambda f, s: calls.append((f, s)))

    # last progress call must be (1.0, duration)
    assert calls[-1] == (1.0, 10.0)
    assert all(0.0 <= f <= 1.0 for f, _ in calls)


# ---------------------------------------------------------------------------
# B. transcribe() — load → transcribe_wav → release (em sucesso e em erro)
# ---------------------------------------------------------------------------


def test_release_model_descarrega_runtime() -> None:
    """release_model descarrega os pesos CTranslate2 antes de limpar caches."""
    from meet import transcribe as mod

    fake = MagicMock()
    with (
        patch.object(mod.gc, "collect") as collect,
        patch.dict("sys.modules", {"torch": MagicMock()}),
    ):
        mod.release_model(fake)

    fake.model.unload_model.assert_called_once_with()
    collect.assert_called_once_with()


def test_transcribe_releases_on_success() -> None:
    """transcribe() chama release_model mesmo quando tudo corre bem."""
    from meet import transcribe as mod

    fake = _make_model_mock()
    released: list[object] = []

    with (
        patch.object(mod, "load_model", return_value=fake),
        patch.object(mod, "release_model", side_effect=lambda m: released.append(m)),
    ):
        segs = mod.transcribe(Path("x.wav"), _settings())

    assert len(segs) == 1
    assert len(released) == 1
    assert released[0] is fake


def test_transcribe_releases_on_error() -> None:
    """transcribe() chama release_model mesmo quando transcribe_wav levanta."""
    from meet import transcribe as mod

    fake = _make_model_mock()
    released: list[object] = []

    with (
        patch.object(mod, "load_model", return_value=fake),
        patch.object(mod, "transcribe_wav", side_effect=RuntimeError("GPU crash")),
        patch.object(mod, "release_model", side_effect=lambda m: released.append(m)),
    ):
        with pytest.raises(RuntimeError, match="GPU crash"):
            mod.transcribe(Path("x.wav"), _settings())

    assert len(released) == 1
    assert released[0] is fake


# ---------------------------------------------------------------------------
# C. Pipeline multi-track — um modelo, release antes do diarize
# ---------------------------------------------------------------------------


def test_multitrack_loads_model_once_and_releases() -> None:
    """Multi-track: load_model chamado 1x, release_model 1x antes do diarize."""
    from meet.pipeline import _analyse

    s = _settings()
    fake_model = MagicMock()
    tracks = _make_tracks(mic=True)
    call_order: list[str] = []

    with (
        patch("meet.audio.prepare", return_value=tracks),
        patch("meet.transcribe.load_model", return_value=fake_model) as mock_load,
        patch("meet.transcribe.transcribe_wav", return_value=[]) as mock_twav,
        patch(
            "meet.transcribe.release_model",
            side_effect=lambda m: call_order.append("release"),
        ) as mock_release,
        patch(
            "meet.diarize.diarize",
            side_effect=lambda *a, **kw: call_order.append("diarize") or ([], {}),
        ),
        patch("meet.merge.assign_speakers", return_value=[]),
        patch("meet.merge.combine", return_value=[]),
        patch("meet.merge.rename_speakers", return_value=[]),
        patch("meet.voicebank.resolve_with_scores", return_value={}),
    ):
        _analyse(
            video=Path("vid.mkv"),
            mic_track=1,
            others_track=2,
            no_llm=True,
            settings=s,
            store=MagicMock(),
            workdir=Path("/tmp"),
            today="2026-01-01",
            tracker=MagicMock(),
        )

    mock_load.assert_called_once_with(s)
    mock_release.assert_called_once_with(fake_model)
    # transcribe_wav chamado para mic e depois para others (mesma instância)
    assert mock_twav.call_count == 2
    assert mock_twav.call_args_list[0][0][0] is fake_model
    assert mock_twav.call_args_list[1][0][0] is fake_model
    # release antes do diarize
    assert call_order.index("release") < call_order.index("diarize")


def test_multitrack_releases_on_transcription_error() -> None:
    """Multi-track: release_model é chamado mesmo quando transcrição falha."""
    from meet.pipeline import _analyse

    s = _settings()
    fake_model = MagicMock()
    tracks = _make_tracks(mic=True)
    released: list[object] = []

    with (
        patch("meet.audio.prepare", return_value=tracks),
        patch("meet.transcribe.load_model", return_value=fake_model),
        patch("meet.transcribe.transcribe_wav", side_effect=RuntimeError("GPU crash")),
        patch(
            "meet.transcribe.release_model",
            side_effect=lambda m: released.append(m),
        ),
        patch("meet.diarize.diarize", return_value=([], {})),
        patch("meet.merge.assign_speakers", return_value=[]),
        patch("meet.merge.combine", return_value=[]),
        patch("meet.merge.rename_speakers", return_value=[]),
        patch("meet.voicebank.resolve_with_scores", return_value={}),
    ):
        with pytest.raises(RuntimeError):
            _analyse(
                video=Path("vid.mkv"),
                mic_track=1,
                others_track=2,
                no_llm=True,
                settings=s,
                store=MagicMock(),
                workdir=Path("/tmp"),
                today="2026-01-01",
                tracker=MagicMock(),
            )

    assert len(released) == 1
    assert released[0] is fake_model
