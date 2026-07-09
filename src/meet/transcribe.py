"""Transcrição de áudio com faster-whisper; modelo carregado e liberado por chamada."""

from __future__ import annotations

import gc
from pathlib import Path

from .config import Settings
from .models import TranscriptSegment


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


def transcribe(wav: Path, settings: Settings) -> list[TranscriptSegment]:
    """Transcreve wav usando faster-whisper.

    Cria e destrói o modelo dentro da chamada para liberar VRAM antes da diarização.
    Fallback automático para CPU/int8 se a inicialização CUDA falhar.
    """
    from faster_whisper import WhisperModel  # import pesado: lazy
    _preload_cuda12_libs()
    from rich.console import Console

    console = Console(stderr=True)

    try:
        model = WhisperModel(
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
        model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",
        )

    try:
        segments_iter, _info = model.transcribe(
            str(wav),
            language=settings.language,
            vad_filter=True,
        )
        result = [
            TranscriptSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
                speaker=None,
            )
            for seg in segments_iter
        ]
    finally:
        del model
        gc.collect()
        try:
            import torch  # noqa: PLC0415 — import pesado: lazy
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    return result
