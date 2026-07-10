"""Diarização de falantes via pyannote (community-1, pyannote.audio 4.x)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .config import Settings
from .models import SpeakerTurn

if TYPE_CHECKING:
    import numpy as np

CHECKPOINT = "pyannote/speaker-diarization-community-1"


def diarize(
    wav: Path,
    settings: Settings,
    num_speakers: int = 0,
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

    if num_speakers > 0:
        output = pipe(str(wav), num_speakers=num_speakers)
    else:
        output = pipe(str(wav))

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
