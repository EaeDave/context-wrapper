"""App FastAPI — API JSON + SPA (React)."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Annotated, AsyncGenerator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import hf_token_source, load_settings, save_local_settings
from ..store import Store
from ..anthropic_oauth import (
    build_authorize_url,
    clear_tokens,
    exchange_code,
    generate_pkce,
    load_tokens,
    save_tokens,
)
from .jobs import Job, JobStatus, manager

WEB_DIR = Path(__file__).resolve().parent
DIST_DIR = WEB_DIR / "dist"

# Extensões aceitas no seletor de arquivos
MEDIA_EXTS = {".mkv", ".mp4", ".mov", ".webm", ".wav", ".m4a", ".mp3", ".ogg", ".flac"}

# Pastas rápidas no seletor
QUICK_DIRS = [
    Path.home(),
    Path.home() / "Videos",
    Path.home() / "Vídeos",
    Path.home() / "Downloads",
    Path.home() / "reunioes",
]


def _settings_store():
    settings = load_settings()
    return settings, Store(settings.db_path)


def _pending_labels(settings, meeting_id: int) -> list[str]:
    path = settings.data_dir / "pending" / f"{meeting_id}.npz"
    if not path.exists():
        return []
    import numpy as np

    data = np.load(str(path))
    return sorted(data.files)


def _group_transcript(segments) -> list[dict]:
    """Agrupa segmentos consecutivos do mesmo falante.

    Retorna start/end numéricos + text concatenado + seg_ids dos segmentos agrupados.
    """
    groups: list[dict] = []
    for seg in segments:
        if groups and groups[-1]["speaker"] == seg.speaker:
            groups[-1]["_texts"].append(seg.text)
            groups[-1]["end"] = seg.end
            if seg.id is not None:
                groups[-1]["seg_ids"].append(seg.id)
        else:
            seg_ids = [seg.id] if seg.id is not None else []
            groups.append(
                {
                    "speaker": seg.speaker or "?",
                    "start": seg.start,
                    "end": seg.end,
                    "_texts": [seg.text],
                    "seg_ids": seg_ids,
                }
            )
    return [
        {
            "speaker": g["speaker"],
            "start": g["start"],
            "end": g["end"],
            "text": " ".join(g["_texts"]),
            "seg_ids": g["seg_ids"],
        }
        for g in groups
    ]


def _serialize_job(job: Job) -> dict:
    """Serializa Job para dict com status como string (JobStatus é str Enum)."""
    return {
        "id": job.id,
        "kind": job.kind,
        "label": job.label,
        "status": job.status.value,
        "stage": job.stage,
        "error": job.error,
        "meeting_id": job.meeting_id,
        "result_path": job.result_path,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }


def _highlight_snippet(raw: str) -> str:
    """Converte marcadores FTS5 [word] para <mark>word</mark>."""
    return re.sub(r"\[([^\]]*)\]", r"<mark>\1</mark>", raw)


# ── Pydantic request bodies ──────────────────────────────────────────────────


class PatchMeetingBody(BaseModel):
    title: str


class RelinkBody(BaseModel):
    path: str
    import_media: bool = True


class AssignBody(BaseModel):
    label: str
    name: str


class ProcessBody(BaseModel):
    video: str
    title: str = ""
    mic_track: int = 1
    others_track: int = 2
    no_llm: bool = False
    import_media: bool = True
    num_speakers: int = 0


class BulkDeleteBody(BaseModel):
    ids: list[int]


class HFTokenBody(BaseModel):
    token: str


class LLMSettingsBody(BaseModel):
    provider: str
    model: str


class ExchangeBody(BaseModel):
    code: str
    state: str


class PatchActionItemBody(BaseModel):
    what: str | None = None
    where: str | None = None
    details: str | None = None
    requested_by: str | None = None
    priority: str | None = None
    status: str | None = None
    due: str | None = None


class AddActionItemBody(BaseModel):
    what: str
    where: str | None = None
    details: str | None = None
    requested_by: str | None = None
    priority: str = "media"


class PatchTurnBody(BaseModel):
    seg_ids: list[int]
    text: str | None = None
    speaker: str | None = None


class ReprocessBody(BaseModel):
    mic_track: int = 1
    others_track: int = 2
    no_llm: bool = False
    num_speakers: int = 0



class RenameSpeakerBody(BaseModel):
    new_name: str


class TuningBody(BaseModel):
    whisper_model: str | None = None
    language: str | None = None
    similarity_threshold: float | None = None
    device: str | None = None
    compute_type: str | None = None

# Verifiers PKCE pendentes: {state → verifier}. Limpo após uso, max 5 entradas.
_pending_verifiers: dict[str, str] = {}
_MAX_PENDING = 5
_VALID_LLM_PROVIDERS = frozenset({"claude-code", "anthropic", "openai", "ollama"})

# ── App ──────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(title="meet", docs_url=None, redoc_url=None)

    # ── Meetings ──────────────────────────────────────────────────────────────

    @app.get("/api/meetings")
    def api_list_meetings() -> list[dict]:
        _, store = _settings_store()
        rows = store.list_meeting_rows()
        return [
            {
                "id": r.id,
                "date": r.date,
                "title": r.title,
                "source": r.source,
                "source_origin": r.source_origin,
                "media_managed": r.media_managed,
                "media_ok": r.media_ok,
                "duration": r.duration,
            }
            for r in rows
        ]

    @app.get("/api/search")
    def api_search(q: str = "") -> list[dict]:
        if not q.strip():
            return []
        _, store = _settings_store()
        results = store.search(q, limit=30)
        return [
            {
                "meeting_id": int(r["meeting_id"]),
                "title": r["title"],
                "date": r["date"],
                "kind": r["kind"],
                "snippet": _highlight_snippet(r["snippet"]),
            }
            for r in results
        ]

    # Registrar bulk-delete ANTES de /{meeting_id} para evitar conflito de rota
    @app.post("/api/meetings/bulk-delete")
    def api_bulk_delete(body: BulkDeleteBody) -> dict:
        clean = sorted({i for i in body.ids if i > 0})
        if not clean:
            raise HTTPException(400, "Nenhuma reunião selecionada")
        settings, store = _settings_store()
        deleted = store.delete_meetings(clean, data_dir=settings.data_dir)
        return {"deleted": deleted}

    @app.get("/api/meetings/{meeting_id}")
    def api_meeting_detail(meeting_id: int) -> dict:
        from ..audio import (
            PREVIEW_FULL,
            PREVIEW_WEB,
            listen_mix_path,
            listen_preview_path,
            probe_video_size,
            probe_video_streams,
        )

        settings, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")

        groups = _group_transcript(result.segments)
        # Labels pendentes só valem enquanto o transcript ainda usa SPEAKER_XX;
        # depois de nomeado, some do card (o .npz fica p/ histórico).
        speakers_now = {seg.speaker for seg in result.segments}
        pending = [
            lbl for lbl in _pending_labels(settings, meeting_id) if lbl in speakers_now
        ]
        source = Path(result.source)
        source_exists = source.is_file()
        has_video = False
        preview_ready = False
        preview_full_ready = False
        mix_ready = False
        source_w, source_h = 0, 0

        if source_exists:
            try:
                has_video = probe_video_streams(source) >= 1
                if has_video:
                    source_w, source_h = probe_video_size(source)
            except Exception:
                has_video = False
            preview_ready = has_video and listen_preview_path(source, PREVIEW_WEB).is_file()
            preview_full_ready = has_video and listen_preview_path(source, PREVIEW_FULL).is_file()
            mix_ready = listen_mix_path(source).is_file()

        web_h = min(720, source_h) if source_h else 720
        full_h = source_h or 1080
        media_managed = bool(getattr(result, "media_managed", False))
        source_origin = getattr(result, "source_origin", result.source) or result.source
        md_path = getattr(result, "md_path", None)

        return {
            "id": meeting_id,
            "title": result.title,
            "date": result.date,
            "duration": result.duration,
            "source": result.source,
            "source_origin": source_origin,
            "media_managed": media_managed,
            "md_path": str(md_path) if md_path else None,
            "source_exists": source_exists,
            "has_video": has_video,
            "preview_ready": preview_ready,
            "preview_full_ready": preview_full_ready,
            "mix_ready": mix_ready,
            "source_w": source_w,
            "source_h": source_h,
            "quality_web_h": web_h,
            "quality_full_h": full_h,
            "participants": result.participants,
            "summary": result.summary or "",
            "action_items": [
                {
                    "id": ai.id,
                    "what": ai.what,
                    "where": ai.where,
                    "details": ai.details,
                    "requested_by": ai.requested_by,
                    "priority": ai.priority,
                    "status": ai.status,
                    "due": ai.due,
                }
                for ai in result.action_items
            ],
            "pending": pending,
            "groups": groups,
            "speaker_matches": result.speaker_matches,
        }

    @app.patch("/api/meetings/{meeting_id}")
    def api_patch_meeting(meeting_id: int, body: PatchMeetingBody) -> dict:
        from .. import render as render_mod

        if not body.title.strip():
            raise HTTPException(400, "Título vazio")
        settings, store = _settings_store()
        try:
            ok = store.update_title(meeting_id, body.title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not ok:
            raise HTTPException(404, "Reunião não encontrada")
        result = store.get_meeting(meeting_id)
        if result is not None:
            md_path = getattr(result, "md_path", None)
            if md_path:
                Path(md_path).write_text(render_mod.to_markdown(result), encoding="utf-8")
        return {"ok": True}

    @app.delete("/api/meetings/{meeting_id}", status_code=204)
    def api_delete_meeting(meeting_id: int) -> None:
        settings, store = _settings_store()
        if not store.delete_meeting(meeting_id, data_dir=settings.data_dir):
            raise HTTPException(404, "Reunião não encontrada")

    @app.post("/api/meetings/{meeting_id}/relink")
    def api_relink(meeting_id: int, body: RelinkBody) -> dict:
        settings, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        src = Path(body.path).expanduser()
        if not src.is_file():
            raise HTTPException(400, f"Arquivo inválido: {body.path}")
        if body.import_media:
            store.adopt_media(meeting_id, settings.data_dir, src)
        else:
            store.set_media(
                meeting_id,
                source=src.resolve(),
                source_origin=str(src.resolve()),
                media_managed=False,
            )
        return {"ok": True}

    @app.post("/api/meetings/{meeting_id}/assign")
    def api_assign(meeting_id: int, body: AssignBody) -> dict:
        import numpy as np

        from .. import render as render_mod
        from .. import voicebank as voicebank_mod

        settings, store = _settings_store()
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Nome vazio")

        pending_path = settings.data_dir / "pending" / f"{meeting_id}.npz"
        if pending_path.exists():
            data = np.load(str(pending_path))
            if body.label in data:
                voicebank_mod.enroll(name, data[body.label], store)

        store.update_speaker(meeting_id, body.label, name)
        result = store.get_meeting(meeting_id)
        if result is not None:
            md_path = getattr(result, "md_path", None)
            if md_path:
                Path(md_path).write_text(render_mod.to_markdown(result), encoding="utf-8")
        return {"ok": True}

    @app.post("/api/meetings/{meeting_id}/mix")
    def api_mix(meeting_id: int) -> dict:
        _, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        path = Path(result.source)
        if not path.is_file():
            raise HTTPException(400, "Arquivo fonte não encontrado no disco")
        job = manager.submit(kind="mix", label=f"mix · {path.name}", video=str(path))
        return _serialize_job(job)

    # ── Action items ──────────────────────────────────────────────────────────

    @app.patch("/api/action-items/{item_id}")
    def api_patch_action_item(item_id: int, body: PatchActionItemBody) -> dict:
        _, store = _settings_store()
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not store.update_action_item(item_id, fields):
            raise HTTPException(404, "Action item não encontrado")
        return {"ok": True}

    @app.post("/api/meetings/{meeting_id}/action-items")
    def api_add_action_item(meeting_id: int, body: AddActionItemBody) -> dict:
        from ..models import ActionItem

        if not body.what.strip():
            raise HTTPException(400, "Campo 'what' obrigatório")
        _, store = _settings_store()
        if store.get_meeting(meeting_id) is None:
            raise HTTPException(404, "Reunião não encontrada")
        item = ActionItem(
            what=body.what.strip(),
            where=body.where,
            details=body.details,
            requested_by=body.requested_by,
            priority=body.priority,
        )
        new_id = store.add_action_item(meeting_id, item)
        return {"id": new_id}

    @app.delete("/api/action-items/{item_id}", status_code=204)
    def api_delete_action_item(item_id: int) -> None:
        _, store = _settings_store()
        if not store.delete_action_item(item_id):
            raise HTTPException(404, "Action item não encontrado")

    @app.get("/api/tasks")
    def api_tasks(status: str = "aberto") -> list[dict]:
        if status not in {"aberto", "feito", "todos"}:
            raise HTTPException(400, "status deve ser aberto|feito|todos")
        _, store = _settings_store()
        return store.list_tasks(status)

    # ── Editar turno do transcript ────────────────────────────────────────────

    @app.patch("/api/meetings/{meeting_id}/turn")
    def api_patch_turn(meeting_id: int, body: PatchTurnBody) -> dict:
        if not body.seg_ids:
            raise HTTPException(400, "seg_ids não pode ser vazio")
        if body.text is None and body.speaker is None:
            raise HTTPException(400, "Forneça text ou speaker")
        _, store = _settings_store()
        if not store.update_turn(meeting_id, body.seg_ids, body.text, body.speaker):
            raise HTTPException(400, "Nenhum seg_id encontrado nesta reunião")
        return {"ok": True}

    # ── Reprocess / reextract ─────────────────────────────────────────────────

    @app.post("/api/meetings/{meeting_id}/reprocess")
    def api_reprocess(meeting_id: int, body: ReprocessBody) -> dict:
        settings, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        src = Path(result.source)
        if not src.is_file():
            raise HTTPException(400, f"Arquivo fonte não encontrado: {result.source}")
        job = manager.submit(
            kind="reprocess",
            label=f"Reprocessamento · {src.name}",
            meeting_id=meeting_id,
            mic_track=body.mic_track,
            others_track=body.others_track,
            no_llm=body.no_llm,
            num_speakers=body.num_speakers,
        )
        return _serialize_job(job)

    @app.post("/api/meetings/{meeting_id}/reextract")
    def api_reextract(meeting_id: int) -> dict:
        _, store = _settings_store()
        if store.get_meeting(meeting_id) is None:
            raise HTTPException(404, "Reunião não encontrada")
        job = manager.submit(
            kind="reextract",
            label=f"Re-extração · #{meeting_id}",
            meeting_id=meeting_id,
        )
        return _serialize_job(job)


    # ── Process ───────────────────────────────────────────────────────────────

    @app.post("/api/process")
    def api_process(body: ProcessBody) -> dict:
        path = Path(body.video).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"Arquivo inválido: {body.video}")
        job = manager.submit(
            kind="process",
            label=path.name,
            video=str(path.resolve()),
            title=body.title.strip(),
            mic_track=body.mic_track,
            others_track=body.others_track,
            no_llm=body.no_llm,
            import_media=body.import_media,
            num_speakers=body.num_speakers,
        )
        return _serialize_job(job)

    # ── Jobs ──────────────────────────────────────────────────────────────────

    @app.get("/api/jobs")
    def api_jobs(limit: int = 8) -> list[dict]:
        return [_serialize_job(j) for j in manager.list_recent(limit)]

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Job não encontrado")
        return _serialize_job(job)

    @app.get("/api/jobs/{job_id}/events")
    async def api_job_events(job_id: str) -> StreamingResponse:
        if manager.get(job_id) is None:
            raise HTTPException(404, "Job não encontrado")

        async def event_stream() -> AsyncGenerator[str, None]:
            last_status: JobStatus | None = None
            last_stage: str | None = None
            last_error: str | None = None

            while True:
                job = manager.get(job_id)
                if job is None:
                    break

                changed = (
                    job.status != last_status
                    or job.stage != last_stage
                    or job.error != last_error
                )
                if changed:
                    last_status = job.status
                    last_stage = job.stage
                    last_error = job.error
                    yield f"data: {json.dumps(_serialize_job(job))}\n\n"

                    if job.status in (JobStatus.done, JobStatus.error):
                        break

                await asyncio.sleep(0.4)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # ── Browse ────────────────────────────────────────────────────────────────

    @app.get("/api/browse")
    def api_browse(path: str = "") -> dict:
        browse_path = Path(path).expanduser() if path else Path.home()
        if not browse_path.exists():
            browse_path = Path.home()
        if browse_path.is_file():
            browse_path = browse_path.parent

        entries: list[dict] = []
        try:
            kids = sorted(
                browse_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except PermissionError:
            kids = []

        for p in kids:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                entries.append({"name": p.name, "path": str(p), "kind": "dir", "size": None})
            elif p.suffix.lower() in MEDIA_EXTS:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                entries.append({"name": p.name, "path": str(p), "kind": "file", "size": size})

        parent = str(browse_path.parent) if browse_path != browse_path.parent else None
        quick = [str(d) for d in QUICK_DIRS if d.exists()]

        return {
            "path": str(browse_path),
            "parent": parent,
            "quick": quick,
            "entries": entries,
        }

    @app.get("/api/probe")
    def api_probe(path: str = Query(...)) -> dict:
        """Conta streams de áudio/vídeo de um arquivo local (só sob $HOME).

        A UI usa p/ avisar quando a separação mic/outros não vai funcionar
        (arquivo com menos de 2 faixas de áudio, ex. mp4 já mixado).
        """
        from ..audio import probe_audio_streams, probe_video_streams

        p = Path(path).expanduser().resolve()
        home = Path.home().resolve()
        try:
            p.relative_to(home)
        except ValueError as exc:
            raise HTTPException(403, "Acesso fora do home negado") from exc
        if not p.is_file():
            raise HTTPException(404, "Arquivo não encontrado")
        try:
            audio = probe_audio_streams(p)
            video = probe_video_streams(p)
        except Exception as exc:
            raise HTTPException(500, f"ffprobe falhou: {exc}") from exc
        return {"audio_streams": audio, "video_streams": video}

    # ── Speakers ──────────────────────────────────────────────────────────────

    @app.get("/api/speakers")
    def api_speakers() -> list[dict]:
        _, store = _settings_store()
        voices = store.all_voices()
        # meetings count: COUNT DISTINCT meeting_id per speaker
        counts_rows = store._conn.execute(
            "SELECT speaker, COUNT(DISTINCT meeting_id) AS cnt FROM segments"
            " WHERE speaker IS NOT NULL GROUP BY speaker"
        ).fetchall()
        meetings_by_name = {r["speaker"]: r["cnt"] for r in counts_rows}
        return [
            {"name": n, "dims": len(blob) // 4, "meetings": meetings_by_name.get(n, 0)}
            for n, blob in sorted(voices.items())
        ]

    @app.patch("/api/speakers/{name}")
    def api_rename_speaker(name: str, body: RenameSpeakerBody) -> dict:
        new_name = body.new_name.strip()
        if not new_name:
            raise HTTPException(400, "new_name não pode ser vazio")
        _, store = _settings_store()
        store.rename_voice(name, new_name)
        return {"ok": True}

    @app.get("/api/speakers/{name}/usage")
    def api_speaker_usage(name: str) -> list[dict]:
        _, store = _settings_store()
        return store.voice_usage(name)

    @app.delete("/api/speakers/{name}", status_code=204)
    def api_delete_speaker(name: str) -> None:
        _, store = _settings_store()
        store.delete_voice(name)

    # ── Rotas de mídia (comportamento inalterado) ─────────────────────────────

    def _resolve_source(meeting_id: int):
        _, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        source = Path(result.source)
        if not source.is_file():
            raise HTTPException(
                404,
                'Vídeo ausente no disco. Use "Localizar vídeo" na página da reunião.',
            )
        return result, source

    def _serve_listen_file(
        meeting_id: int,
        *,
        kind: str,
        force: bool,
        quality: str = "web",
    ) -> FileResponse:
        """Gera (se preciso) e serve preview de vídeo ou mix de áudio."""
        from ..audio import (
            PREVIEW_FULL,
            PREVIEW_WEB,
            ensure_listen_mix,
            ensure_listen_preview,
            listen_mix_path,
            listen_preview_path,
            probe_video_streams,
        )

        _, source = _resolve_source(meeting_id)
        settings, _ = _settings_store()
        listen_dir = settings.data_dir / "listen"
        listen_dir.mkdir(parents=True, exist_ok=True)

        if kind == "preview":
            has_video = probe_video_streams(source) >= 1
            if not has_video:
                kind = "audio"
            else:
                q = quality if quality in (PREVIEW_WEB, PREVIEW_FULL) else PREVIEW_WEB
                preferred = listen_preview_path(source, q)
                suffix = "full.mp4" if q == PREVIEW_FULL else "mp4"
                fallback = listen_dir / f"{meeting_id}.listen.{suffix}"
                try:
                    path = ensure_listen_preview(
                        source, force=force, output_path=preferred, quality=q
                    )
                except Exception:
                    try:
                        path = ensure_listen_preview(
                            source, force=force, output_path=fallback, quality=q
                        )
                    except Exception as exc:
                        raise HTTPException(
                            500, f"Falha ao gerar preview: {exc}"
                        ) from exc
                return FileResponse(
                    path,
                    media_type="video/mp4",
                    filename=path.name,
                    content_disposition_type="inline",
                    headers={"Cache-Control": "private, max-age=3600"},
                )

        preferred = listen_mix_path(source)
        fallback = listen_dir / f"{meeting_id}.listen.m4a"
        try:
            path = ensure_listen_mix(source, force=force, output_path=preferred)
        except Exception:
            try:
                path = ensure_listen_mix(source, force=force, output_path=fallback)
            except Exception as exc:
                raise HTTPException(500, f"Falha ao gerar mix: {exc}") from exc
        return FileResponse(
            path,
            media_type="audio/mp4",
            filename=path.name,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/meetings/{meeting_id}/preview")
    def meeting_preview(
        meeting_id: int,
        force: Annotated[bool, Query()] = False,
        q: Annotated[str, Query()] = "full",
        v: Annotated[str | None, Query()] = None,  # cache-bust opcional
    ) -> FileResponse:
        """Vídeo + mic/desktop misturados (mp4)."""
        del v
        from ..audio import _normalize_quality

        quality = _normalize_quality(q)
        return _serve_listen_file(meeting_id, kind="preview", force=force, quality=quality)

    @app.get("/meetings/{meeting_id}/audio")
    def meeting_audio(
        meeting_id: int,
        force: Annotated[bool, Query()] = False,
    ) -> FileResponse:
        """Só o mix de áudio mic+desktop (.listen.m4a)."""
        return _serve_listen_file(meeting_id, kind="audio", force=force)

    @app.get("/files")
    def serve_local_file(path: Annotated[str, Query()]) -> FileResponse:
        """Serve arquivo local (só sob home do usuário) — play / download."""
        p = Path(path).expanduser().resolve()
        home = Path.home().resolve()
        try:
            p.relative_to(home)
        except ValueError as exc:
            raise HTTPException(403, "Acesso fora do home negado") from exc
        if not p.is_file():
            raise HTTPException(404, "Arquivo não encontrado")
        media = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        return FileResponse(p, media_type=media, filename=p.name)

    # ── Settings + Auth ───────────────────────────────────────────────────────

    @app.get("/api/settings")
    def api_get_settings() -> dict:
        """Retorna estado atual de configurações (sem segredos inteiros)."""
        settings = load_settings()
        tokens = load_tokens(settings)
        hf = settings.hf_token
        masked = (hf[:3] + "…" + hf[-4:]) if hf else None
        source = hf_token_source(settings) if hf else None
        return {
            "hf_token": {
                "configured": bool(hf),
                "masked": masked,
                "source": source,
            },
            "anthropic": {
                "connected": bool(tokens),
                "email": tokens.get("email") if tokens else None,
                "expires": tokens.get("expires") if tokens else None,
                "api_key_configured": bool(settings.anthropic_api_key),
            },
            "llm": {
                "provider": settings.llm_provider,
                "model": settings.llm_model,
            },
            "tuning": {
                "whisper_model": settings.whisper_model,
                "language": settings.language,
                "similarity_threshold": settings.similarity_threshold,
                "device": settings.device,
                "compute_type": settings.compute_type,
            },
        }

    @app.put("/api/settings/hf-token")
    def api_put_hf_token(body: HFTokenBody) -> dict:
        """Salva hf_token em settings.local.json."""
        if not body.token.strip():
            raise HTTPException(400, "Token vazio")
        settings = load_settings()
        save_local_settings({"hf_token": body.token.strip()}, settings)
        return {"ok": True}

    @app.delete("/api/settings/hf-token")
    def api_delete_hf_token() -> dict:
        """Remove override local do hf_token. 409 se a fonte é config.toml ou env."""
        settings = load_settings()
        src = hf_token_source(settings)
        if src in ("config", "env"):
            detail = (
                f"Token definido na fonte '{src}' — não pode ser removido via UI. "
                "Remova de ~/.config/meet/config.toml ou da variável de ambiente HF_TOKEN."
            )
            raise HTTPException(409, detail)
        save_local_settings({"hf_token": None}, settings)
        return {"ok": True}

    @app.put("/api/settings/llm")
    def api_put_llm(body: LLMSettingsBody) -> dict:
        """Atualiza provider e model em settings.local.json."""
        if body.provider not in _VALID_LLM_PROVIDERS:
            raise HTTPException(
                400,
                f"Provider inválido: {body.provider!r}. "
                f"Válidos: {sorted(_VALID_LLM_PROVIDERS)}",
            )
        settings = load_settings()
        save_local_settings({"llm_provider": body.provider, "llm_model": body.model}, settings)
        return {"ok": True}

    @app.put("/api/settings/tuning")
    def api_put_tuning(body: TuningBody) -> dict:
        """Atualiza parâmetros de transcrição/diarização em settings.local.json."""
        patch: dict = {}
        if body.similarity_threshold is not None:
            if not (0.0 <= body.similarity_threshold <= 1.0):
                raise HTTPException(400, "similarity_threshold deve estar em [0, 1]")
            patch["similarity_threshold"] = body.similarity_threshold
        if body.device is not None:
            if body.device not in {"cuda", "cpu"}:
                raise HTTPException(400, "device deve ser 'cuda' ou 'cpu'")
            patch["device"] = body.device
        if body.whisper_model is not None:
            if not body.whisper_model.strip():
                raise HTTPException(400, "whisper_model não pode ser vazio")
            patch["whisper_model"] = body.whisper_model.strip()
        if body.language is not None:
            patch["language"] = body.language
        if body.compute_type is not None:
            patch["compute_type"] = body.compute_type
        settings = load_settings()
        save_local_settings(patch, settings)
        return {"ok": True}

    @app.post("/api/auth/anthropic/authorize")
    def api_authorize() -> dict:
        """Gera URL de autorização OAuth com PKCE. Verifier fica em memória."""
        import os as _os

        state = _os.urandom(16).hex()
        verifier, challenge = generate_pkce()
        # Manter no máx _MAX_PENDING entradas; descartar a mais antiga.
        if len(_pending_verifiers) >= _MAX_PENDING:
            oldest = next(iter(_pending_verifiers))
            del _pending_verifiers[oldest]
        _pending_verifiers[state] = verifier
        url = build_authorize_url(state, challenge)
        return {"url": url, "state": state}

    @app.post("/api/auth/anthropic/exchange")
    def api_exchange(body: ExchangeBody) -> dict:
        """Troca código de autorização por tokens e persiste."""
        code = body.code.strip()
        state = body.state.strip()
        # code pode conter state como fragmento (#)
        if "#" in code:
            code, state = code.split("#", 1)
        verifier = _pending_verifiers.pop(state, None)
        if verifier is None:
            raise HTTPException(400, "State inválido ou expirado")
        try:
            d = exchange_code(code, state, verifier)
        except Exception as exc:
            raise HTTPException(400, f"Troca de código falhou: {exc}") from exc
        account = d.get("account") or {}
        now_ms = int(time.time() * 1000)
        tokens = {
            "access": d["access_token"],
            "refresh": d["refresh_token"],
            "expires": now_ms + d["expires_in"] * 1000 - 5 * 60 * 1000,
            "email": account.get("email_address"),
            "account_id": account.get("uuid"),
        }
        settings = load_settings()
        save_tokens(settings, tokens)
        # Ativar provider anthropic automaticamente após login.
        save_local_settings({"llm_provider": "anthropic"}, settings)
        return {"ok": True, "email": tokens["email"]}

    @app.delete("/api/auth/anthropic")
    def api_logout_anthropic() -> dict:
        """Remove tokens OAuth Anthropic."""
        settings = load_settings()
        clear_tokens(settings)
        return {"ok": True}

    # ── SPA (REGISTRAR POR ÚLTIMO) ────────────────────────────────────────────

    # /assets/ servido com StaticFiles se o build existir
    assets_dir = DIST_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def spa_index(full_path: str) -> FileResponse:
        index = DIST_DIR / "index.html"
        if not index.exists():
            raise HTTPException(
                503, "Frontend não compilado. Execute: cd frontend && bun run build"
            )
        # Serve arquivos raiz do dist (favicon.svg, etc.) diretamente,
        # bloqueando escapes do dist via ".." no path.
        if full_path:
            candidate = (DIST_DIR / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(DIST_DIR):
                media = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
                return FileResponse(str(candidate), media_type=media)
        return FileResponse(str(index))

    return app
