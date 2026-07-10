"""Banco de vozes: resolução por similaridade de cosseno e enrollment incremental."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .store import Store  # type: ignore[import]


def _to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _unit(v: np.ndarray) -> np.ndarray:
    """Normaliza para vetor unitário; retorna original se norma for zero."""
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return v
    return v / n


def resolve_with_scores(
    embeddings: dict[str, np.ndarray],
    store: "Store",
    threshold: float,
) -> dict[str, tuple[str, float]]:
    """Mapeia cada label para (nome_ou_label, best_sim).

    Para cada label, calcula similaridade de cosseno contra todas as vozes
    do banco. Retorna (nome, sim) se sim >= threshold, senão (label, sim).
    """
    known: dict[str, np.ndarray] = {
        name: _from_blob(blob) for name, blob in store.all_voices().items()
    }

    result: dict[str, tuple[str, float]] = {}
    for label, emb in embeddings.items():
        unit_emb = _unit(np.asarray(emb, dtype=np.float32))
        best_name: str | None = None
        best_sim = -2.0  # cosseno ∈ [-1, 1]

        for name, known_emb in known.items():
            sim = float(np.dot(unit_emb, _unit(known_emb)))
            if sim > best_sim:
                best_sim = sim
                best_name = name

        if best_name is not None and best_sim >= threshold:
            result[label] = (best_name, best_sim)
        else:
            result[label] = (label, best_sim if best_name is not None else 0.0)

    return result


def resolve(
    embeddings: dict[str, np.ndarray],
    store: "Store",
    threshold: float,
) -> dict[str, str]:
    """Mapeia cada label de falante para nome conhecido ou mantém o label.

    Wrapper em torno de resolve_with_scores que descarta os scores.
    """
    return {label: name for label, (name, _) in resolve_with_scores(embeddings, store, threshold).items()}


def enroll(name: str, embedding: np.ndarray, store: "Store") -> None:
    """Registra ou atualiza voz no banco com média incremental simples."""
    new_emb = np.asarray(embedding, dtype=np.float32)
    existing_blob = store.all_voices().get(name)
    if existing_blob is not None:
        old_emb = _from_blob(existing_blob)
        new_emb = (old_emb + new_emb) / 2.0
    store.upsert_voice(name, _to_blob(new_emb))
