"""App FastAPI — UI web local do meet."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_settings
from ..store import Store
from .jobs import JobStatus, manager

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

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


def _fmt_duration(seconds: float) -> str:
    total = int(seconds or 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}:{s:02d}"


def _fmt_ts(seconds: float) -> str:
    total = int(seconds or 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _pending_labels(settings, meeting_id: int) -> list[str]:
    path = settings.data_dir / "pending" / f"{meeting_id}.npz"
    if not path.exists():
        return []
    import numpy as np

    data = np.load(str(path))
    return sorted(data.files)


def _group_transcript(segments):
    """Agrupa segmentos consecutivos do mesmo falante."""
    groups: list[dict] = []
    for seg in segments:
        if groups and groups[-1]["speaker"] == seg.speaker:
            groups[-1]["texts"].append(seg.text)
            groups[-1]["end"] = seg.end
        else:
            groups.append(
                {
                    "speaker": seg.speaker or "?",
                    "start": seg.start,
                    "end": seg.end,
                    "texts": [seg.text],
                }
            )
    for g in groups:
        g["text"] = " ".join(g["texts"])
        g["start_fmt"] = _fmt_ts(g["start"])
    return groups


def create_app() -> FastAPI:
    app = FastAPI(title="meet", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    TEMPLATES.env.globals["fmt_duration"] = _fmt_duration
    TEMPLATES.env.globals["fmt_ts"] = _fmt_ts
    TEMPLATES.env.globals["JobStatus"] = JobStatus

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, ctx)

    # ------------------------------------------------------------------
    # Páginas
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, q: str = "") -> HTMLResponse:
        settings, store = _settings_store()
        meetings = store.list_meeting_rows()
        results = store.search(q, limit=30) if q.strip() else []
        jobs = manager.list_recent(8)
        return render(
            request,
            "index.html",
            meetings=meetings,
            q=q,
            results=results,
            jobs=jobs,
            output_dir=str(settings.output_dir),
        )

    @app.get("/new", response_class=HTMLResponse)
    def new_meeting(
        request: Request,
        path: str = "",
    ) -> HTMLResponse:
        browse_path = Path(path).expanduser() if path else Path.home()
        if not browse_path.exists():
            browse_path = Path.home()
        if browse_path.is_file():
            browse_path = browse_path.parent

        entries: list[dict] = []
        try:
            kids = sorted(browse_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
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
                entries.append(
                    {
                        "name": p.name,
                        "path": str(p),
                        "kind": "file",
                        "size": size,
                        "size_h": _human_size(size),
                    }
                )

        parent = str(browse_path.parent) if browse_path != browse_path.parent else None
        quick = [str(d) for d in QUICK_DIRS if d.exists()]

        return render(
            request,
            "new.html",
            browse_path=str(browse_path),
            parent=parent,
            entries=entries,
            quick=quick,
            selected="",
        )

    @app.post("/process")
    def start_process(
        video: Annotated[str, Form()],
        title: Annotated[str, Form()] = "",
        mic_track: Annotated[int, Form()] = 1,
        others_track: Annotated[int, Form()] = 2,
        no_llm: Annotated[str, Form()] = "",
        import_media: Annotated[str, Form()] = "on",
    ) -> RedirectResponse:
        path = Path(video).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"Arquivo inválido: {video}")
        # checkbox: presente = on; ausente = não importar
        do_import = import_media in ("on", "true", "1", "yes")
        job = manager.submit(
            kind="process",
            label=path.name,
            video=str(path.resolve()),
            title=title.strip(),
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm in ("on", "true", "1", "yes"),
            import_media=do_import,
        )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> HTMLResponse:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Job não encontrado")
        return render(request, "job.html", job=job)

    @app.get("/jobs/{job_id}/status", response_class=HTMLResponse)
    def job_status_partial(request: Request, job_id: str) -> HTMLResponse:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Job não encontrado")
        return render(request, "partials/job_status.html", job=job)

    def _resolve_source(meeting_id: int) -> tuple[object, Path]:
        _, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        source = Path(result.source)
        if not source.is_file():
            raise HTTPException(
                404,
                "Vídeo ausente no disco. Use “Localizar vídeo” na página da reunião.",
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

    @app.get("/meetings/{meeting_id}", response_class=HTMLResponse)
    def meeting_detail(request: Request, meeting_id: int) -> HTMLResponse:
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
        pending = _pending_labels(settings, meeting_id)
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
            preview_ready = has_video and listen_preview_path(
                source, PREVIEW_WEB
            ).is_file()
            preview_full_ready = has_video and listen_preview_path(
                source, PREVIEW_FULL
            ).is_file()
            mix_ready = listen_mix_path(source).is_file()
        # Labels do seletor Plyr (size = altura aproximada)
        web_h = min(720, source_h) if source_h else 720
        full_h = source_h or 1080
        media_managed = bool(getattr(result, "media_managed", False))
        source_origin = getattr(result, "source_origin", result.source) or result.source
        return render(
            request,
            "meeting.html",
            meeting_id=meeting_id,
            m=result,
            groups=groups,
            pending=pending,
            source_exists=source_exists,
            has_video=has_video,
            preview_ready=preview_ready,
            preview_full_ready=preview_full_ready,
            mix_ready=mix_ready,
            source_w=source_w,
            source_h=source_h,
            quality_web_h=web_h,
            quality_full_h=full_h,
            md_path=getattr(result, "md_path", None),
            media_managed=media_managed,
            source_origin=source_origin,
        )

    @app.post("/meetings/{meeting_id}/edit")
    def edit_meeting(
        meeting_id: int,
        title: Annotated[str, Form()],
    ) -> RedirectResponse:
        from .. import render as render_mod

        settings, store = _settings_store()
        try:
            ok = store.update_title(meeting_id, title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not ok:
            raise HTTPException(404, "Reunião não encontrada")
        result = store.get_meeting(meeting_id)
        if result is not None:
            md_path = getattr(result, "md_path", None)
            if md_path:
                Path(md_path).write_text(
                    render_mod.to_markdown(result), encoding="utf-8"
                )
        return RedirectResponse(f"/meetings/{meeting_id}", status_code=303)

    @app.post("/meetings/{meeting_id}/delete")
    def delete_meeting(
        meeting_id: int,
        confirm: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        settings, store = _settings_store()
        if confirm not in ("on", "true", "1", "yes", "delete"):
            raise HTTPException(400, "Confirme a exclusão")
        if not store.delete_meeting(meeting_id, data_dir=settings.data_dir):
            raise HTTPException(404, "Reunião não encontrada")
        return RedirectResponse("/", status_code=303)

    @app.post("/meetings/bulk-delete")
    def bulk_delete_meetings(
        ids: Annotated[list[int], Form()] = [],
    ) -> RedirectResponse:
        """Exclui reuniões selecionadas na lista (checkboxes ``ids``)."""
        settings, store = _settings_store()
        # dedupe + só positivos
        clean = sorted({int(i) for i in ids if int(i) > 0})
        if not clean:
            raise HTTPException(400, "Nenhuma reunião selecionada")
        store.delete_meetings(clean, data_dir=settings.data_dir)
        return RedirectResponse("/", status_code=303)

    @app.post("/meetings/{meeting_id}/relink")
    def relink_meeting(
        meeting_id: int,
        path: Annotated[str, Form()],
        import_media: Annotated[str, Form()] = "on",
    ) -> RedirectResponse:
        settings, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        src = Path(path).expanduser()
        if not src.is_file():
            raise HTTPException(400, f"Arquivo inválido: {path}")
        if import_media in ("on", "true", "1", "yes"):
            store.adopt_media(meeting_id, settings.data_dir, src)
        else:
            store.set_media(
                meeting_id,
                source=src.resolve(),
                source_origin=str(src.resolve()),
                media_managed=False,
            )
        return RedirectResponse(f"/meetings/{meeting_id}", status_code=303)

    @app.get("/meetings/{meeting_id}/preview")
    def meeting_preview(
        meeting_id: int,
        force: Annotated[bool, Query()] = False,
        q: Annotated[str, Query()] = "full",
        v: Annotated[str | None, Query()] = None,  # cache-bust opcional
    ) -> FileResponse:
        """Vídeo + mic/desktop misturados (mp4).

        ``q=full`` (default): resolução original, re-encoded p/ browser.
        ``q=web``: ≤1280px, mais leve.
        """
        del v
        from ..audio import _normalize_quality

        quality = _normalize_quality(q)
        return _serve_listen_file(
            meeting_id, kind="preview", force=force, quality=quality
        )

    @app.get("/meetings/{meeting_id}/audio")
    def meeting_audio(
        meeting_id: int,
        force: Annotated[bool, Query()] = False,
    ) -> FileResponse:
        """Só o mix de áudio mic+desktop (.listen.m4a)."""
        return _serve_listen_file(meeting_id, kind="audio", force=force)

    @app.post("/meetings/{meeting_id}/assign")
    def assign_speaker(
        meeting_id: int,
        label: Annotated[str, Form()],
        name: Annotated[str, Form()],
    ) -> RedirectResponse:
        import numpy as np

        from .. import render as render_mod
        from .. import voicebank as voicebank_mod

        settings, store = _settings_store()
        name = name.strip()
        if not name:
            raise HTTPException(400, "Nome vazio")

        pending_path = settings.data_dir / "pending" / f"{meeting_id}.npz"
        if pending_path.exists():
            data = np.load(str(pending_path))
            if label in data:
                voicebank_mod.enroll(name, data[label], store)

        store.update_speaker(meeting_id, label, name)
        result = store.get_meeting(meeting_id)
        if result is not None:
            md_path = getattr(result, "md_path", None)
            if md_path:
                Path(md_path).write_text(render_mod.to_markdown(result), encoding="utf-8")

        return RedirectResponse(f"/meetings/{meeting_id}", status_code=303)

    @app.post("/meetings/{meeting_id}/mix")
    def mix_meeting(meeting_id: int) -> RedirectResponse:
        _, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        path = Path(result.source)
        if not path.is_file():
            raise HTTPException(400, "Arquivo fonte não encontrado no disco")
        job = manager.submit(kind="mix", label=f"mix · {path.name}", video=str(path))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/speakers", response_class=HTMLResponse)
    def speakers_page(request: Request) -> HTMLResponse:
        _, store = _settings_store()
        voices = store.all_voices()
        rows = [
            {"name": n, "dims": len(blob) // 4}
            for n, blob in sorted(voices.items())
        ]
        return render(request, "speakers.html", voices=rows)

    @app.post("/speakers/{name}/rm")
    def speakers_rm(name: str) -> RedirectResponse:
        _, store = _settings_store()
        store.delete_voice(name)
        return RedirectResponse("/speakers", status_code=303)

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

    return app


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
