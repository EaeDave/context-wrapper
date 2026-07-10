"""App FastAPI — API JSON + SPA (React)."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from pathlib import Path
from typing import Annotated, AsyncGenerator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import load_settings
from ..store import Store
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

    Retorna start/end numéricos + text concatenado; sem start_fmt nem texts.
    """
    groups: list[dict] = []
    for seg in segments:
        if groups and groups[-1]["speaker"] == seg.speaker:
            groups[-1]["_texts"].append(seg.text)
            groups[-1]["end"] = seg.end
        else:
            groups.append(
                {
                    "speaker": seg.speaker or "?",
                    "start": seg.start,
                    "end": seg.end,
                    "_texts": [seg.text],
                }
            )
    return [
        {
            "speaker": g["speaker"],
            "start": g["start"],
            "end": g["end"],
            "text": " ".join(g["_texts"]),
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


class BulkDeleteBody(BaseModel):
    ids: list[int]


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
                    "what": ai.what,
                    "where": ai.where,
                    "details": ai.details,
                    "requested_by": ai.requested_by,
                    "priority": ai.priority,
                }
                for ai in result.action_items
            ],
            "pending": pending,
            "groups": groups,
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
        return [
            {"name": n, "dims": len(blob) // 4}
            for n, blob in sorted(voices.items())
        ]

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
