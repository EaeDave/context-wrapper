"""Mídia gerida pelo meet: ``~/.local/share/meet/media/{id}/``."""

from __future__ import annotations
from collections.abc import Callable

import fcntl
import shutil
from pathlib import Path


_FICLONE = 0x40049409


def media_dir(data_dir: Path, meeting_id: int) -> Path:
    """Pasta canônica de uma reunião."""
    return data_dir / "media" / str(meeting_id)


def original_path(data_dir: Path, meeting_id: int, suffix: str = ".mkv") -> Path:
    """Path do original importado."""
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    return media_dir(data_dir, meeting_id) / f"original{ext.lower()}"


def import_original(
    data_dir: Path,
    meeting_id: int,
    source: Path,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Copia ``source`` para ``media/{id}/original.ext`` e retorna o destino.

    Não apaga o arquivo de origem (OBS / Videos).
    """
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {source}")

    dest_dir = media_dir(data_dir, meeting_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = original_path(data_dir, meeting_id, source.suffix or ".mkv")

    if source.resolve() != dest.resolve():
        cloned = False
        try:
            with source.open("rb") as src, dest.open("wb") as dst:
                fcntl.ioctl(dst.fileno(), _FICLONE, src.fileno())
            cloned = True
        except OSError:
            dest.unlink(missing_ok=True)

        if not cloned:
            total = max(source.stat().st_size, 1)
            copied = 0
            with source.open("rb") as src, dest.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    copied += len(chunk)
                    if on_progress is not None and copied < total:
                        on_progress(copied / total)
        shutil.copystat(source, dest)

    if on_progress is not None:
        on_progress(1.0)

    # Move previews antigos gerados ao lado do OBS (se existirem)
    for pattern in (
        f"{source.stem}.listen.mp4",
        f"{source.stem}.listen.full.mp4",
        f"{source.stem}.listen.m4a",
    ):
        old = source.with_name(pattern)
        if old.is_file():
            target = dest_dir / old.name
            try:
                shutil.move(str(old), str(target))
            except OSError:
                pass

    return dest


def purge_media(data_dir: Path, meeting_id: int) -> None:
    """Remove a pasta ``media/{id}/`` inteira."""
    d = media_dir(data_dir, meeting_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def media_exists(path: str | Path | None) -> bool:
    """True se o path aponta para um arquivo existente."""
    if not path:
        return False
    return Path(path).expanduser().is_file()
