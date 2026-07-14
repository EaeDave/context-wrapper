"""Núcleo do pipeline (CLI e web compartilham esta função)."""

from __future__ import annotations

import shutil
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
    num_speakers: int = 0,
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
            num_speakers=num_speakers,
        )
    finally:
        if not keep_wav:
            shutil.rmtree(workdir, ignore_errors=True)


def _analyse(
    *,
    video: Path,
    mic_track: int,
    others_track: int,
    no_llm: bool,
    settings: Settings,
    store: Store,
    workdir: Path,
    today: str,
    progress: ProgressCb,
    title: str | None = None,
    num_speakers: int = 0,
) -> tuple[MeetingResult, dict[str, Any], list[str]]:
    """Faz análise completa (audio→transcribe→diarize→merge→llm) sem salvar no banco.

    Retorna (result, embeddings, unresolved_labels).
    """
    # Imports pesados (torch/whisper/pyannote) só aqui
    from . import audio as audio_mod
    from . import diarize as diarize_mod
    from . import merge as merge_mod
    from . import transcribe as transcribe_mod
    from . import voicebank as voicebank_mod

    if not no_llm:
        from . import extract as extract_mod

        progress("Validando acesso ao LLM…")
        try:
            extract_mod.validate_credentials(settings)
        except Exception as exc:
            raise RuntimeError(f"Erro na autenticação LLM: {exc}") from exc

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
            turns, embeddings = diarize_mod.diarize(tracks.others, settings, num_speakers=num_speakers)
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
            turns, embeddings = diarize_mod.diarize(tracks.mixed, settings, num_speakers=num_speakers)
        except Exception as exc:
            raise RuntimeError(f"Erro na diarização: {exc}") from exc

        segments = merge_mod.assign_speakers(segments, turns)

    progress("Resolvendo falantes no banco de vozes…")
    mapping_with_scores = voicebank_mod.resolve_with_scores(
        embeddings, store, settings.similarity_threshold
    )
    mapping = {label: name for label, (name, _) in mapping_with_scores.items()}
    unresolved = [label for label, name in mapping.items() if label == name]
    speaker_matches = {
        name: score
        for label, (name, score) in mapping_with_scores.items()
        if label != name
    }
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
        speaker_matches=speaker_matches,
    )
    return result, embeddings, unresolved


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
    num_speakers: int = 0,
) -> tuple[int, MeetingResult, Path]:
    from . import render as render_mod

    result, embeddings, unresolved = _analyse(
        video=video,
        mic_track=mic_track,
        others_track=others_track,
        no_llm=no_llm,
        settings=settings,
        store=store,
        workdir=workdir,
        today=today,
        progress=progress,
        title=title,
        num_speakers=num_speakers,
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


def reprocess_meeting(
    meeting_id: int,
    *,
    settings: Settings,
    store: Store,
    mic_track: int = 1,
    others_track: int = 2,
    no_llm: bool = False,
    on_progress: ProgressCb | None = None,
    num_speakers: int = 0,
) -> MeetingResult:
    """Reprocessa reunião existente in-place (áudio completo → replace_meeting_content).

    Preserva title do usuário, source, date e media. Grava pending .npz para
    labels não resolvidos. Regenera o .md.
    """
    from . import render as render_mod

    progress = on_progress or _noop
    existing = store.get_meeting(meeting_id)
    if existing is None:
        raise ValueError(f"Reunião #{meeting_id} não encontrada")

    source = Path(existing.source).expanduser()
    if not source.is_file():
        raise FileNotFoundError(
            f"Arquivo fonte não encontrado: {existing.source}"
        )

    workdir = Path(tempfile.mkdtemp(prefix="meet-reprocess-"))
    try:
        result, embeddings, unresolved = _analyse(
            video=source,
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm,
            settings=settings,
            store=store,
            workdir=workdir,
            today=existing.date,
            progress=progress,
            num_speakers=num_speakers,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Preservar o title existente (não sobrescrever com sugestão do LLM)
    result.title = existing.title

    progress("Salvando resultado no banco…")
    store.replace_meeting_content(meeting_id, result)

    # Gravar pending .npz para labels não resolvidos
    if unresolved:
        import numpy as np

        pending_dir = settings.data_dir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_data = {
            lbl: embeddings[lbl] for lbl in unresolved if lbl in embeddings
        }
        if pending_data:
            np.savez(str(pending_dir / f"{meeting_id}.npz"), **pending_data)

    # Regenerar .md
    store._regen_md(meeting_id)

    progress(f"Reprocessamento concluído — reunião #{meeting_id}")
    return result


def reextract_meeting(
    meeting_id: int,
    *,
    settings: Settings,
    store: Store,
    on_progress: ProgressCb | None = None,
) -> None:
    """Re-extrai resumo e action items via LLM sobre os segments existentes.

    NÃO reprocessa áudio. Preserva title existente.
    """
    progress = on_progress or _noop
    existing = store.get_meeting(meeting_id)
    if existing is None:
        raise ValueError(f"Reunião #{meeting_id} não encontrada")

    from . import extract as extract_mod

    progress("Extraindo resumo e action items (LLM)…")
    try:
        summary, action_items, _suggested_title = extract_mod.extract(
            existing.segments, existing.participants, settings
        )
    except Exception as exc:
        raise RuntimeError(f"Erro na extração LLM: {exc}") from exc

    progress("Salvando resultado no banco…")
    store.update_meeting_extract(meeting_id, summary, action_items, None)

    store._regen_md(meeting_id)
    progress(f"Re-extração concluída — reunião #{meeting_id}")
