"""App FastAPI — API JSON + SPA (React)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Annotated, AsyncGenerator, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import CONFIG_PATH, hf_token_source, load_settings, save_local_settings
from ..progress import ProgressUpdate
from ..model_catalog import get_model_catalog
from ..store import Store
from ..anthropic_oauth import (
    build_authorize_url,
    clear_tokens,
    exchange_code,
    generate_pkce,
    load_tokens,
    save_tokens,
)
from .. import openai_oauth as _oai
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

# Hosts loopback aceitos (mitiga DNS rebinding). MEET_ALLOW_REMOTE=1 relaxa.
# testserver = Starlette/FastAPI TestClient (não é vetor de rebinding real)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver"})
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Store por db_path — schema/migrate só no primeiro open, não a cada request.
_store_cache: dict[str, Store] = {}
_store_lock = threading.Lock()


def _get_store(settings) -> Store:
    key = str(Path(settings.db_path).expanduser().resolve())
    with _store_lock:
        store = _store_cache.get(key)
        if store is None:
            store = Store(settings.db_path)
            _store_cache[key] = store
        return store


def _settings_store():
    settings = load_settings()
    return settings, _get_store(settings)


def _allow_remote() -> bool:
    return os.environ.get("MEET_ALLOW_REMOTE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _normalize_host(host_header: str) -> str:
    host = (host_header or "").split(",")[0].strip().lower()
    if not host:
        return ""
    # IPv6 com porta: [2001:db8::1]:8741
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[1:end]
        return host.strip("[]")
    # host:port
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _host_is_allowed(host_header: str) -> bool:
    if _allow_remote():
        return True
    host = _normalize_host(host_header)
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    # *.localhost (RFC 6761)
    return host.endswith(".localhost")


def _peer_is_loopback(request: Request) -> bool:
    """Exige que o socket peer seja loopback (não só o header Host)."""
    if _allow_remote():
        return True
    client = request.client
    if client is None:
        # ASGI sem client (alguns testes) — Host loopback já foi checado.
        return True
    host = (client.host or "").strip().lower()
    # Starlette TestClient usa "testclient"
    if host in {"testclient", "testserver", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_host(origin: str) -> str | None:
    try:
        parsed = urlparse(origin)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return _normalize_host(parsed.netloc)


def _path_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _sensitive_roots(settings) -> list[Path]:
    roots = [
        Path(settings.data_dir).expanduser().resolve(),
        CONFIG_PATH.parent.resolve(),
    ]
    # data_dir padrão se settings apontar outro lugar — ainda bloquear o default
    default = (Path.home() / ".local" / "share" / "meet").resolve()
    if default not in roots:
        roots.append(default)
    return roots


def _assert_user_media_path(path: str, *, for_browse: bool = False) -> Path:
    """Resolve path sob $HOME e fora de data_dir/config (tokens, db, settings)."""
    settings = load_settings()
    raw = Path(path).expanduser() if path else Path.home()
    try:
        p = raw.resolve()
    except OSError as exc:
        raise HTTPException(400, "Caminho inválido") from exc

    home = Path.home().resolve()
    if not _path_under(p, home) and p != home:
        raise HTTPException(403, "Acesso fora do home negado")

    for root in _sensitive_roots(settings):
        if p == root or _path_under(p, root):
            raise HTTPException(403, "Acesso a dados internos do meet negado")

    # Arquivos sensíveis mesmo se data_dir foi customizado para fora do default
    if not for_browse and p.is_file():
        name = p.name.lower()
        if name in {"auth.json", "settings.local.json", "meet.db", "config.toml"}:
            raise HTTPException(403, "Acesso a dados internos do meet negado")
        if name.endswith(".db") and "meet" in name:
            raise HTTPException(403, "Acesso a dados internos do meet negado")

    return p


class _HostOriginMiddleware(BaseHTTPMiddleware):
    """Rejeita Host/peer fora do loopback e Origin cruzado em mutações."""

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if not _host_is_allowed(host):
            return JSONResponse(
                {"detail": "Host não permitido (use loopback ou MEET_ALLOW_REMOTE=1)"},
                status_code=403,
            )
        # Host é spoofable; exige peer loopback quando remoto não está liberado.
        if not _peer_is_loopback(request):
            return JSONResponse(
                {
                    "detail": "Conexão não-loopback negada "
                    "(use 127.0.0.1 ou MEET_ALLOW_REMOTE=1)"
                },
                status_code=403,
            )
        if request.method in _MUTATING:
            origin = request.headers.get("origin")
            if origin:
                o_host = _origin_host(origin)
                req_host = _normalize_host(host)
                if o_host is None or (
                    o_host != req_host
                    and o_host not in _LOOPBACK_HOSTS
                    and not (req_host in _LOOPBACK_HOSTS and o_host in _LOOPBACK_HOSTS)
                ):
                    return JSONResponse(
                        {"detail": "Origin não permitido"},
                        status_code=403,
                    )
        return await call_next(request)


def _validate_speaker_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(400, "Nome vazio")
    if any(c in cleaned for c in ("/", "\\", "\0")):
        raise HTTPException(400, "Nome de falante não pode conter / ou \\")
    if len(cleaned) > 120:
        raise HTTPException(400, "Nome de falante muito longo")
    return cleaned


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
    Inclui original_text (só quando algum segmento foi corrigido) e corrections
    agregadas para auditoria.
    """
    groups: list[dict] = []
    for seg in segments:
        if groups and groups[-1]["speaker"] == seg.speaker:
            groups[-1]["_texts"].append(seg.text)
            # Para o original concatenado: usar original_text se houve correção,
            # senão o texto atual (semântica: "o que o Whisper entregou").
            groups[-1]["_originals"].append(seg.original_text if seg.original_text else seg.text)
            if seg.original_text:
                groups[-1]["_has_original"] = True
            groups[-1]["_corrections"].extend(seg.corrections)
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
                    "_originals": [seg.original_text if seg.original_text else seg.text],
                    "_has_original": bool(seg.original_text),
                    "_corrections": list(seg.corrections),
                    "seg_ids": seg_ids,
                }
            )
    result = []
    for g in groups:
        entry: dict = {
            "speaker": g["speaker"],
            "start": g["start"],
            "end": g["end"],
            "text": " ".join(g["_texts"]),
            "seg_ids": g["seg_ids"],
        }
        if g["_has_original"]:
            entry["original_text"] = " ".join(g["_originals"])
        if g["_corrections"]:
            entry["corrections"] = [
                {
                    "original": c.original,
                    "corrected": c.corrected,
                    "confidence": c.confidence,
                    "reason": c.reason,
                }
                for c in g["_corrections"]
            ]
        result.append(entry)
    return result


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
        "progress": job.progress.to_dict() if job.progress is not None else None,
    }


def _highlight_snippet(raw: str) -> str:
    """HTML-escape o snippet FTS e re-aplica <mark> só nos hits (marcadores […])."""
    import html

    # FTS5 snippet() usa '[', ']' em volta dos termos; escapar o resto evita XSS.
    escaped = html.escape(raw, quote=True)
    return re.sub(r"\[([^\]]*)\]", r"<mark>\1</mark>", escaped)


# ── Pydantic request bodies ──────────────────────────────────────────────────


class PatchMeetingBody(BaseModel):
    title: str | None = None
    project_id: int | None = None  # usar model_fields_set p/ distinguir "ausente" de "null"

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
    analyze_visual: bool = False
    import_media: bool = True
    num_speakers: int = 0
    project_id: int | None = None


class BulkDeleteBody(BaseModel):
    ids: list[int]


class BulkProjectBody(BaseModel):
    ids: list[int]
    project_id: int | None = None  # None = desassociar


class CreateProjectBody(BaseModel):
    name: str
    description: str = ""
    repo_path: str = ""


class PatchProjectBody(BaseModel):
    name: str | None = None
    description: str | None = None
    repo_path: str | None = None

class HFTokenBody(BaseModel):
    token: str


class LLMSettingsBody(BaseModel):
    provider: str
    model: str


class ExchangeBody(BaseModel):
    code: str
    state: str



class OpenAIExchangeBody(BaseModel):
    state: str


class PatchActionItemBody(BaseModel):
    what: str | None = None
    where: str | None = None
    details: str | None = None
    requested_by: str | None = None
    priority: Literal["alta", "media", "baixa"] | None = None
    status: Literal["aberto", "feito"] | None = None
    due: str | None = None
    assigned_to: list[str] | None = None
    source_start: float | None = None
    source_end: float | None = None
    evidence_quote: str | None = None
    explicitness: Literal["explicit", "inferred"] | None = None
    review_status: Literal["confirmed", "needs_review"] | None = None


class AddActionItemBody(BaseModel):
    what: str
    where: str | None = None
    details: str | None = None
    requested_by: str | None = None
    priority: Literal["alta", "media", "baixa"] = "media"
    assigned_to: list[str] | None = None
    source_start: float | None = None
    source_end: float | None = None
    evidence_quote: str | None = None
    explicitness: Literal["explicit", "inferred"] = "inferred"
    review_status: Literal["confirmed", "needs_review"] = "needs_review"

class PatchMeetingFactBody(BaseModel):
    text: str | None = None
    kind: Literal["decision", "requirement", "constraint", "open_question"] | None = None
    source_start: float | None = None
    source_end: float | None = None
    evidence_quote: str | None = None
    explicitness: Literal["explicit", "inferred"] | None = None
    review_status: Literal["confirmed", "needs_review"] | None = None


class PatchTurnBody(BaseModel):
    seg_ids: list[int]
    text: str | None = None
    speaker: str | None = None


class ReprocessBody(BaseModel):
    mic_track: int = 1
    others_track: int = 2
    no_llm: bool = False
    analyze_visual: bool = False
    num_speakers: int = 0



class RenameSpeakerBody(BaseModel):
    name: str
    new_name: str


class TuningBody(BaseModel):
    whisper_model: str | None = None
    language: str | None = None
    similarity_threshold: float | None = None
    device: str | None = None
    compute_type: str | None = None


class TestConnectionBody(BaseModel):
    target: str


class ContextExportBody(BaseModel):
    task_ids: list[int]
    objective: str = ""
    format: Literal["markdown", "json"] = "markdown"
    include_summary: bool = True
    include_facts: bool = True
    include_evidence: bool = True
    include_transcript: bool = False

# Verifiers PKCE pendentes: {state → (verifier, created_monotonic)}. Limpo após uso.
_pending_verifiers: dict[str, tuple[str, float]] = {}
_pending_auth_lock = threading.Lock()
_MAX_PENDING = 5
_PENDING_TTL_S = 15 * 60
_VALID_LLM_PROVIDERS = frozenset({"claude-code", "anthropic", "openai", "ollama"})
# Device-code flows OpenAI: {state → {..., created: monotonic}}
_pending_openai: dict[str, dict] = {}
_MAX_PENDING_OPENAI = 5


def _purge_expired_pending(now: float | None = None) -> None:
    """Remove states OAuth expirados (caller deve segurar _pending_auth_lock)."""
    t = time.monotonic() if now is None else now
    dead_v = [k for k, (_, created) in _pending_verifiers.items() if t - created > _PENDING_TTL_S]
    for k in dead_v:
        del _pending_verifiers[k]
    dead_o = [
        k
        for k, v in _pending_openai.items()
        if t - float(v.get("created", 0)) > _PENDING_TTL_S
    ]
    for k in dead_o:
        del _pending_openai[k]

# ── App ──────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(title="meet", docs_url=None, redoc_url=None)
    app.add_middleware(_HostOriginMiddleware)

    # ── Meetings ──────────────────────────────────────────────────────────────

    def _parse_project_filter(project_id: str | None) -> "int | str | None":
        if project_id is None:
            return None
        if project_id.lower() == "none":
            return "none"
        try:
            return int(project_id)
        except ValueError:
            raise HTTPException(400, f"project_id inválido: {project_id!r}")

    @app.get("/api/meetings")
    def api_list_meetings(project_id: str | None = None) -> list[dict]:
        _, store = _settings_store()
        pf = _parse_project_filter(project_id)
        rows = store.list_meeting_rows(project_filter=pf)
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
                "project_id": r.project_id,
                "project_name": r.project_name,
            }
            for r in rows
        ]

    @app.get("/api/search")
    def api_search(q: str = "", project_id: str | None = None) -> list[dict]:
        if not q.strip():
            return []
        _, store = _settings_store()
        pf = _parse_project_filter(project_id)
        results = store.search(q, limit=30, project_filter=pf)
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

    # Registrar bulk-project e bulk-delete ANTES de /{meeting_id} para evitar conflito de rota
    @app.patch("/api/meetings/bulk-project")
    def api_bulk_project(body: BulkProjectBody) -> dict:
        clean = sorted({i for i in body.ids if i > 0})
        if not clean:
            raise HTTPException(400, "Nenhuma reunião selecionada")
        _, store = _settings_store()
        try:
            count = store.bulk_set_meeting_project(clean, body.project_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"updated": count}

    # Registrar bulk-delete ANTES de /{meeting_id} para evitar conflito de rota
    @app.post("/api/meetings/bulk-delete")
    def api_bulk_delete(body: BulkDeleteBody) -> dict:
        clean = sorted({i for i in body.ids if i > 0})
        if not clean:
            raise HTTPException(400, "Nenhuma reunião selecionada")
        settings, store = _settings_store()
        deleted = store.delete_meetings(clean, data_dir=settings.data_dir)
        return {"deleted": deleted}

    def _visual_to_dict(meeting_id: int, evidence) -> dict:
        return {
            "id": evidence.id,
            "timestamp": evidence.timestamp,
            "thumbnail_url": f"/api/meetings/{meeting_id}/visual-evidence/{evidence.id}",
            "description": evidence.description,
            "visible_text": evidence.visible_text,
            "relevance": evidence.relevance,
        }

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
        media_managed = bool(result.media_managed)
        source_origin = result.source_origin or result.source
        md_path = result.md_path
        project = store.get_project(result.project_id) if result.project_id is not None else None

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
                    "assigned_to": ai.assigned_to,
                    "source_start": ai.source_start,
                    "source_end": ai.source_end,
                    "evidence_quote": ai.evidence_quote,
                    "explicitness": ai.explicitness,
                    "review_status": ai.review_status,
                    "visual_evidence": [
                        _visual_to_dict(meeting_id, evidence)
                        for evidence in ai.visual_evidence
                    ],
                }
                for ai in result.action_items
            ],
            "facts": [
                {
                    "id": f.id,
                    "kind": f.kind,
                    "text": f.text,
                    "source_start": f.source_start,
                    "source_end": f.source_end,
                    "evidence_quote": f.evidence_quote,
                    "explicitness": f.explicitness,
                    "review_status": f.review_status,
                    "visual_evidence": [
                        _visual_to_dict(meeting_id, evidence)
                        for evidence in f.visual_evidence
                    ],
                }
                for f in result.facts
            ],
            "visual_evidence": [
                _visual_to_dict(meeting_id, evidence)
                for evidence in result.visual_evidence
            ],
            "pending": pending,
            "groups": groups,
            "speaker_matches": result.speaker_matches,
            "project_id": result.project_id,
            "project_name": project.name if project else None,
        }

    @app.get("/api/meetings/{meeting_id}/markdown")
    def api_meeting_markdown(meeting_id: int) -> FileResponse:
        """Regenera e baixa o documento canônico a partir do estado atual do banco."""
        _settings, store = _settings_store()
        result = store.get_meeting(meeting_id)
        if result is None:
            raise HTTPException(404, "Reunião não encontrada")
        md_path = getattr(result, "md_path", None)
        if not md_path:
            raise HTTPException(404, "Reunião sem arquivo Markdown associado")
        store._regen_md(meeting_id)
        path = Path(md_path)
        if not path.is_file():
            raise HTTPException(404, "Arquivo Markdown não encontrado")
        return FileResponse(path, media_type="text/markdown", filename=path.name)

    @app.get("/api/meetings/{meeting_id}/visual-evidence/{evidence_id}")
    def api_visual_evidence(meeting_id: int, evidence_id: int) -> FileResponse:
        settings, store = _settings_store()
        row = store._conn.execute(
            "SELECT image_path FROM visual_evidence WHERE id = ? AND meeting_id = ?",
            (evidence_id, meeting_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Evidência visual não encontrada")
        path = Path(row["image_path"]).resolve()
        allowed = (settings.data_dir / "media" / str(meeting_id) / "visual").resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise HTTPException(404, "Arquivo da evidência visual não encontrado")
        return FileResponse(path, media_type="image/jpeg")

    @app.patch("/api/meetings/{meeting_id}")
    def api_patch_meeting(meeting_id: int, body: PatchMeetingBody) -> dict:
        from .. import render as render_mod

        has_title = body.title is not None
        has_project = "project_id" in body.model_fields_set
        if not has_title and not has_project:
            raise HTTPException(400, "Forneça title ou project_id")
        settings, store = _settings_store()
        if has_title:
            t = (body.title or "").strip()
            if not t:
                raise HTTPException(400, "Título vazio")
            try:
                ok = store.update_title(meeting_id, t)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if not ok:
                raise HTTPException(404, "Reunião não encontrada")
        if has_project:
            try:
                ok = store.set_meeting_project(meeting_id, body.project_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            if not ok:
                raise HTTPException(404, "Reunião não encontrada")
        result = store.get_meeting(meeting_id)
        if result is not None:
            md_path = result.md_path
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
        src = _assert_user_media_path(body.path)
        if not src.is_file():
            raise HTTPException(400, f"Arquivo inválido: {body.path}")
        if body.import_media:
            store.adopt_media(meeting_id, settings.data_dir, src)
        else:
            store.set_media(
                meeting_id,
                source=src,
                source_origin=str(src),
                media_managed=False,
            )
        return {"ok": True}

    @app.post("/api/meetings/{meeting_id}/assign")
    def api_assign(meeting_id: int, body: AssignBody) -> dict:
        import numpy as np

        from .. import render as render_mod
        from .. import voicebank as voicebank_mod

        settings, store = _settings_store()
        if store.get_meeting(meeting_id) is None:
            raise HTTPException(404, "Reunião não encontrada")
        name = _validate_speaker_name(body.name)

        pending_path = settings.data_dir / "pending" / f"{meeting_id}.npz"
        if pending_path.exists():
            data = np.load(str(pending_path))
            if body.label in data:
                voicebank_mod.enroll(name, data[body.label], store)

        store.update_speaker(meeting_id, body.label, name)
        result = store.get_meeting(meeting_id)
        if result is not None:
            md_path = result.md_path
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


    @app.patch("/api/action-items/{item_id}")
    def api_patch_action_item(item_id: int, body: PatchActionItemBody) -> dict:
        _, store = _settings_store()
        fields = body.model_dump(exclude_unset=True)
        if not store.update_action_item(item_id, fields):
            raise HTTPException(404, "Action item não encontrado")
        return {"ok": True}

    @app.patch("/api/meeting-facts/{fact_id}")
    def api_patch_meeting_fact(fact_id: int, body: PatchMeetingFactBody) -> dict:
        _, store = _settings_store()
        fields = body.model_dump(exclude_unset=True)
        if not store.update_meeting_fact(fact_id, fields):
            raise HTTPException(404, "Fato não encontrado")
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
            assigned_to=body.assigned_to,
            source_start=body.source_start,
            source_end=body.source_end,
            evidence_quote=body.evidence_quote,
            explicitness=body.explicitness,
            review_status=body.review_status,
        )
        new_id = store.add_action_item(meeting_id, item)
        return {"id": new_id}

    @app.delete("/api/action-items/{item_id}", status_code=204)
    def api_delete_action_item(item_id: int) -> None:
        _, store = _settings_store()
        if not store.delete_action_item(item_id):
            raise HTTPException(404, "Action item não encontrado")

    @app.get("/api/tasks")
    def api_tasks(
        status: str = "aberto",
        project_id: str | None = None,
        scope: str = "personal",
    ) -> list[dict]:
        if status not in {"aberto", "feito", "todos"}:
            raise HTTPException(400, "status deve ser aberto|feito|todos")
        if scope not in {"personal", "delegated", "all"}:
            raise HTTPException(400, "scope deve ser personal|delegated|all")
        _, store = _settings_store()
        pf = _parse_project_filter(project_id)
        return store.list_tasks(status, project_filter=pf, scope=scope)

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
            analyze_visual=body.analyze_visual,
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
        path = _assert_user_media_path(body.video)
        if not path.is_file():
            raise HTTPException(400, f"Arquivo inválido: {body.video}")
        if body.project_id is not None:
            _, store = _settings_store()
            if store.get_project(body.project_id) is None:
                raise HTTPException(404, "Projeto não encontrado")
        job = manager.submit(
            kind="process",
            label=path.name,
            video=str(path),
            title=body.title.strip(),
            mic_track=body.mic_track,
            others_track=body.others_track,
            no_llm=body.no_llm,
            analyze_visual=body.analyze_visual,
            import_media=body.import_media,
            num_speakers=body.num_speakers,
            project_id=body.project_id,
        )
        return _serialize_job(job)

    # ── Projetos ──────────────────────────────────────────────────────────────

    def _project_to_dict(p) -> dict:  # p: ProjectRow
        return {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "repo_path": p.repo_path,
            "meeting_count": p.meeting_count,
            "open_task_count": p.open_task_count,
            "done_task_count": p.done_task_count,
            "last_meeting_date": p.last_meeting_date,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }

    @app.get("/api/projects")
    def api_list_projects() -> list[dict]:
        _, store = _settings_store()
        return [_project_to_dict(p) for p in store.list_projects()]

    @app.post("/api/projects", status_code=201)
    def api_create_project(body: CreateProjectBody) -> dict:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Nome do projeto não pode ser vazio")
        _, store = _settings_store()
        try:
            pid = store.create_project(name, body.description, body.repo_path)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        proj = store.get_project(pid)
        assert proj is not None
        return _project_to_dict(proj)

    @app.get("/api/projects/{project_id}")
    def api_get_project(project_id: int) -> dict:
        _, store = _settings_store()
        proj = store.get_project(project_id)
        if proj is None:
            raise HTTPException(404, "Projeto não encontrado")
        return _project_to_dict(proj)

    @app.patch("/api/projects/{project_id}")
    def api_patch_project(project_id: int, body: PatchProjectBody) -> dict:
        if body.name is None and body.description is None and body.repo_path is None:
            raise HTTPException(400, "Forneça pelo menos um campo para atualizar")
        _, store = _settings_store()
        try:
            ok = store.update_project(
                project_id,
                name=body.name,
                description=body.description,
                repo_path=body.repo_path,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not ok:
            raise HTTPException(404, "Projeto não encontrado")
        proj = store.get_project(project_id)
        assert proj is not None
        return _project_to_dict(proj)

    @app.delete("/api/projects/{project_id}", status_code=204)
    def api_delete_project(project_id: int) -> None:
        _, store = _settings_store()
        if not store.delete_project(project_id):
            raise HTTPException(404, "Projeto não encontrado")


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

    @app.post("/api/jobs/{job_id}/retry")
    def api_job_retry(job_id: str) -> dict:
        """Reenvia job terminal (done/error) com os mesmos params."""
        job = manager.retry(job_id)
        if job is None:
            existing = manager.get(job_id)
            if existing is None:
                raise HTTPException(404, "Job não encontrado")
            raise HTTPException(
                400,
                "Só é possível repetir jobs concluídos ou com erro",
            )
        return _serialize_job(job)

    @app.get("/api/jobs/{job_id}/events")
    async def api_job_events(job_id: str) -> StreamingResponse:
        if manager.get(job_id) is None:
            raise HTTPException(404, "Job não encontrado")

        async def event_stream() -> AsyncGenerator[str, None]:
            last_status: JobStatus | None = None
            last_stage: str | None = None
            last_error: str | None = None
            last_progress: ProgressUpdate | None = None

            while True:
                job = manager.get(job_id)
                if job is None:
                    break

                changed = (
                    job.status != last_status
                    or job.stage != last_stage
                    or job.error != last_error
                    or job.progress != last_progress
                )
                if changed:
                    last_status = job.status
                    last_stage = job.stage
                    last_error = job.error
                    last_progress = job.progress
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
        settings = load_settings()
        try:
            browse_path = _assert_user_media_path(path or str(Path.home()), for_browse=True)
        except HTTPException:
            browse_path = Path.home().resolve()
        if not browse_path.exists():
            browse_path = Path.home().resolve()
        if browse_path.is_file():
            browse_path = browse_path.parent

        sensitive = _sensitive_roots(settings)
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
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if any(resolved == root or _path_under(resolved, root) for root in sensitive):
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
        # Parent só se ainda sob home e fora de sensitive
        if parent:
            try:
                _assert_user_media_path(parent, for_browse=True)
            except HTTPException:
                parent = None
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

        p = _assert_user_media_path(path)
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
        meetings_by_name = store.speaker_meeting_counts()
        return [
            {"name": n, "dims": len(blob) // 4, "meetings": meetings_by_name.get(n, 0)}
            for n, blob in sorted(voices.items())
        ]

    @app.patch("/api/speakers")
    def api_rename_speaker(body: RenameSpeakerBody) -> dict:
        new_name = _validate_speaker_name(body.new_name)
        _, store = _settings_store()
        store.rename_voice(body.name, new_name)
        return {"ok": True}

    @app.get("/api/speakers/usage")
    def api_speaker_usage(name: str = Query(...)) -> list[dict]:
        _, store = _settings_store()
        return store.voice_usage(name)

    @app.delete("/api/speakers", status_code=204)
    def api_delete_speaker(name: str = Query(...)) -> None:
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
        """Serve preview/mix em cache; encode síncrono só com force=1.

        Sem force, ausência de cache → 409 (use POST mix job). Evita travar a
        API em ffmpeg multi-minuto no path GET.
        """
        from ..audio import (
            PREVIEW_FULL,
            PREVIEW_WEB,
            _cache_is_fresh,
            _preview_is_browser_safe,
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

        def _not_ready(what: str) -> None:
            raise HTTPException(
                409,
                f"{what} ainda não gerado. Dispare o job de mix "
                f"(POST /api/meetings/{meeting_id}/mix) ou use force=1.",
            )

        if kind == "preview":
            has_video = probe_video_streams(source) >= 1
            if not has_video:
                kind = "audio"
            else:
                q = quality if quality in (PREVIEW_WEB, PREVIEW_FULL) else PREVIEW_WEB
                preferred = listen_preview_path(source, q)
                suffix = "full.mp4" if q == PREVIEW_FULL else "mp4"
                fallback = listen_dir / f"{meeting_id}.listen.{suffix}"
                if not force:
                    for candidate in (preferred, fallback):
                        if _cache_is_fresh(candidate, source) and _preview_is_browser_safe(
                            candidate, q
                        ):
                            return FileResponse(
                                candidate,
                                media_type="video/mp4",
                                filename=candidate.name,
                                content_disposition_type="inline",
                                headers={"Cache-Control": "private, max-age=3600"},
                            )
                    _not_ready("Preview")
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
        if not force:
            for candidate in (preferred, fallback):
                if _cache_is_fresh(candidate, source):
                    return FileResponse(
                        candidate,
                        media_type="audio/mp4",
                        filename=candidate.name,
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=3600"},
                    )
            _not_ready("Mix de áudio")
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
        """Vídeo + mic/desktop misturados (mp4).

        GET nunca re-encoda à força (evita CSRF/DoS via force=true cross-origin).
        Gera só se o cache ainda não existir. Force: POST .../rebuild.
        """
        del v
        if force:
            raise HTTPException(
                405,
                "force=true não permitido em GET; use POST "
                f"/meetings/{meeting_id}/preview/rebuild",
            )
        from ..audio import _normalize_quality

        quality = _normalize_quality(q)
        return _serve_listen_file(
            meeting_id, kind="preview", force=False, quality=quality
        )

    @app.post("/meetings/{meeting_id}/preview/rebuild")
    def meeting_preview_rebuild(
        meeting_id: int,
        q: Annotated[str, Query()] = "full",
    ) -> FileResponse:
        """Re-gera preview (ffmpeg) — método mutável, coberto pelo Origin check."""
        from ..audio import _normalize_quality

        quality = _normalize_quality(q)
        return _serve_listen_file(
            meeting_id, kind="preview", force=True, quality=quality
        )

    @app.get("/meetings/{meeting_id}/audio")
    def meeting_audio(
        meeting_id: int,
        force: Annotated[bool, Query()] = False,
    ) -> FileResponse:
        """Só o mix de áudio mic+desktop (.listen.m4a). GET sem force."""
        if force:
            raise HTTPException(
                405,
                "force=true não permitido em GET; use POST "
                f"/meetings/{meeting_id}/audio/rebuild",
            )
        return _serve_listen_file(meeting_id, kind="audio", force=False)

    @app.post("/meetings/{meeting_id}/audio/rebuild")
    def meeting_audio_rebuild(meeting_id: int) -> FileResponse:
        """Re-gera mix de áudio (ffmpeg) — mutável, Origin-guarded."""
        return _serve_listen_file(meeting_id, kind="audio", force=True)

    @app.get("/files")
    def serve_local_file(path: Annotated[str, Query()]) -> FileResponse:
        """Serve mídia local (extensões MEDIA_EXTS; sob home; nunca data_dir/config)."""
        p = _assert_user_media_path(path)
        if not p.is_file():
            raise HTTPException(404, "Arquivo não encontrado")
        if p.suffix.lower() not in MEDIA_EXTS:
            raise HTTPException(
                403, f"Tipo de arquivo não permitido (aceitos: {', '.join(sorted(MEDIA_EXTS))})"
            )
        media = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        return FileResponse(p, media_type=media, filename=p.name)

    # ── Settings + Auth ───────────────────────────────────────────────────────

    @app.get("/api/settings")
    def api_get_settings() -> dict:
        """Retorna estado atual de configurações (sem segredos inteiros)."""
        settings = load_settings()
        tokens = load_tokens(settings)
        oai_tokens = _oai.load_tokens(settings)
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
            "openai": {
                "connected": bool(oai_tokens),
                "email": oai_tokens.get("email") if oai_tokens else None,
                "expires": oai_tokens.get("expires") if oai_tokens else None,
                "plan": oai_tokens.get("plan") if oai_tokens else None,
                "api_key_configured": bool(settings.openai_api_key),
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

    @app.get("/api/settings/models")
    def api_get_llm_models(provider: str = Query(...)) -> dict:
        """Lista modelos selecionáveis, com descoberta específica por provider."""
        settings = load_settings()
        try:
            return get_model_catalog(settings, provider)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

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

    @app.post("/api/settings/test")
    def api_test_connection(body: TestConnectionBody) -> dict:
        """Testa conectividade por provider. Nunca vaza tokens; nunca retorna 500."""
        import httpx

        target = body.target.strip().lower()
        settings = load_settings()

        if target == "anthropic":
            try:
                from ..anthropic_oauth import get_access_token, load_tokens
                get_access_token(settings)
                tokens = load_tokens(settings)
                email = (tokens or {}).get("email")
                detail = f"Conectado ({email})" if email else "Conectado"
                return {"ok": True, "detail": detail}
            except Exception as exc:
                return {"ok": False, "detail": str(exc)}

        if target == "hf":
            hf_token = settings.hf_token
            if not hf_token:
                return {"ok": False, "detail": "Token Hugging Face não configurado"}
            try:
                resp = httpx.get(
                    "https://huggingface.co/api/whoami-v2",
                    headers={"Authorization": f"Bearer {hf_token}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    name = resp.json().get("name", "")
                    return {"ok": True, "detail": f"Token válido ({name})" if name else "Token válido"}
                return {"ok": False, "detail": f"Token inválido (HTTP {resp.status_code})"}
            except Exception as exc:
                return {"ok": False, "detail": f"Erro de conexão: {exc}"}

        if target == "openai":
            # OAuth tem prioridade; API key é fallback.
            oai_tokens = _oai.load_tokens(settings)
            oauth_error: Exception | None = None
            if oai_tokens:
                try:
                    _oai.get_access_token(settings)
                    email = oai_tokens.get("email")
                    detail = f"Conectado via OAuth ({email})" if email else "Conectado via OAuth"
                    return {"ok": True, "detail": detail}
                except Exception as exc:
                    oauth_error = exc
            api_key = settings.openai_api_key
            if not api_key:
                detail = str(oauth_error) if oauth_error else "OpenAI não configurado (OAuth nem API key)"
                return {"ok": False, "detail": detail}
            try:
                resp = httpx.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    return {"ok": True, "detail": "Conectado via API key"}
                return {"ok": False, "detail": f"Chave inválida (HTTP {resp.status_code})"}
            except Exception as exc:
                return {"ok": False, "detail": f"Erro de conexão: {exc}"}

        if target == "ollama":
            url = (settings.ollama_url or "http://localhost:11434").rstrip("/")
            try:
                resp = httpx.get(f"{url}/api/tags", timeout=10.0)
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    n = len(models)
                    return {"ok": True, "detail": f"{n} modelo{'s' if n != 1 else ''} disponível{'is' if n != 1 else ''}"}
                return {"ok": False, "detail": f"Ollama respondeu HTTP {resp.status_code}"}
            except Exception as exc:
                return {"ok": False, "detail": f"Erro de conexão: {exc}"}

        raise HTTPException(400, f"Target inválido: {target!r}. Válidos: anthropic, hf, openai, ollama")

    @app.post("/api/auth/anthropic/authorize")
    def api_authorize() -> dict:
        """Gera URL de autorização OAuth com PKCE. Verifier fica em memória."""
        import os as _os

        state = _os.urandom(16).hex()
        verifier, challenge = generate_pkce()
        with _pending_auth_lock:
            _purge_expired_pending()
            if len(_pending_verifiers) >= _MAX_PENDING:
                oldest = next(iter(_pending_verifiers))
                del _pending_verifiers[oldest]
            _pending_verifiers[state] = (verifier, time.monotonic())
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
        with _pending_auth_lock:
            _purge_expired_pending()
            entry = _pending_verifiers.pop(state, None)
        if entry is None:
            raise HTTPException(400, "State inválido ou expirado")
        verifier, _created = entry
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

    @app.post("/api/auth/openai/authorize")
    def api_openai_authorize() -> dict:
        """Inicia device-code flow OpenAI. Retorna url/state/user_code."""
        import os as _os

        try:
            data = _oai.request_device_code()
        except Exception as exc:
            raise HTTPException(503, f"Falha ao iniciar autenticação OpenAI: {exc}") from exc

        state = _os.urandom(16).hex()
        with _pending_auth_lock:
            _purge_expired_pending()
            if len(_pending_openai) >= _MAX_PENDING_OPENAI:
                oldest = next(iter(_pending_openai))
                del _pending_openai[oldest]
            _pending_openai[state] = {
                "device_auth_id": data["device_auth_id"],
                "user_code": data["user_code"],
                "interval": data.get("interval", 5),
                "created": time.monotonic(),
            }
        url = data.get("verification_uri_complete") or _oai._DEVICE_PAGE
        return {"url": url, "state": state, "user_code": data["user_code"]}

    @app.post("/api/auth/openai/exchange")
    def api_openai_exchange(body: OpenAIExchangeBody) -> dict:
        """Faz polling e troca de tokens OpenAI por state; persiste e ativa provider."""
        state = body.state.strip()
        with _pending_auth_lock:
            _purge_expired_pending()
            pending = _pending_openai.pop(state, None)
        if pending is None:
            raise HTTPException(400, "State inválido ou expirado")
        try:
            poll_result = _oai.poll_device_token(
                pending["device_auth_id"],
                pending["user_code"],
                interval=pending.get("interval", 5),
                timeout=30,
            )
            token_data = _oai.exchange_device_code(
                poll_result["authorization_code"],
                poll_result["code_verifier"],
            )
            tokens = _oai.build_stored_tokens(token_data)
        except TimeoutError as exc:
            with _pending_auth_lock:
                _pending_openai[state] = pending
            raise HTTPException(
                408,
                "Autorização ainda pendente. Autorize na OpenAI e tente concluir novamente.",
            ) from exc
        except Exception as exc:
            with _pending_auth_lock:
                _pending_openai[state] = pending
            raise HTTPException(400, f"Autenticação OpenAI falhou: {exc}") from exc
        settings = load_settings()
        _oai.save_tokens(settings, tokens)
        llm_patch = {"llm_provider": "openai"}
        if settings.llm_provider != "openai":
            llm_patch["llm_model"] = ""
        save_local_settings(llm_patch, settings)
        return {"ok": True, "email": tokens.get("email")}

    @app.delete("/api/auth/openai")
    def api_openai_logout() -> dict:
        """Remove tokens OAuth OpenAI."""
        settings = load_settings()
        _oai.clear_tokens(settings)
        return {"ok": True}

    # ── SPA (REGISTRAR POR ÚLTIMO) ────────────────────────────────────────────

    # ── Context export ────────────────────────────────────────────────────────

    @app.post("/api/context/export")
    def api_context_export(body: ContextExportBody) -> dict:
        if not body.task_ids:
            raise HTTPException(400, "task_ids não pode ser vazio")
        _, store = _settings_store()
        from ..context_export import build_export_package
        try:
            return build_export_package(
                store=store,
                task_ids=body.task_ids,
                objective=body.objective,
                fmt=body.format,
                include_summary=body.include_summary,
                include_facts=body.include_facts,
                include_evidence=body.include_evidence,
                include_transcript=body.include_transcript,
            )
        except KeyError as exc:
            raise HTTPException(404, f"ID não encontrado: {exc}") from exc

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
