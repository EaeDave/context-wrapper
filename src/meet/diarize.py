"""Diarização de falantes via pyannote (community-1, pyannote.audio 4.x)."""

from __future__ import annotations

import gc
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Settings
from .models import SpeakerTurn

if TYPE_CHECKING:
    import numpy as np

CHECKPOINT = "pyannote/speaker-diarization-community-1"

DiarizeProgress = Callable[[str, str, float | None], None]

_STEP_LABELS = {
    "segmentation": "Analisando atividade de voz",
    "speaker_counting": "Contando falantes",
    "embeddings": "Comparando características vocais",
    "discrete_diarization": "Montando turnos dos falantes",
}


def diarize(
    wav: Path,
    settings: Settings,
    num_speakers: int = 0,
    on_progress: DiarizeProgress | None = None,
) -> tuple[list[SpeakerTurn], dict[str, "np.ndarray"]]:
    """Roda diarização e retorna turns + centroide de embedding por falante.

    Requer hf_token configurado e termos do modelo aceitos em hf.co.
    Fallback automático para CPU se settings.device não estiver disponível.
    """
    if not settings.hf_token:
        raise RuntimeError(
            "hf_token não configurado.\n"
            "1. Crie um token em https://hf.co/settings/tokens\n"
            f"2. Aceite os termos de uso em https://hf.co/{CHECKPOINT}\n"
            "3. Defina HF_TOKEN no ambiente ou em ~/.config/meet/config.toml"
        )

    import numpy as np
    import torch
    from pyannote.audio import Pipeline

    pipe = None
    try:
        pipe = Pipeline.from_pretrained(CHECKPOINT, token=settings.hf_token)
        if pipe is None:
            raise RuntimeError(
                f"Não foi possível carregar {CHECKPOINT}. "
                "Confira o token e a aceitação dos termos em hf.co."
            )

        try:
            pipe.to(torch.device(settings.device))
        except Exception:
            from rich.console import Console

            Console().print(
                f"[yellow]AVISO: não foi possível usar '{settings.device}' para"
                " diarização; usando CPU.[/yellow]"
            )
            pipe.to(torch.device("cpu"))

        def hook(
            step_name: str,
            _artifact,
            *,
            total: int | None = None,
            completed: int | None = None,
            **_kwargs,
        ) -> None:
            if on_progress is None:
                return
            fraction = None
            if total and completed is not None:
                fraction = min(max(completed / total, 0.0), 1.0)
            on_progress(step_name, _STEP_LABELS.get(step_name, step_name), fraction)

        if num_speakers > 0:
            output = pipe(str(wav), num_speakers=num_speakers, hook=hook)
        else:
            output = pipe(str(wav), hook=hook)

        # pyannote 4.x: DiarizeOutput(speaker_diarization, speaker_embeddings, ...).
        # Pipelines legacy (3.x) retornam a Annotation diretamente, sem embeddings.
        annotation = getattr(output, "speaker_diarization", output)
        raw_embeddings = getattr(output, "speaker_embeddings", None)

        turns = [
            SpeakerTurn(start=float(seg.start), end=float(seg.end), label=label)
            for seg, _track, label in annotation.itertracks(yield_label=True)
        ]

        # speaker_embeddings: ndarray (num_speakers, D), linhas na ordem de labels().
        # Falantes sem áudio suficiente podem vir com linha zerada ou NaN — descartar.
        centroids: dict[str, np.ndarray] = {}
        if raw_embeddings is not None:
            labels = annotation.labels()
            for i, label in enumerate(labels):
                if i >= raw_embeddings.shape[0]:
                    break
                row = np.asarray(raw_embeddings[i], dtype=np.float32)
                if np.all(np.isfinite(row)) and np.linalg.norm(row) > 0.0:
                    centroids[label] = row

        return turns, centroids
    finally:
        del pipe
        try:
            gc.collect()
        except Exception:
            pass
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
