"""Pacote de contexto canônico para exportação a outra LLM.

Responsabilidades:
- Busca ordenada de tarefas por IDs fornecidos (rejeita IDs ausentes).
- Coleta das reuniões e projetos associados.
- Renderização Markdown/JSON semanticamente equivalentes.
- Nenhuma geração de conteúdo — apenas agrupa dados existentes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from .models import FACT_KIND_LABELS, FACT_KINDS

if TYPE_CHECKING:
    from .store import Store



# ---------------------------------------------------------------------------
# Estruturas de dados internas (não são os modelos de domínio)
# ---------------------------------------------------------------------------


@dataclass
class _ProjectCtx:
    id: int
    name: str
    description: str
    repo_path: str


@dataclass
class _FactCtx:
    id: int
    kind: str
    text: str
    source_start: float | None
    evidence_quote: str | None
    explicitness: str
    review_status: str


@dataclass
class _MeetingCtx:
    id: int
    title: str
    date: str
    summary: str
    project_id: int | None
    facts: list[_FactCtx] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)  # {start, end, speaker, text}


@dataclass
class _TaskCtx:
    id: int
    what: str
    where: str | None
    details: str | None
    requested_by: str | None
    assigned_to: list[str] | None
    priority: str
    status: str
    due: str | None
    explicitness: str
    review_status: str
    source_start: float | None
    evidence_quote: str | None
    meeting_id: int
    meeting_title: str
    meeting_date: str
    project_id: int | None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _fetch_tasks_ordered(store: "Store", task_ids: list[int]) -> list[_TaskCtx]:
    """Busca tarefas na ordem dos IDs fornecidos; levanta KeyError em ID ausente."""
    if not task_ids:
        return []

    conn = store._conn  # acesso direto ao SQLite3 connection (padrão do projeto)
    ph = ",".join("?" * len(task_ids))
    rows = conn.execute(
        f"SELECT ai.id, ai.meeting_id, m.title AS meeting_title, m.date,"
        f"       ai.what, ai.where_, ai.details, ai.requested_by, ai.assigned_to,"
        f"       ai.priority, ai.status, ai.due, ai.explicitness, ai.review_status,"
        f"       ai.source_start, ai.evidence_quote, m.project_id"
        f" FROM action_items ai"
        f" JOIN meetings m ON m.id = ai.meeting_id"
        f" WHERE ai.id IN ({ph})",
        task_ids,
    ).fetchall()

    by_id: dict[int, _TaskCtx] = {}
    for r in rows:
        assigned_raw = r["assigned_to"]
        by_id[r["id"]] = _TaskCtx(
            id=r["id"],
            what=r["what"],
            where=r["where_"],
            details=r["details"],
            requested_by=r["requested_by"],
            assigned_to=json.loads(assigned_raw) if assigned_raw else None,
            priority=r["priority"],
            status=r["status"],
            due=r["due"],
            explicitness=r["explicitness"] or "inferred",
            review_status=r["review_status"] or "needs_review",
            source_start=r["source_start"],
            evidence_quote=r["evidence_quote"],
            meeting_id=r["meeting_id"],
            meeting_title=r["meeting_title"],
            meeting_date=r["date"],
            project_id=r["project_id"],
        )

    missing = [tid for tid in task_ids if tid not in by_id]
    if missing:
        raise KeyError(missing[0])

    return [by_id[tid] for tid in task_ids]


def _fetch_meetings(store: "Store", meeting_ids: set[int]) -> dict[int, _MeetingCtx]:
    if not meeting_ids:
        return {}
    conn = store._conn
    ph = ",".join("?" * len(meeting_ids))
    ids = list(meeting_ids)
    rows = conn.execute(
        f"SELECT id, title, date, summary, project_id"
        f" FROM meetings WHERE id IN ({ph})",
        ids,
    ).fetchall()
    result: dict[int, _MeetingCtx] = {}
    for r in rows:
        result[r["id"]] = _MeetingCtx(
            id=r["id"],
            title=r["title"],
            date=r["date"],
            summary=r["summary"] or "",
            project_id=r["project_id"],
        )

    # Fetch facts for all meetings
    fact_rows = conn.execute(
        f"SELECT id, meeting_id, kind, text, source_start, evidence_quote,"
        f"       explicitness, review_status"
        f" FROM meeting_facts WHERE meeting_id IN ({ph})"
        f" ORDER BY meeting_id, id",
        ids,
    ).fetchall()
    for fr in fact_rows:
        mid = fr["meeting_id"]
        if mid in result:
            result[mid].facts.append(
                _FactCtx(
                    id=fr["id"],
                    kind=fr["kind"],
                    text=fr["text"],
                    source_start=fr["source_start"],
                    evidence_quote=fr["evidence_quote"],
                    explicitness=fr["explicitness"] or "inferred",
                    review_status=fr["review_status"] or "needs_review",
                )
            )

    return result


def _fetch_segments(store: "Store", meeting_ids: set[int]) -> dict[int, list[dict]]:
    if not meeting_ids:
        return {}
    conn = store._conn
    ph = ",".join("?" * len(meeting_ids))
    ids = list(meeting_ids)
    rows = conn.execute(
        f"SELECT meeting_id, start, end, speaker, text"
        f" FROM segments WHERE meeting_id IN ({ph})"
        f" ORDER BY meeting_id, start",
        ids,
    ).fetchall()
    result: dict[int, list[dict]] = {mid: [] for mid in ids}
    for r in rows:
        result[r["meeting_id"]].append(
            {
                "start": r["start"],
                "end": r["end"],
                "speaker": r["speaker"],
                "text": r["text"],
            }
        )
    return result


def _fetch_projects(store: "Store", project_ids: set[int]) -> dict[int, _ProjectCtx]:
    if not project_ids:
        return {}
    conn = store._conn
    ph = ",".join("?" * len(project_ids))
    ids = list(project_ids)
    rows = conn.execute(
        f"SELECT id, name, description, repo_path FROM projects WHERE id IN ({ph})",
        ids,
    ).fetchall()
    return {
        r["id"]: _ProjectCtx(
            id=r["id"],
            name=r["name"],
            description=r["description"] or "",
            repo_path=r["repo_path"] or "",
        )
        for r in rows
    }


# ---------------------------------------------------------------------------
# Deterministic filename
# ---------------------------------------------------------------------------


def _safe_filename(task_ids: list[int], fmt: str) -> str:
    """Nome de arquivo determinístico baseado nos IDs, sem depender de wall clock."""
    digest = hashlib.sha1(",".join(str(i) for i in task_ids).encode()).hexdigest()[:8]
    ext = "md" if fmt == "markdown" else "json"
    return f"context-export-{digest}.{ext}"


# ---------------------------------------------------------------------------
# Helpers de formatação
# ---------------------------------------------------------------------------


def _fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"




# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_markdown(
    tasks: list[_TaskCtx],
    meetings: dict[int, _MeetingCtx],
    projects: dict[int, _ProjectCtx],
    objective: str,
    include_summary: bool,
    include_facts: bool,
    include_evidence: bool,
    include_transcript: bool,
) -> str:
    lines: list[str] = []

    # Header
    lines.append("# Context Export")
    lines.append("")
    if objective:
        lines.append(f"**Objetivo:** {objective}")
        lines.append("")

    # Scope summary
    task_count = len(tasks)
    meeting_ids_used = {t.meeting_id for t in tasks}
    lines.append(
        f"**Tarefas:** {task_count}  "
    )
    lines.append(
        f"**Reuniões referenciadas:** {len(meeting_ids_used)}  "
    )
    lines.append("")

    # Projects section
    proj_ids_used = {
        meetings[mid].project_id
        for mid in meeting_ids_used
        if mid in meetings and meetings[mid].project_id is not None
    }
    if proj_ids_used:
        lines.append("## Projetos")
        lines.append("")
        for pid in sorted(proj_ids_used):
            proj = projects.get(pid)
            if proj is None:
                continue
            lines.append(f"### {proj.name}")
            if proj.description:
                lines.append("")
                lines.append(proj.description)
            if proj.repo_path:
                lines.append("")
                lines.append(f"**Repositório:** `{proj.repo_path}`")
            lines.append("")

    # Tasks section
    lines.append("## Tarefas")
    lines.append("")
    for task in tasks:
        status_label = "✅" if task.status == "feito" else "⬜"
        lines.append(f"### {status_label} [{task.id}] {task.what}")
        lines.append("")
        if task.where:
            lines.append(f"- **Onde:** {task.where}")
        if task.details:
            lines.append(f"- **Detalhes:** {task.details}")
        if task.requested_by:
            lines.append(f"- **Pedido por:** {task.requested_by}")
        if task.assigned_to:
            lines.append(f"- **Responsáveis:** {', '.join(task.assigned_to)}")
        lines.append(f"- **Prioridade:** {task.priority}")
        lines.append(f"- **Status:** {task.status}")
        if task.due:
            lines.append(f"- **Prazo:** {task.due}")
        lines.append(
            f"- **Reunião:** {task.meeting_title} ({task.meeting_date}) [id={task.meeting_id}]"
        )
        lines.append(f"- **Explicitness:** {task.explicitness}")
        lines.append(f"- **Review:** {task.review_status}")
        if include_evidence and task.evidence_quote:
            ts = _fmt_ts(task.source_start)
            lines.append(f"- **Evidência** ({ts}): _{task.evidence_quote}_")
        lines.append("")

    # Meetings section (ordered by date, then id)
    ordered_meetings = sorted(
        (meetings[mid] for mid in meeting_ids_used if mid in meetings),
        key=lambda m: (m.date, m.id),
    )
    if ordered_meetings:
        lines.append("## Reuniões")
        lines.append("")
        for mtg in ordered_meetings:
            proj = projects.get(mtg.project_id) if mtg.project_id else None
            proj_tag = f" [{proj.name}]" if proj else ""
            lines.append(f"### {mtg.title}{proj_tag} — {mtg.date} [id={mtg.id}]")
            lines.append("")

            if include_summary and mtg.summary:
                lines.append("**Resumo:**")
                lines.append("")
                lines.append(mtg.summary)
                lines.append("")

            if include_facts and mtg.facts:
                lines.append("**Fatos:**")
                lines.append("")
                for kind in FACT_KINDS:
                    kind_facts = [f for f in mtg.facts if f.kind == kind]
                    if not kind_facts:
                        continue
                    lines.append(f"*{FACT_KIND_LABELS.get(kind, kind)}*")
                    lines.append("")
                    for fact in kind_facts:
                        review_badge = (
                            "✔" if fact.review_status == "confirmed" else "?"
                        )
                        lines.append(
                            f"- [{review_badge}] {fact.text}"
                            f" ({fact.explicitness})"
                        )
                        if include_evidence and fact.evidence_quote:
                            ts = _fmt_ts(fact.source_start)
                            lines.append(
                                f"  > _{fact.evidence_quote}_ ({ts})"
                            )
                    lines.append("")

            if include_transcript and mtg.segments:
                lines.append("**Transcript:**")
                lines.append("")
                prev_speaker: str | None = None
                for seg in mtg.segments:
                    speaker = seg.get("speaker") or "?"
                    ts = _fmt_ts(seg["start"])
                    if speaker != prev_speaker:
                        lines.append(f"**{speaker}** [{ts}]")
                        prev_speaker = speaker
                    lines.append(seg["text"])
                lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON renderer
# ---------------------------------------------------------------------------


def _render_json_data(
    tasks: list[_TaskCtx],
    meetings: dict[int, _MeetingCtx],
    projects: dict[int, _ProjectCtx],
    objective: str,
    include_summary: bool,
    include_facts: bool,
    include_evidence: bool,
    include_transcript: bool,
) -> dict:
    meeting_ids_used = {t.meeting_id for t in tasks}
    proj_ids_used = {
        meetings[mid].project_id
        for mid in meeting_ids_used
        if mid in meetings and meetings[mid].project_id is not None
    }

    tasks_out = []
    for task in tasks:
        t: dict = {
            "id": task.id,
            "what": task.what,
            "where": task.where,
            "details": task.details,
            "requested_by": task.requested_by,
            "assigned_to": task.assigned_to,
            "priority": task.priority,
            "status": task.status,
            "due": task.due,
            "explicitness": task.explicitness,
            "review_status": task.review_status,
            "meeting_id": task.meeting_id,
            "meeting_title": task.meeting_title,
            "meeting_date": task.meeting_date,
            "project_id": task.project_id,
        }
        if include_evidence:
            t["source_start"] = task.source_start
            t["evidence_quote"] = task.evidence_quote
        tasks_out.append(t)

    meetings_out = []
    for mtg in sorted(
        (meetings[mid] for mid in meeting_ids_used if mid in meetings),
        key=lambda m: (m.date, m.id),
    ):
        m: dict = {
            "id": mtg.id,
            "title": mtg.title,
            "date": mtg.date,
            "project_id": mtg.project_id,
        }
        if include_summary:
            m["summary"] = mtg.summary
        if include_facts and mtg.facts:
            facts_by_kind: dict[str, list] = {}
            for fact in mtg.facts:
                f: dict = {
                    "id": fact.id,
                    "text": fact.text,
                    "explicitness": fact.explicitness,
                    "review_status": fact.review_status,
                }
                if include_evidence:
                    f["source_start"] = fact.source_start
                    f["evidence_quote"] = fact.evidence_quote
                facts_by_kind.setdefault(fact.kind, []).append(f)
            m["facts"] = facts_by_kind
        else:
            m["facts"] = {}
        if include_transcript and mtg.segments:
            m["transcript"] = mtg.segments
        meetings_out.append(m)

    projects_out = []
    for pid in sorted(proj_ids_used):
        proj = projects.get(pid)
        if proj is None:
            continue
        projects_out.append(
            {
                "id": proj.id,
                "name": proj.name,
                "description": proj.description,
                "repo_path": proj.repo_path,
            }
        )

    return {
        "objective": objective,
        "projects": projects_out,
        "tasks": tasks_out,
        "meetings": meetings_out,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_export_package(
    store: "Store",
    task_ids: list[int],
    objective: str = "",
    fmt: str = "markdown",
    include_summary: bool = True,
    include_facts: bool = True,
    include_evidence: bool = True,
    include_transcript: bool = False,
) -> dict:
    """Monta e serializa pacote de contexto. Levanta KeyError em ID ausente.

    Retorna dict com chaves: format, filename, content, task_count, meeting_count.
    """
    tasks = _fetch_tasks_ordered(store, task_ids)

    meeting_ids = {t.meeting_id for t in tasks}
    meetings = _fetch_meetings(store, meeting_ids)

    if include_transcript:
        segs = _fetch_segments(store, meeting_ids)
        for mid, seg_list in segs.items():
            if mid in meetings:
                meetings[mid].segments = seg_list

    proj_ids = {
        meetings[mid].project_id
        for mid in meeting_ids
        if mid in meetings and meetings[mid].project_id is not None
    }
    projects = _fetch_projects(store, proj_ids)

    filename = _safe_filename(task_ids, fmt)

    if fmt == "markdown":
        content = _render_markdown(
            tasks=tasks,
            meetings=meetings,
            projects=projects,
            objective=objective,
            include_summary=include_summary,
            include_facts=include_facts,
            include_evidence=include_evidence,
            include_transcript=include_transcript,
        )
    else:
        data = _render_json_data(
            tasks=tasks,
            meetings=meetings,
            projects=projects,
            objective=objective,
            include_summary=include_summary,
            include_facts=include_facts,
            include_evidence=include_evidence,
            include_transcript=include_transcript,
        )
        content = json.dumps(data, ensure_ascii=False, indent=2)

    return {
        "format": fmt,
        "filename": filename,
        "content": content,
        "task_count": len(tasks),
        "meeting_count": len(meeting_ids),
    }
