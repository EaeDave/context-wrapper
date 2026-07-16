"""Transcrição de áudio com faster-whisper; criação/release separados do consumo do WAV."""

from __future__ import annotations

import gc
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Settings
from .models import TranscriptSegment, Word

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

TranscribeProgress = Callable[[float, float], None]


def _preload_cuda12_libs() -> None:
    """ctranslate2 dlopen'a libcublas.so.12/libcudnn.so.9 em runtime.

    O torch cu13 não as fornece; carregamos as wheels nvidia-*-cu12 com
    RTLD_GLOBAL para que o dlopen posterior as encontre já residentes.
    """
    import ctypes
    import sysconfig

    site = Path(sysconfig.get_paths()["purelib"])
    patterns = (
        "nvidia/cublas/lib/libcublas.so.12",
        "nvidia/cublas/lib/libcublasLt.so.12",
        "nvidia/cudnn/lib/libcudnn.so.9",
    )
    for pattern in patterns:
        for lib in sorted(site.glob(pattern)):
            try:
                ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass  # sem a lib, o fallback CPU do init cobre


def load_model(settings: Settings) -> WhisperModel:
    """Cria WhisperModel com fallback automático CUDA→CPU/int8.

    O caller é responsável por chamar ``release_model`` quando terminar.
    Permite uma única instância para transcrições sequenciais (multi-track).
    """
    from faster_whisper import WhisperModel as _WhisperModel  # import pesado: lazy
    from rich.console import Console

    _preload_cuda12_libs()
    console = Console(stderr=True)

    try:
        return _WhisperModel(
            settings.whisper_model,
            device=settings.device,
            compute_type=settings.compute_type,
        )
    except RuntimeError:
        console.print(
            f"[yellow]Aviso: falha ao inicializar "
            f"{settings.device}/{settings.compute_type}. "
            f"Usando CPU/int8.[/yellow]"
        )
        return _WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",
        )


def release_model(model: WhisperModel) -> None:
    """Descarrega pesos do runtime e libera caches antes da diarização."""
    try:
        model.model.unload_model()
    except Exception:
        pass
    gc.collect()
    try:
        import torch  # noqa: PLC0415 — import pesado: lazy

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def transcribe_wav(
    model: WhisperModel,
    wav: Path,
    settings: Settings,
    on_progress: TranscribeProgress | None = None,
    hotwords: list[str] | None = None,
) -> list[TranscriptSegment]:
    """Transcreve wav usando modelo existente; caller gerencia o lifecycle do modelo.

    Reporta (fração, segundo alcançado) via on_progress ao consumir segmentos.
    Parâmetros idênticos aos da chamada legada: language, vad_filter, word_timestamps.
    """
    transcribe_kwargs = {
        "language": settings.language,
        "vad_filter": True,
        "word_timestamps": True,
    }
    if hotwords:
        transcribe_kwargs["hotwords"] = ", ".join(hotwords)
    segments_iter, info = model.transcribe(str(wav), **transcribe_kwargs)
    duration = max(float(info.duration), 0.001)
    result: list[TranscriptSegment] = []
    for seg in segments_iter:
        result.append(
            TranscriptSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
                speaker=None,
                words=[Word(w.start, w.end, w.word) for w in (seg.words or [])] or None,
            )
        )
        if on_progress is not None:
            on_progress(min(float(seg.end) / duration, 1.0), float(seg.end))
    if on_progress is not None:
        on_progress(1.0, duration)
    return result


def transcribe(
    wav: Path,
    settings: Settings,
    on_progress: TranscribeProgress | None = None,
    hotwords: list[str] | None = None,
) -> list[TranscriptSegment]:
    """Cria modelo, transcreve e libera VRAM num único call.

    Atalho para single-track e reprocess; para multi-track use
    ``load_model`` / ``transcribe_wav`` / ``release_model`` diretamente.
    Fallback automático para CPU/int8 se a inicialização CUDA falhar.
    """
    model = load_model(settings)
    try:
        return transcribe_wav(model, wav, settings, on_progress, hotwords)
    finally:
        release_model(model)
