"""Núcleo do pipeline (CLI e web compartilham esta função)."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from .config import Settings
from .models import ActionItem, MeetingResult
from .store import Store

ProgressCb = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def run_pipeline(
    video: Path,
    *,
    settings: Settings,
    store: Store,
    title: str | None = None,
    mic_track: int = 1,
    others_track: int = 2,
    no_llm: bool = False,
    keep_wav: bool = False,
    import_media: bool = True,
    today: str | None = None,
    on_progress: ProgressCb | None = None,
) -> tuple[int, MeetingResult, Path]:
    """Processa gravação de ponta a ponta.

    Returns:
        (meeting_id, result, md_path)

    Se ``import_media`` (default), copia o vídeo para
    ``~/.local/share/meet/media/{id}/original.ext`` e atualiza o SQLite.

    Raises:
        FileNotFoundError, RuntimeError, ValueError — erros de pipeline.
    """
    if not video.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {video}")

    progress = on_progress or _noop
    workdir = Path(tempfile.mkdtemp(prefix="meet-"))
    try:
        return _run(
            video=video,
            title=title,
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm,
            import_media=import_media,
            settings=settings,
            store=store,
            workdir=workdir,
            today=today or date.today().isoformat(),
            progress=progress,
        )
    finally:
        if not keep_wav:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)


def _run(
    *,
    video: Path,
    title: str | None,
    mic_track: int,
    others_track: int,
    no_llm: bool,
    import_media: bool,
    settings: Settings,
    store: Store,
    workdir: Path,
    today: str,
    progress: ProgressCb,
) -> tuple[int, MeetingResult, Path]:
    # Imports pesados (torch/whisper/pyannote) só aqui
    from . import audio as audio_mod
    from . import diarize as diarize_mod
    from . import merge as merge_mod
    from . import render as render_mod
    from . import transcribe as transcribe_mod
    from . import voicebank as voicebank_mod

    progress("Preparando áudio…")
    try:
        tracks = audio_mod.prepare(video, workdir, mic_track, others_track)
    except Exception as exc:
        raise RuntimeError(f"Erro ao preparar áudio: {exc}") from exc

    embeddings: dict[str, Any] = {}

    if tracks.mic is not None:
        progress("Transcrevendo microfone…")
        try:
            mic_segs = transcribe_mod.transcribe(tracks.mic, settings)
        except Exception as exc:
            raise RuntimeError(f"Erro ao transcrever microfone: {exc}") from exc

        progress("Transcrevendo outros participantes…")
        try:
            others_segs = transcribe_mod.transcribe(tracks.others, settings)
        except Exception as exc:
            raise RuntimeError(
                f"Erro ao transcrever outros participantes: {exc}"
            ) from exc

        progress("Diarizando falantes…")
        try:
            turns, embeddings = diarize_mod.diarize(tracks.others, settings)
        except Exception as exc:
            raise RuntimeError(f"Erro na diarização: {exc}") from exc

        others_segs = merge_mod.assign_speakers(others_segs, turns)
        segments = merge_mod.combine(mic_segs, others_segs)
    else:
        progress(
            "1 track só — sem separação automática da sua voz ('me'). "
            "Transcrevendo…"
        )
        try:
            segments = transcribe_mod.transcribe(tracks.mixed, settings)
        except Exception as exc:
            raise RuntimeError(f"Erro ao transcrever: {exc}") from exc

        progress("Diarizando falantes…")
        try:
            turns, embeddings = diarize_mod.diarize(tracks.mixed, settings)
        except Exception as exc:
            raise RuntimeError(f"Erro na diarização: {exc}") from exc

        segments = merge_mod.assign_speakers(segments, turns)

    progress("Resolvendo falantes no banco de vozes…")
    mapping = voicebank_mod.resolve(
        embeddings, store, settings.similarity_threshold
    )
    unresolved = [label for label, name in mapping.items() if label == name]
    segments = merge_mod.rename_speakers(segments, mapping)
    participants = sorted({s.speaker for s in segments if s.speaker})

    summary = ""
    action_items: list[ActionItem] = []
    suggested_title = ""

    if not no_llm:
        progress("Extraindo resumo e action items (LLM)…")
        from . import extract as extract_mod

        try:
            summary, action_items, suggested_title = extract_mod.extract(
                segments, participants, settings
            )
        except Exception as exc:
            raise RuntimeError(f"Erro na extração LLM: {exc}") from exc

    meeting_title = title or suggested_title or video.stem
    result = MeetingResult(
        source=str(video),
        date=today,
        title=meeting_title,
        duration=tracks.duration,
        participants=participants,
        summary=summary,
        action_items=action_items,
        segments=segments,
    )

    progress("Salvando markdown e banco…")
    md_content = render_mod.to_markdown(result)
    filename = render_mod.meeting_filename(result)
    md_path = settings.output_dir / filename
    md_path.write_text(md_content, encoding="utf-8")
    meeting_id = store.save_meeting(result, md_path)

    if import_media:
        progress("Importando vídeo para o meet (media/{id})…")
        try:
            dest = store.adopt_media(meeting_id, settings.data_dir, video)
            result.source = str(dest)
        except Exception as exc:
            # Pipeline ok; import falhou — reunião fica com path externo
            progress(f"Aviso: não importou mídia ({exc})")

    if unresolved:
        import numpy as np

        pending_dir = settings.data_dir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_data = {
            lbl: embeddings[lbl] for lbl in unresolved if lbl in embeddings
        }
        if pending_data:
            np.savez(str(pending_dir / f"{meeting_id}.npz"), **pending_data)

    progress(f"Concluído — reunião #{meeting_id}")
    return meeting_id, result, md_path
