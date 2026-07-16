"""Núcleo do pipeline (CLI e web compartilham esta função)."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from .config import Settings
from .models import ActionItem, MeetingResult, VisualEvidence
from .progress import ProgressCallback, ProgressTracker, StepSpec
from .store import Store


def _process_plan(*, no_llm: bool, import_media: bool) -> tuple[StepSpec, ...]:
    steps: list[StepSpec] = []
    if not no_llm:
        steps.append(StepSpec("auth", "Validar acesso ao LLM", 2.0))
    steps.extend(
        (
            StepSpec("audio", "Preparar áudio", 7.0),
            StepSpec("transcribe", "Transcrever áudio", 55.0),
            StepSpec("diarize", "Identificar falantes", 22.0),
            StepSpec("speakers", "Reconhecer vozes", 4.0),
        )
    )
    if not no_llm:
        steps.append(StepSpec("normalize", "Revisar termos da transcrição", 4.0))
        steps.append(StepSpec("llm", "Gerar resumo e tarefas", 5.0))
    steps.append(StepSpec("save", "Salvar reunião", 2.0))
    if import_media:
        steps.append(StepSpec("import", "Importar mídia", 3.0))
    return tuple(steps)


def _fmt_progress_time(seconds: float, duration: float) -> str:
    def fmt(value: float) -> str:
        minutes, secs = divmod(max(int(value), 0), 60)
        return f"{minutes:02d}:{secs:02d}"

    return f"{fmt(seconds)} de {fmt(duration)} de áudio"


def run_pipeline(
    video: Path,
    *,
    settings: Settings,
    store: Store,
    title: str | None = None,
    mic_track: int = 1,
    others_track: int = 2,
    no_llm: bool = False,
    analyze_visual: bool = False,
    keep_wav: bool = False,
    import_media: bool = True,
    today: str | None = None,
    on_progress: ProgressCallback | None = None,
    num_speakers: int = 0,
    project_id: int | None = None,
) -> tuple[int, MeetingResult, Path]:
    """Processa gravação de ponta a ponta com progresso estruturado."""
    if not video.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {video}")

    tracker = ProgressTracker(
        _process_plan(no_llm=no_llm, import_media=import_media),
        on_progress,
    )
    workdir = Path(tempfile.mkdtemp(prefix="meet-"))
    try:
        return _run(
            video=video,
            title=title,
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm,
            analyze_visual=analyze_visual,
            import_media=import_media,
            settings=settings,
            store=store,
            workdir=workdir,
            today=today or date.today().isoformat(),
            tracker=tracker,
            num_speakers=num_speakers,
            project_id=project_id,
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
    analyze_visual: bool = False,
    settings: Settings,
    store: Store,
    workdir: Path,
    today: str,
    tracker: ProgressTracker,
    title: str | None = None,
    num_speakers: int = 0,
    project_id: int | None = None,
) -> tuple[MeetingResult, dict[str, Any], list[str]]:
    """Executa áudio → transcrição → diarização → merge → LLM, sem persistir."""
    from . import audio as audio_mod
    from . import diarize as diarize_mod
    from . import merge as merge_mod
    from . import transcribe as transcribe_mod
    from . import voicebank as voicebank_mod

    if not no_llm:
        from . import extract as extract_mod

        tracker.start("auth", "Validando acesso ao LLM")
        try:
            extract_mod.validate_credentials(settings)
        except Exception as exc:
            raise RuntimeError(f"Erro na autenticação LLM: {exc}") from exc
        tracker.update(1.0, "Acesso ao LLM validado")

    tracker.start("audio", "Preparando faixas de áudio")
    try:
        tracks = audio_mod.prepare(
            video,
            workdir,
            mic_track,
            others_track,
            on_progress=lambda fraction: tracker.update(
                fraction, "Preparando faixas de áudio"
            ),
        )
    except Exception as exc:
        raise RuntimeError(f"Erro ao preparar áudio: {exc}") from exc

    project = store.get_project(project_id) if project_id is not None else None
    learned_terms = store.project_vocabulary(project_id) if project_id is not None else []
    embeddings: dict[str, Any] = {}
    tracker.start("transcribe", "Transcrevendo áudio")
    duration = tracks.duration

    def transcription_progress(
        offset: float,
        span: float,
        label: str,
    ) -> Callable[[float, float], None]:
        return lambda fraction, seconds: tracker.update(
            offset + span * fraction,
            f"{label} · {_fmt_progress_time(seconds, duration)}",
        )

    if tracks.mic is not None:
        _model = transcribe_mod.load_model(settings)
        try:
            try:
                mic_segs = transcribe_mod.transcribe_wav(
                    _model,
                    tracks.mic,
                    settings,
                    on_progress=transcription_progress(
                        0.0, 0.5, "Transcrevendo microfone"
                    ),
                    hotwords=learned_terms,
                )
            except Exception as exc:
                raise RuntimeError(f"Erro ao transcrever microfone: {exc}") from exc

            try:
                others_segs = transcribe_mod.transcribe_wav(
                    _model,
                    tracks.others,
                    settings,
                    on_progress=transcription_progress(
                        0.5, 0.5, "Transcrevendo participantes"
                    ),
                    hotwords=learned_terms,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Erro ao transcrever outros participantes: {exc}"
                ) from exc
        finally:
            transcribe_mod.release_model(_model)
        diarize_input = tracks.others
    else:
        try:
            segments = transcribe_mod.transcribe(
                tracks.mixed,
                settings,
                on_progress=transcription_progress(
                    0.0, 1.0, "Transcrevendo reunião"
                ),
                hotwords=learned_terms,
            )
        except Exception as exc:
            raise RuntimeError(f"Erro ao transcrever: {exc}") from exc
        diarize_input = tracks.mixed

    tracker.start("diarize", "Carregando identificação de falantes", determinate=False)
    diarize_ranges = {
        "segmentation": (0.0, 0.55),
        "speaker_counting": (0.55, 0.05),
        "embeddings": (0.60, 0.32),
        "discrete_diarization": (0.92, 0.08),
    }

    def diarize_progress(
        key: str,
        label: str,
        fraction: float | None,
    ) -> None:
        start, span = diarize_ranges.get(key, (0.0, 1.0))
        tracker.update(
            None if fraction is None else start + span * fraction,
            label,
        )

    try:
        turns, embeddings = diarize_mod.diarize(
            diarize_input,
            settings,
            num_speakers=num_speakers,
            on_progress=diarize_progress,
        )
    except Exception as exc:
        raise RuntimeError(f"Erro na diarização: {exc}") from exc

    if tracks.mic is not None:
        others_segs = merge_mod.assign_speakers(others_segs, turns)
        segments = merge_mod.combine(mic_segs, others_segs)
    else:
        segments = merge_mod.assign_speakers(segments, turns)

    tracker.start("speakers", "Reconhecendo vozes conhecidas", determinate=False)
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
    tracker.update(1.0, "Vozes reconhecidas")

    if not no_llm:
        tracker.start("normalize", "Revisando termos da transcrição", determinate=True)
        segments = extract_mod.normalize_transcript(
            segments,
            settings,
            project_name=project.name if project else None,
            project_description=project.description if project else None,
            learned_terms=learned_terms,
            on_progress=tracker.update,
        )

    summary = ""
    action_items: list[ActionItem] = []
    suggested_title = ""
    if not no_llm:
        tracker.start(
            "llm",
            "Gerando resumo e tarefas com LLM",
            determinate=False,
        )
        from . import extract as extract_mod

        visual_observations: list[dict] = []
        if analyze_visual:
            try:
                from . import visual as visual_mod

                tracker.update(None, "Selecionando evidências visuais")
                frames = visual_mod.extract_relevant_frames(
                    video,
                    segments,
                    tracks.duration,
                    workdir / "visual-frames",
                )
                if frames:
                    tracker.update(None, f"Analisando {len(frames)} evidências visuais")
                    visual_observations = extract_mod.analyze_visual_frames(
                        extract_mod.get_provider(settings), frames, segments
                    )
            except Exception:
                # Visão é enriquecimento best-effort; transcript continua sendo a fonte base.
                visual_observations = []

        try:
            extract_kwargs: dict[str, Any] = {"on_progress": tracker.update}
            if visual_observations:
                extract_kwargs["visual_observations"] = visual_observations
            summary, action_items, suggested_title, facts = extract_mod.extract(
                segments,
                participants,
                settings,
                **extract_kwargs,
            )
        except Exception as exc:
            raise RuntimeError(f"Erro na extração LLM: {exc}") from exc

    visual_evidence = [
        VisualEvidence(
            timestamp=float(observation["timestamp"]),
            image_path=str(observation["image_path"]),
            description=str(observation["description"]),
            visible_text=list(observation.get("visible_text") or []),
            relevance=str(observation.get("relevance") or "medium"),
        )
        for observation in (visual_observations if not no_llm else [])
        if observation.get("image_path")
    ]

    def linked(start: float | None, end: float | None) -> list[VisualEvidence]:
        if start is None:
            return []
        upper = end if end is not None else start
        return [
            evidence
            for evidence in visual_evidence
            if start - 5.0 <= evidence.timestamp <= upper + 5.0
        ]

    for item in action_items:
        item.visual_evidence = linked(item.source_start, item.source_end)
    for fact in facts if not no_llm else []:
        fact.visual_evidence = linked(fact.source_start, fact.source_end)

    meeting_title = title or suggested_title or video.stem
    result = MeetingResult(
        source=str(video),
        date=today,
        title=meeting_title,
        duration=tracks.duration,
        participants=participants,
        summary=summary,
        action_items=action_items,
        facts=facts if not no_llm else [],
        segments=segments,
        visual_evidence=visual_evidence,
        speaker_matches=speaker_matches,
    )
    return result, embeddings, unresolved


def _save_pending(
    meeting_id: int,
    embeddings: dict[str, Any],
    unresolved: list[str],
    data_dir: Path,
) -> None:
    if not unresolved:
        return
    import numpy as np

    pending_dir = data_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_data = {label: embeddings[label] for label in unresolved if label in embeddings}
    if pending_data:
        np.savez(str(pending_dir / f"{meeting_id}.npz"), **pending_data)


def _run(
    *,
    video: Path,
    title: str | None,
    mic_track: int,
    others_track: int,
    no_llm: bool,
    analyze_visual: bool,
    import_media: bool,
    settings: Settings,
    store: Store,
    workdir: Path,
    today: str,
    tracker: ProgressTracker,
    num_speakers: int = 0,
    project_id: int | None = None,
) -> tuple[int, MeetingResult, Path]:
    from . import render as render_mod

    result, embeddings, unresolved = _analyse(
        video=video,
        mic_track=mic_track,
        others_track=others_track,
        no_llm=no_llm,
        analyze_visual=analyze_visual,
        settings=settings,
        store=store,
        workdir=workdir,
        today=today,
        tracker=tracker,
        title=title,
        num_speakers=num_speakers,
        project_id=project_id,
    )

    tracker.start("save", "Salvando markdown e banco")
    md_content = render_mod.to_markdown(result)
    md_path = render_mod.allocate_meeting_md_path(settings.output_dir, result)
    md_path.write_text(md_content, encoding="utf-8")
    meeting_id = store.save_meeting(result, md_path, project_id=project_id)
    result.visual_evidence = store.replace_visual_evidence(
        meeting_id, result.visual_evidence, settings.data_dir
    )
    _save_pending(meeting_id, embeddings, unresolved, settings.data_dir)
    tracker.update(1.0, "Reunião salva")

    if import_media:
        tracker.start("import", "Importando mídia para o acervo")
        try:
            dest = store.adopt_media(
                meeting_id,
                settings.data_dir,
                video,
                on_progress=lambda fraction: tracker.update(
                    fraction, "Importando mídia para o acervo"
                ),
            )
            result.source = str(dest)
        except Exception as exc:
            tracker.mark_current_error(f"Mídia não importada: {exc}")

    tracker.finish(f"Concluído — reunião #{meeting_id}")
    return meeting_id, result, md_path


def reprocess_meeting(
    meeting_id: int,
    *,
    settings: Settings,
    store: Store,
    mic_track: int = 1,
    others_track: int = 2,
    no_llm: bool = False,
    analyze_visual: bool = False,
    on_progress: ProgressCallback | None = None,
    num_speakers: int = 0,
) -> MeetingResult:
    """Reprocessa reunião existente in-place, preservando metadados do usuário."""
    existing = store.get_meeting(meeting_id)
    if existing is None:
        raise ValueError(f"Reunião #{meeting_id} não encontrada")

    source = Path(existing.source).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"Arquivo fonte não encontrado: {existing.source}")

    tracker = ProgressTracker(
        _process_plan(no_llm=no_llm, import_media=False),
        on_progress,
    )
    workdir = Path(tempfile.mkdtemp(prefix="meet-reprocess-"))
    try:
        result, embeddings, unresolved = _analyse(
            video=source,
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm,
            analyze_visual=analyze_visual,
            settings=settings,
            store=store,
            workdir=workdir,
            today=existing.date,
            tracker=tracker,
            num_speakers=num_speakers,
            project_id=existing.project_id,
        )
        result.visual_evidence = store.replace_visual_evidence(
            meeting_id, result.visual_evidence, settings.data_dir
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    result.title = existing.title
    tracker.start("save", "Salvando resultado no banco")
    store.replace_meeting_content(meeting_id, result)
    _save_pending(meeting_id, embeddings, unresolved, settings.data_dir)
    store._regen_md(meeting_id)
    tracker.finish(f"Reprocessamento concluído — reunião #{meeting_id}")
    return result


def reextract_meeting(
    meeting_id: int,
    *,
    settings: Settings,
    store: Store,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Re-extrai resumo e action items via LLM sobre os segmentos existentes."""
    existing = store.get_meeting(meeting_id)
    if existing is None:
        raise ValueError(f"Reunião #{meeting_id} não encontrada")

    from . import extract as extract_mod

    tracker = ProgressTracker(
        (
            StepSpec("auth", "Validar acesso ao LLM", 1.0),
            StepSpec("normalize", "Revisar termos da transcrição", 3.0),
            StepSpec("llm", "Gerar resumo e tarefas", 5.0),
            StepSpec("save", "Salvar reunião", 1.0),
        ),
        on_progress,
    )
    tracker.start("auth", "Validando acesso ao LLM")
    try:
        extract_mod.validate_credentials(settings)
    except Exception as exc:
        raise RuntimeError(f"Erro na autenticação LLM: {exc}") from exc
    tracker.update(1.0, "Acesso ao LLM validado")
    project = (
        store.get_project(existing.project_id)
        if existing.project_id is not None
        else None
    )
    learned_terms = (
        store.project_vocabulary(existing.project_id)
        if existing.project_id is not None
        else []
    )
    tracker.start("normalize", "Revisando termos da transcrição", determinate=True)
    existing.segments = extract_mod.normalize_transcript(
        existing.segments,
        settings,
        project_name=project.name if project else None,
        project_description=project.description if project else None,
        learned_terms=learned_terms,
        on_progress=tracker.update,
    )

    tracker.start("llm", "Gerando resumo e tarefas com LLM", determinate=False)
    try:
        summary, action_items, _suggested_title, facts = extract_mod.extract(
            existing.segments,
            existing.participants,
            settings,
            on_progress=tracker.update,
        )
    except Exception as exc:
        raise RuntimeError(f"Erro na extração LLM: {exc}") from exc

    tracker.start("save", "Salvando resultado no banco")
    store.update_segment_normalization(meeting_id, existing.segments)
    store.update_meeting_extract(meeting_id, summary, action_items, None, facts)
    store._regen_md(meeting_id)
    tracker.finish(f"Re-extração concluída — reunião #{meeting_id}")
