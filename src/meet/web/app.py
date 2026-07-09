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
        meetings = store.list_meetings()
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
    ) -> RedirectResponse:
        path = Path(video).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"Arquivo inválido: {video}")
        job = manager.submit(
            kind="process",
            label=path.name,
            video=str(path.resolve()),
            title=title.strip(),
            mic_track=mic_track,
            others_track=others_track,
            no_llm=no_llm in ("on", "true", "1", "yes"),
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

    @app.get("/meetings/{meeting_id}", response_class=HTMLResponse)
    def meeting_detail(request: Request, meeting_id: int) -> HTMLResponse:
        from ..audio import listen_mix_path

        settings, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        groups = _group_transcript(result.segments)
        pending = _pending_labels(settings, meeting_id)
        source = Path(result.source)
        source_exists = source.is_file()
        mix_ready = source_exists and listen_mix_path(source).is_file()
        return render(
            request,
            "meeting.html",
            meeting_id=meeting_id,
            m=result,
            groups=groups,
            pending=pending,
            source_exists=source_exists,
            mix_ready=mix_ready,
            md_path=getattr(result, "md_path", None),
        )

    @app.get("/meetings/{meeting_id}/audio")
    def meeting_audio(
        meeting_id: int,
        force: Annotated[bool, Query()] = False,
    ) -> FileResponse:
        """Serve o mix mic+desktop (gera .listen.m4a sob demanda se faltar)."""
        from ..audio import ensure_listen_mix, listen_mix_path

        _, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        source = Path(result.source)
        if not source.is_file():
            raise HTTPException(404, "Arquivo fonte não encontrado no disco")

        # Preferir cache em data_dir se a pasta da fonte não for gravável
        preferred = listen_mix_path(source)
        try:
            mix = ensure_listen_mix(source, force=force, output_path=preferred)
        except Exception:
            settings, _ = _settings_store()
            fallback = settings.data_dir / "listen" / f"{meeting_id}.m4a"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            try:
                mix = ensure_listen_mix(source, force=force, output_path=fallback)
            except Exception as exc:
                raise HTTPException(500, f"Falha ao gerar mix: {exc}") from exc

        return FileResponse(
            mix,
            media_type="audio/mp4",
            filename=mix.name,
            content_disposition_type="inline",  # <audio> embutido, não download
            headers={"Cache-Control": "private, max-age=3600"},
        )

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
