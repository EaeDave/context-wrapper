"""Persistência SQLite para o pipeline de reuniões."""

from __future__ import annotations

from collections.abc import Callable
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import ActionItem, MeetingFact, MeetingResult, TranscriptSegment


_SCHEMA = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meetings (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT    NOT NULL,
    title    TEXT    NOT NULL,
    source   TEXT    NOT NULL,
    duration REAL    NOT NULL,
    summary  TEXT    NOT NULL DEFAULT '',
    md_path  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   INTEGER NOT NULL REFERENCES meetings(id),
    what         TEXT    NOT NULL,
    where_       TEXT,
    details      TEXT,
    requested_by TEXT,
    priority     TEXT    NOT NULL DEFAULT 'media',
    status       TEXT    NOT NULL DEFAULT 'aberto',
    due          TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    start      REAL    NOT NULL,
    end        REAL    NOT NULL,
    speaker    TEXT,
    text       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS voices (
    name      TEXT PRIMARY KEY,
    embedding BLOB NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5 (
    content,
    meeting_id UNINDEXED,
    kind       UNINDEXED
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    repo_path   TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_name ON projects (name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS meeting_facts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id     INTEGER NOT NULL REFERENCES meetings(id),
    kind           TEXT    NOT NULL,
    text           TEXT    NOT NULL,
    source_start   REAL,
    source_end     REAL,
    evidence_quote TEXT,
    explicitness   TEXT    NOT NULL DEFAULT 'inferred',
    review_status  TEXT    NOT NULL DEFAULT 'needs_review'
);

"""
# Colunas novas (migração idempotente via PRAGMA table_info)
_MEETING_EXTRA_COLS: list[tuple[str, str]] = [
    ("source_origin", "TEXT NOT NULL DEFAULT ''"),
    ("media_managed", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("speaker_matches", "TEXT NOT NULL DEFAULT '{}'"),
    ("project_id", "INTEGER"),
]

_ACTION_ITEM_EXTRA_COLS: list[tuple[str, str]] = [
    ("status", "TEXT NOT NULL DEFAULT 'aberto'"),
    ("due", "TEXT"),
    ("assigned_to", "TEXT"),
    ("source_start", "REAL"),
    ("source_end", "REAL"),
    ("evidence_quote", "TEXT"),
    ("explicitness", "TEXT NOT NULL DEFAULT 'inferred'"),
    ("review_status", "TEXT NOT NULL DEFAULT 'needs_review'"),
]

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MeetingRow:
    """Resumo de reunião pra listagem / UI."""

    id: int
    date: str
    title: str
    source: str
    source_origin: str
    media_managed: bool
    media_ok: bool
    duration: float = 0.0
    project_id: int | None = None
    project_name: str | None = None


@dataclass
class ProjectRow:
    """Projeto com contagens."""

    id: int
    name: str
    description: str
    repo_path: str
    meeting_count: int
    open_task_count: int
    done_task_count: int
    last_meeting_date: str | None
    created_at: str
    updated_at: str


class Store:
    """Banco de dados de reuniões (sqlite3, WAL)."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        meeting_cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(meetings)").fetchall()
        }
        action_cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(action_items)").fetchall()
        }
        with self._conn:
            for name, decl in _MEETING_EXTRA_COLS:
                if name not in meeting_cols:
                    self._conn.execute(
                        f"ALTER TABLE meetings ADD COLUMN {name} {decl}"
                    )
            for name, decl in _ACTION_ITEM_EXTRA_COLS:
                if name not in action_cols:
                    self._conn.execute(
                        f"ALTER TABLE action_items ADD COLUMN {name} {decl}"
                    )
            # Backfill timestamps vazios
            now = _now()
            self._conn.execute(
                "UPDATE meetings SET created_at = ? WHERE created_at = '' OR created_at IS NULL",
                (now,),
            )
            self._conn.execute(
                "UPDATE meetings SET updated_at = ? WHERE updated_at = '' OR updated_at IS NULL",
                (now,),
            )
            # Reuniões antigas: source_origin vazio → copia source
            self._conn.execute(
                "UPDATE meetings SET source_origin = source"
                " WHERE source_origin = '' OR source_origin IS NULL"
            )
            # Índice de project_id (idempotente)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_meetings_project_id ON meetings(project_id)"
            )
            # Índice meeting_facts.meeting_id (idempotente)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_meeting_facts_meeting_id"
                " ON meeting_facts(meeting_id)"
            )

    # ------------------------------------------------------------------
    # Reuniões
    # ------------------------------------------------------------------

    def save_meeting(
        self,
        result: MeetingResult,
        md_path: Path,
        *,
        project_id: int | None = None,
    ) -> int:
        """Persiste reunião completa; retorna o id gerado."""
        now = _now()
        origin = result.source
        pid = project_id if project_id is not None else result.project_id
        if pid is not None and self.get_project(pid) is None:
            raise ValueError(f"Projeto {pid} não encontrado")
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO meetings"
                " (date, title, source, duration, summary, md_path,"
                "  source_origin, media_managed, created_at, updated_at, speaker_matches, project_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (
                    result.date,
                    result.title,
                    result.source,
                    result.duration,
                    result.summary,
                    str(md_path),
                    origin,
                    now,
                    now,
                    json.dumps(result.speaker_matches),
                    pid,
                ),
            )
            meeting_id: int = cur.lastrowid  # type: ignore[assignment]

            for item in result.action_items:
                self._conn.execute(
                    "INSERT INTO action_items"
                    " (meeting_id, what, where_, details, requested_by, priority, status, due,"
                    "  assigned_to, source_start, source_end, evidence_quote, explicitness, review_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        meeting_id,
                        item.what,
                        item.where,
                        item.details,
                        item.requested_by,
                        item.priority,
                        item.status,
                        item.due,
                        json.dumps(item.assigned_to) if item.assigned_to is not None else None,
                        item.source_start,
                        item.source_end,
                        item.evidence_quote,
                        item.explicitness,
                        item.review_status,
                    ),
                )

            for fact in result.facts:
                self._conn.execute(
                    "INSERT INTO meeting_facts"
                    " (meeting_id, kind, text, source_start, source_end,"
                    "  evidence_quote, explicitness, review_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        meeting_id,
                        fact.kind,
                        fact.text,
                        fact.source_start,
                        fact.source_end,
                        fact.evidence_quote,
                        fact.explicitness,
                        fact.review_status,
                    ),
                )

            for seg in result.segments:
                self._conn.execute(
                    "INSERT INTO segments (meeting_id, start, end, speaker, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (meeting_id, seg.start, seg.end, seg.speaker, seg.text),
                )

            self._index_meeting(meeting_id, result.segments, result.action_items, result.facts)

        return meeting_id

    def get_meeting(self, meeting_id: int) -> MeetingResult | None:
        """Reconstrói MeetingResult; anexa attrs de mídia/md_path."""
        row = self._conn.execute(
            "SELECT id, date, title, source, duration, summary, md_path,"
            "       source_origin, media_managed, created_at, updated_at, speaker_matches, project_id"
            " FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if row is None:
            return None

        segments = [
            TranscriptSegment(
                start=r["start"],
                end=r["end"],
                text=r["text"],
                speaker=r["speaker"],
                id=r["id"],
            )
            for r in self._conn.execute(
                "SELECT id, start, end, speaker, text FROM segments"
                " WHERE meeting_id = ? ORDER BY start",
                (meeting_id,),
            )
        ]

        action_items = [
            ActionItem(
                what=r["what"],
                where=r["where_"],
                details=r["details"],
                requested_by=r["requested_by"],
                priority=r["priority"],
                id=r["id"],
                status=r["status"],
                due=r["due"],
                assigned_to=json.loads(r["assigned_to"]) if r["assigned_to"] else None,
                source_start=r["source_start"],
                source_end=r["source_end"],
                evidence_quote=r["evidence_quote"],
                explicitness=r["explicitness"] or "inferred",
                review_status=r["review_status"] or "needs_review",
            )
            for r in self._conn.execute(
                "SELECT id, what, where_, details, requested_by, priority, status, due,"
                "       assigned_to, source_start, source_end, evidence_quote,"
                "       explicitness, review_status"
                " FROM action_items WHERE meeting_id = ?",
                (meeting_id,),
            )
        ]

        facts = [
            MeetingFact(
                id=r["id"],
                kind=r["kind"],
                text=r["text"],
                source_start=r["source_start"],
                source_end=r["source_end"],
                evidence_quote=r["evidence_quote"],
                explicitness=r["explicitness"] or "inferred",
                review_status=r["review_status"] or "needs_review",
            )
            for r in self._conn.execute(
                "SELECT id, kind, text, source_start, source_end, evidence_quote,"
                "       explicitness, review_status"
                " FROM meeting_facts WHERE meeting_id = ? ORDER BY id",
                (meeting_id,),
            )
        ]

        participants = sorted({s.speaker for s in segments if s.speaker})

        result = MeetingResult(
            source=row["source"],
            date=row["date"],
            title=row["title"],
            duration=row["duration"],
            participants=participants,
            summary=row["summary"],
            action_items=action_items,
            facts=facts,
            segments=segments,
            speaker_matches=json.loads(row["speaker_matches"] or "{}"),
            project_id=row["project_id"],
        )
        result.md_path = Path(row["md_path"]) if row["md_path"] else None  # type: ignore[attr-defined]
        result.meeting_id = meeting_id  # type: ignore[attr-defined]
        result.source_origin = row["source_origin"] or row["source"]  # type: ignore[attr-defined]
        result.media_managed = bool(row["media_managed"])  # type: ignore[attr-defined]
        result.media_ok = Path(row["source"]).expanduser().is_file()  # type: ignore[attr-defined]
        result.created_at = row["created_at"] or ""  # type: ignore[attr-defined]
        result.updated_at = row["updated_at"] or ""  # type: ignore[attr-defined]
        return result

    def list_meetings(self) -> list[tuple[int, str, str]]:
        """(id, date, title) — compat CLI / testes."""
        rows = self.list_meeting_rows()
        return [(r.id, r.date, r.title) for r in rows]

    def list_meeting_rows(
        self,
        *,
        project_filter: "int | str | None" = None,
    ) -> list[MeetingRow]:
        """Listagem rica com status de mídia.

        project_filter:
            None       → sem filtro (todas as reuniões)
            'none'     → reuniões sem projeto (project_id IS NULL)
            int        → reuniões do projeto especificado
        """
        where = ""
        params: list = []
        if project_filter == "none":
            where = " WHERE m.project_id IS NULL"
        elif isinstance(project_filter, int):
            where = " WHERE m.project_id = ?"
            params.append(project_filter)

        sql = (
            "SELECT m.id, m.date, m.title, m.source, m.source_origin, m.media_managed,"
            "       m.duration, m.project_id, p.name AS project_name"
            " FROM meetings m"
            " LEFT JOIN projects p ON p.id = m.project_id"
            + where
            + " ORDER BY m.date DESC, m.id DESC"
        )
        rows = self._conn.execute(sql, params).fetchall()
        out: list[MeetingRow] = []
        for r in rows:
            src = r["source"] or ""
            out.append(
                MeetingRow(
                    id=r["id"],
                    date=r["date"],
                    title=r["title"],
                    source=src,
                    source_origin=r["source_origin"] or src,
                    media_managed=bool(r["media_managed"]),
                    media_ok=Path(src).expanduser().is_file() if src else False,
                    duration=float(r["duration"] or 0),
                    project_id=r["project_id"],
                    project_name=r["project_name"],
                )
            )
        return out

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        project_filter: "int | str | None" = None,
    ) -> list[dict]:
        """FTS5 full-text search; retorna dicts com meeting_id, date, title, kind, snippet.

        project_filter tem a mesma semântica de list_meeting_rows.
        """
        extra_where = ""
        extra_params: list = []
        if project_filter == "none":
            extra_where = " AND m.project_id IS NULL"
        elif isinstance(project_filter, int):
            extra_where = " AND m.project_id = ?"
            extra_params.append(project_filter)

        sql = (
            "SELECT si.meeting_id,"
            "       m.date,"
            "       m.title,"
            "       si.kind,"
            "       snippet(search_index, 0, '[', ']', '...', 10) AS snippet"
            " FROM   search_index si"
            " JOIN   meetings m ON m.id = CAST(si.meeting_id AS INTEGER)"
            " WHERE  search_index MATCH ?"
            + extra_where
            + " ORDER  BY rank"
            " LIMIT  ?"
        )
        rows = self._conn.execute(sql, [query, *extra_params, limit]).fetchall()
        return [dict(r) for r in rows]

    def update_title(self, meeting_id: int, title: str) -> bool:
        """Atualiza título; retorna False se id inexistente."""
        title = title.strip()
        if not title:
            raise ValueError("Título vazio")
        with self._conn:
            cur = self._conn.execute(
                "UPDATE meetings SET title = ?, updated_at = ? WHERE id = ?",
                (title, _now(), meeting_id),
            )
            return cur.rowcount > 0

    def update_summary(self, meeting_id: int, summary: str) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE meetings SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, _now(), meeting_id),
            )
            return cur.rowcount > 0

    def set_media(
        self,
        meeting_id: int,
        *,
        source: Path,
        source_origin: str,
        media_managed: bool,
    ) -> None:
        """Atualiza paths de mídia após import/relink."""
        with self._conn:
            self._conn.execute(
                "UPDATE meetings SET source = ?, source_origin = ?,"
                " media_managed = ?, updated_at = ? WHERE id = ?",
                (
                    str(source),
                    source_origin,
                    1 if media_managed else 0,
                    _now(),
                    meeting_id,
                ),
            )

    def adopt_media(
        self,
        meeting_id: int,
        data_dir: Path,
        origin: Path,
        on_progress: Callable[[float], None] | None = None,
    ) -> Path:
        """Importa origin para media/{id}/ e atualiza o registro."""
        from . import media as media_mod

        origin = Path(origin).expanduser()
        dest = media_mod.import_original(
            data_dir, meeting_id, origin, on_progress=on_progress
        )
        self.set_media(
            meeting_id,
            source=dest,
            source_origin=str(origin.resolve()),
            media_managed=True,
        )
        # MeetingResult.source no pipeline já foi o origin — quem lê o DB vê dest
        return dest

    def delete_meeting(
        self,
        meeting_id: int,
        *,
        data_dir: Path,
        delete_markdown: bool = True,
    ) -> bool:
        """Apaga reunião do DB, pending, pasta media/{id}/ e opcionalmente o .md."""
        from . import media as media_mod

        row = self._conn.execute(
            "SELECT md_path, media_managed FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        if row is None:
            return False

        md_path = row["md_path"] or ""
        with self._conn:
            self._conn.execute(
                "DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,)
            )
            self._conn.execute(
                "DELETE FROM meeting_facts WHERE meeting_id = ?", (meeting_id,)
            )
            self._conn.execute(
                "DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,)
            )
            self._conn.execute(
                "DELETE FROM segments WHERE meeting_id = ?", (meeting_id,)
            )
            self._conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))

        media_mod.purge_media(data_dir, meeting_id)

        pending = data_dir / "pending" / f"{meeting_id}.npz"
        if pending.is_file():
            pending.unlink(missing_ok=True)

        if delete_markdown and md_path:
            p = Path(md_path)
            if p.is_file():
                p.unlink(missing_ok=True)

        return True

    def delete_meetings(
        self,
        meeting_ids: list[int],
        *,
        data_dir: Path,
        delete_markdown: bool = True,
    ) -> int:
        """Apaga várias reuniões; retorna quantas existiam e foram removidas."""
        n = 0
        for mid in meeting_ids:
            if self.delete_meeting(
                mid, data_dir=data_dir, delete_markdown=delete_markdown
            ):
                n += 1
        return n

    # ------------------------------------------------------------------
    # Action items (CRUD)
    # ------------------------------------------------------------------

    _AI_WHITELIST = frozenset({
        "what", "where_", "details", "requested_by", "priority", "status", "due",
        "assigned_to", "source_start", "source_end", "evidence_quote",
        "explicitness", "review_status",
    })

    def _revalidate_traceability(self, meeting_id: int) -> None:
        """Recalcula review_status de tarefas e fatos contra transcript persistido."""
        from .extract import _validate_evidence

        segments = [
            TranscriptSegment(
                start=row["start"],
                end=row["end"],
                text=row["text"],
                speaker=row["speaker"],
            )
            for row in self._conn.execute(
                "SELECT start, end, speaker, text FROM segments WHERE meeting_id = ?"
                " ORDER BY start",
                (meeting_id,),
            )
        ]
        for table in ("action_items", "meeting_facts"):
            rows = self._conn.execute(
                f"SELECT id, source_start, source_end, evidence_quote FROM {table}"
                " WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchall()
            for row in rows:
                confirmed = _validate_evidence(
                    segments,
                    row["source_start"],
                    row["source_end"],
                    row["evidence_quote"],
                )
                self._conn.execute(
                    f"UPDATE {table} SET review_status = ? WHERE id = ?",
                    ("confirmed" if confirmed else "needs_review", row["id"]),
                )

    def update_action_item(self, item_id: int, fields: dict) -> bool:
        """Atualiza campos de action item; reindexa FTS e regenera .md."""
        mapped: dict = {}
        for k, v in fields.items():
            mapped["where_" if k == "where" else k] = v
        # Serialize assigned_to list → JSON if provided
        if "assigned_to" in mapped and isinstance(mapped["assigned_to"], list):
            mapped["assigned_to"] = json.dumps(mapped["assigned_to"])
        safe = {k: v for k, v in mapped.items() if k in self._AI_WHITELIST}
        if not safe:
            return False
        row = self._conn.execute(
            "SELECT meeting_id FROM action_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return False
        meeting_id: int = row["meeting_id"]
        set_clause = ", ".join(f"{k} = ?" for k in safe)
        vals = list(safe.values()) + [item_id]
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE action_items SET {set_clause} WHERE id = ?", vals
            )
            if cur.rowcount == 0:
                return False
            self._revalidate_traceability(meeting_id)
            self._conn.execute(
                "DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,)
            )
            self._reindex_meeting_fts(meeting_id)
            self._conn.execute(
                "UPDATE meetings SET updated_at = ? WHERE id = ?", (_now(), meeting_id)
            )
        self._regen_md(meeting_id)
        return True

    def add_action_item(self, meeting_id: int, item: ActionItem) -> int:
        """Insere novo action item; reindexa FTS e regenera .md. Retorna id."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO action_items"
                " (meeting_id, what, where_, details, requested_by, priority, status, due,"
                "  assigned_to, source_start, source_end, evidence_quote, explicitness, review_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    item.what,
                    item.where,
                    item.details,
                    item.requested_by,
                    item.priority,
                    item.status,
                    item.due,
                    json.dumps(item.assigned_to) if item.assigned_to is not None else None,
                    item.source_start,
                    item.source_end,
                    item.evidence_quote,
                    item.explicitness,
                    item.review_status,
                ),
            )
            new_id: int = cur.lastrowid  # type: ignore[assignment]
            self._revalidate_traceability(meeting_id)
            self._conn.execute(
                "DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,)
            )
            self._reindex_meeting_fts(meeting_id)
            self._conn.execute(
                "UPDATE meetings SET updated_at = ? WHERE id = ?", (_now(), meeting_id)
            )
        self._regen_md(meeting_id)
        return new_id

    def delete_action_item(self, item_id: int) -> bool:
        """Remove action item; reindexa FTS e regenera .md. Retorna False se não existe."""
        row = self._conn.execute(
            "SELECT meeting_id FROM action_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return False
        meeting_id: int = row["meeting_id"]
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM action_items WHERE id = ?", (item_id,)
            )
            if cur.rowcount == 0:
                return False
            self._conn.execute(
                "DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,)
            )
            self._reindex_meeting_fts(meeting_id)
            self._conn.execute(
                "UPDATE meetings SET updated_at = ? WHERE id = ?", (_now(), meeting_id)
            )
        self._regen_md(meeting_id)
        return True

    def list_tasks(
        self,
        status: str = "aberto",
        *,
        project_filter: "int | str | None" = None,
        scope: str = "personal",
    ) -> list[dict]:
        """Lista tarefas com filtro de scope.

        status: 'aberto'|'feito'|'todos'
        project_filter: None=todos, 'none'=sem projeto, int=projeto específico
        scope: 'personal' (me ou sem dono), 'delegated' (não nulo e sem me), 'all'
        """
        conditions: list[str] = []
        params: list = []
        if status != "todos":
            conditions.append("ai.status = ?")
            params.append(status)
        if project_filter == "none":
            conditions.append("m.project_id IS NULL")
        elif isinstance(project_filter, int):
            conditions.append("m.project_id = ?")
            params.append(project_filter)
        # Scope filter using json_each for exact element equality (case-insensitive)
        if scope == "personal":
            # no owner OR assigned_to JSON array contains element 'me' (case-insensitive exact)
            conditions.append(
                "(ai.assigned_to IS NULL"
                " OR EXISTS (SELECT 1 FROM json_each(ai.assigned_to)"
                "            WHERE lower(json_each.value) = 'me'))"
            )
        elif scope == "delegated":
            # owner is set AND does not contain 'me'
            conditions.append(
                "(ai.assigned_to IS NOT NULL"
                " AND NOT EXISTS (SELECT 1 FROM json_each(ai.assigned_to)"
                "                 WHERE lower(json_each.value) = 'me'))"
            )
        # scope == "all": no extra filter
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""\
            SELECT ai.id, ai.meeting_id, m.title AS meeting_title, m.date,
                   ai.what, ai.where_, ai.details, ai.requested_by, ai.priority,
                   ai.status, ai.due, m.project_id, p.name AS project_name,
                   ai.assigned_to, ai.source_start, ai.source_end,
                   ai.evidence_quote, ai.explicitness, ai.review_status
            FROM action_items ai
            JOIN meetings m ON m.id = ai.meeting_id
            LEFT JOIN projects p ON p.id = m.project_id
            {where}
            ORDER BY
                CASE ai.status WHEN 'aberto' THEN 0 ELSE 1 END,
                CASE ai.priority WHEN 'alta' THEN 0 WHEN 'media' THEN 1 ELSE 2 END,
                m.date DESC
        """
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "meeting_id": r["meeting_id"],
                "meeting_title": r["meeting_title"],
                "date": r["date"],
                "what": r["what"],
                "where": r["where_"],
                "details": r["details"],
                "requested_by": r["requested_by"],
                "priority": r["priority"],
                "status": r["status"],
                "due": r["due"],
                "project_id": r["project_id"],
                "project_name": r["project_name"],
                "assigned_to": json.loads(r["assigned_to"]) if r["assigned_to"] else None,
                "source_start": r["source_start"],
                "source_end": r["source_end"],
                "evidence_quote": r["evidence_quote"],
                "explicitness": r["explicitness"] or "inferred",
                "review_status": r["review_status"] or "needs_review",
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Transcript — edição de turnos
    # ------------------------------------------------------------------

    def update_turn(
        self,
        meeting_id: int,
        seg_ids: list[int],
        text: str | None,
        speaker: str | None,
    ) -> bool:
        """Atualiza segmentos de um turno. text=None → só speaker; text fornecido → colapsa."""
        if not seg_ids:
            return False
        placeholders = ",".join("?" * len(seg_ids))
        rows = self._conn.execute(
            f"SELECT id, start, end, speaker FROM segments"
            f" WHERE meeting_id = ? AND id IN ({placeholders})"
            f" ORDER BY start",
            (meeting_id, *seg_ids),
        ).fetchall()
        if not rows:
            return False
        if text is None:
            # speaker-only: UPDATE todos os segmentos
            if speaker is None:
                return False
            with self._conn:
                self._conn.execute(
                    f"UPDATE segments SET speaker = ?"
                    f" WHERE meeting_id = ? AND id IN ({placeholders})",
                    (speaker, meeting_id, *seg_ids),
                )
                self._conn.execute(
                    "DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,)
                )
                self._reindex_meeting_fts(meeting_id)
                self._conn.execute(
                    "UPDATE meetings SET updated_at = ? WHERE id = ?",
                    (_now(), meeting_id),
                )
        else:
            # Colapsar: primeiro segmento recebe start/end/text/speaker; demais deletados
            first = rows[0]
            start = first["start"]
            end = rows[-1]["end"]
            spk = speaker if speaker is not None else first["speaker"]
            first_id = first["id"]
            rest_ids = [r["id"] for r in rows[1:]]
            with self._conn:
                self._conn.execute(
                    "UPDATE segments SET start=?, end=?, text=?, speaker=?"
                    " WHERE id=?",
                    (start, end, text, spk, first_id),
                )
                if rest_ids:
                    rest_ph = ",".join("?" * len(rest_ids))
                    self._conn.execute(
                        f"DELETE FROM segments WHERE id IN ({rest_ph})", rest_ids
                    )
                self._revalidate_traceability(meeting_id)
                self._conn.execute(
                    "DELETE FROM search_index WHERE meeting_id = ?", (meeting_id,)
                )
                self._reindex_meeting_fts(meeting_id)
                self._conn.execute(
                    "UPDATE meetings SET updated_at = ? WHERE id = ?",
                    (_now(), meeting_id),
                )
        self._regen_md(meeting_id)
        return True

    # ------------------------------------------------------------------
    # Reprocess / reextract
    # ------------------------------------------------------------------

    def replace_meeting_content(self, meeting_id: int, result: "MeetingResult") -> None:
        """Substitui segments, action_items e facts in-place; preserva source/date/media. Atômico."""
        with self._conn:
            self._conn.execute(
                "UPDATE meetings SET title=?, summary=?, duration=?, updated_at=?, speaker_matches=?"
                " WHERE id=?",
                (result.title, result.summary, result.duration, _now(),
                 json.dumps(result.speaker_matches), meeting_id),
            )
            self._conn.execute(
                "DELETE FROM segments WHERE meeting_id=?", (meeting_id,)
            )
            self._conn.execute(
                "DELETE FROM action_items WHERE meeting_id=?", (meeting_id,)
            )
            self._conn.execute(
                "DELETE FROM meeting_facts WHERE meeting_id=?", (meeting_id,)
            )
            for item in result.action_items:
                self._conn.execute(
                    "INSERT INTO action_items"
                    " (meeting_id, what, where_, details, requested_by, priority, status, due,"
                    "  assigned_to, source_start, source_end, evidence_quote, explicitness, review_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        meeting_id, item.what, item.where, item.details,
                        item.requested_by, item.priority, item.status, item.due,
                        json.dumps(item.assigned_to) if item.assigned_to is not None else None,
                        item.source_start, item.source_end, item.evidence_quote,
                        item.explicitness, item.review_status,
                    ),
                )
            for fact in result.facts:
                self._conn.execute(
                    "INSERT INTO meeting_facts"
                    " (meeting_id, kind, text, source_start, source_end,"
                    "  evidence_quote, explicitness, review_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (meeting_id, fact.kind, fact.text, fact.source_start,
                     fact.source_end, fact.evidence_quote, fact.explicitness,
                     fact.review_status),
                )
            for seg in result.segments:
                self._conn.execute(
                    "INSERT INTO segments (meeting_id, start, end, speaker, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (meeting_id, seg.start, seg.end, seg.speaker, seg.text),
                )
            self._conn.execute(
                "DELETE FROM search_index WHERE meeting_id=?", (meeting_id,)
            )
            self._index_meeting(meeting_id, result.segments, result.action_items, result.facts)

    def update_meeting_extract(
        self,
        meeting_id: int,
        summary: str,
        action_items: list[ActionItem],
        title: str | None,
        facts: list[MeetingFact] | None = None,
    ) -> None:
        """Atualiza summary, action_items e facts; NÃO sobrescreve title existente. Atômico."""
        with self._conn:
            self._conn.execute(
                "UPDATE meetings SET summary=?, updated_at=? WHERE id=?",
                (summary, _now(), meeting_id),
            )
            self._conn.execute(
                "DELETE FROM action_items WHERE meeting_id=?", (meeting_id,)
            )
            self._conn.execute(
                "DELETE FROM meeting_facts WHERE meeting_id=?", (meeting_id,)
            )
            for item in action_items:
                self._conn.execute(
                    "INSERT INTO action_items"
                    " (meeting_id, what, where_, details, requested_by, priority, status, due,"
                    "  assigned_to, source_start, source_end, evidence_quote, explicitness, review_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        meeting_id, item.what, item.where, item.details,
                        item.requested_by, item.priority, item.status, item.due,
                        json.dumps(item.assigned_to) if item.assigned_to is not None else None,
                        item.source_start, item.source_end, item.evidence_quote,
                        item.explicitness, item.review_status,
                    ),
                )
            for fact in (facts or []):
                self._conn.execute(
                    "INSERT INTO meeting_facts"
                    " (meeting_id, kind, text, source_start, source_end,"
                    "  evidence_quote, explicitness, review_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (meeting_id, fact.kind, fact.text, fact.source_start,
                     fact.source_end, fact.evidence_quote, fact.explicitness,
                     fact.review_status),
                )
            # Reindex FTS: limpa e reinsere com segments do banco + novos action_items
            self._conn.execute(
                "DELETE FROM search_index WHERE meeting_id=?", (meeting_id,)
            )
            self._reindex_meeting_fts(meeting_id)


    def update_speaker(self, meeting_id: int, old: str, new: str) -> None:
        """Renomeia falante em segments e reindexa FTS da reunião."""
        with self._conn:
            self._conn.execute(
                "UPDATE segments SET speaker = ? WHERE meeting_id = ? AND speaker = ?",
                (new, meeting_id, old),
            )
            self._conn.execute(
                "DELETE FROM search_index WHERE meeting_id = ?",
                (meeting_id,),
            )
            segs = [
                TranscriptSegment(
                    start=r["start"],
                    end=r["end"],
                    text=r["text"],
                    speaker=r["speaker"],
                )
                for r in self._conn.execute(
                    "SELECT start, end, speaker, text FROM segments WHERE meeting_id = ?",
                    (meeting_id,),
                )
            ]
            items = [
                ActionItem(
                    what=r["what"],
                    where=r["where_"],
                    details=r["details"],
                    requested_by=r["requested_by"],
                    priority=r["priority"],
                )
                for r in self._conn.execute(
                    "SELECT what, where_, details, requested_by, priority"
                    " FROM action_items WHERE meeting_id = ?",
                    (meeting_id,),
                )
            ]
            self._index_meeting(meeting_id, segs, items)
            self._conn.execute(
                "UPDATE meetings SET updated_at = ? WHERE id = ?",
                (_now(), meeting_id),
            )

    # ------------------------------------------------------------------
    # Banco de vozes
    # ------------------------------------------------------------------

    def get_voice(self, name: str) -> bytes | None:
        """Retorna embedding bytes ou None se não encontrado."""
        row = self._conn.execute(
            "SELECT embedding FROM voices WHERE name = ?", (name,)
        ).fetchone()
        return bytes(row["embedding"]) if row else None

    def all_voices(self) -> dict[str, bytes]:
        """Retorna todos os embeddings como dict nome → bytes."""
        rows = self._conn.execute("SELECT name, embedding FROM voices").fetchall()
        return {r["name"]: bytes(r["embedding"]) for r in rows}

    def upsert_voice(self, name: str, blob: bytes) -> None:
        """Insere ou substitui embedding de voz."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO voices (name, embedding) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET embedding = excluded.embedding",
                (name, blob),
            )

    def delete_voice(self, name: str) -> None:
        """Remove voz do banco."""
        with self._conn:
            self._conn.execute("DELETE FROM voices WHERE name = ?", (name,))

    def rename_voice(self, old: str, new: str) -> None:
        """Renomeia ou funde voz no banco.

        - old == new: noop.
        - new não existe: move embedding de old para new; remove old.
        - new já existe (merge): média (emb_old + emb_new)/2 → new; remove old.
        Em ambos os casos: UPDATE segments SET speaker=new WHERE speaker=old em todas
        as reuniões; reindexar FTS; tudo atômico. Regen .md após commit.
        """
        if old == new:
            return
        import numpy as np

        def _from_blob(blob: bytes) -> np.ndarray:
            return np.frombuffer(blob, dtype=np.float32).copy()

        def _to_blob(v: np.ndarray) -> bytes:
            return np.asarray(v, dtype=np.float32).tobytes()

        old_row = self._conn.execute(
            "SELECT embedding FROM voices WHERE name = ?", (old,)
        ).fetchone()
        if old_row is None:
            return  # old não existe — noop
        old_blob = bytes(old_row["embedding"])

        new_row = self._conn.execute(
            "SELECT embedding FROM voices WHERE name = ?", (new,)
        ).fetchone()

        if new_row is not None:
            # merge: média dos embeddings
            merged = (_from_blob(old_blob) + _from_blob(bytes(new_row["embedding"]))) / 2.0
            new_blob = _to_blob(merged)
        else:
            new_blob = old_blob

        # Reuniões afetadas (para regen .md após commit)
        affected_rows = self._conn.execute(
            "SELECT DISTINCT meeting_id FROM segments WHERE speaker = ?", (old,)
        ).fetchall()
        affected_ids = [r["meeting_id"] for r in affected_rows]

        with self._conn:
            # Upsert do embedding new
            self._conn.execute(
                "INSERT INTO voices (name, embedding) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET embedding = excluded.embedding",
                (new, new_blob),
            )
            # Remove old
            self._conn.execute("DELETE FROM voices WHERE name = ?", (old,))
            # Atualizar segments
            self._conn.execute(
                "UPDATE segments SET speaker = ? WHERE speaker = ?", (new, old)
            )
            # Reindexar FTS para reuniões afetadas
            for mid in affected_ids:
                self._conn.execute(
                    "DELETE FROM search_index WHERE meeting_id = ?", (mid,)
                )
                self._reindex_meeting_fts(mid)
                self._conn.execute(
                    "UPDATE meetings SET updated_at = ? WHERE id = ?", (_now(), mid)
                )

        # Regen .md (I/O fora da transação)
        for mid in affected_ids:
            self._regen_md(mid)

    def voice_usage(self, name: str) -> list[dict]:
        """Reuniões que contêm segmentos do falante name.

        Retorna [{meeting_id, title, date, count}] ordenado por date desc.
        """
        rows = self._conn.execute(
            "SELECT s.meeting_id, m.title, m.date, COUNT(*) AS count"
            " FROM segments s"
            " JOIN meetings m ON m.id = s.meeting_id"
            " WHERE s.speaker = ?"
            " GROUP BY s.meeting_id"
            " ORDER BY m.date DESC, s.meeting_id DESC",
            (name,),
        ).fetchall()
        return [
            {
                "meeting_id": r["meeting_id"],
                "title": r["title"],
                "date": r["date"],
                "count": r["count"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Projetos
    # ------------------------------------------------------------------

    def _project_row(self, row: sqlite3.Row) -> ProjectRow:
        return ProjectRow(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            repo_path=row["repo_path"] or "",
            meeting_count=row["meeting_count"] or 0,
            open_task_count=row["open_task_count"] or 0,
            done_task_count=row["done_task_count"] or 0,
            last_meeting_date=row["last_meeting_date"],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    # Personal-list semantics: same filter as list_tasks (me or no owner)
    _PROJECT_STATS_SQL = (
        "SELECT p.id, p.name, p.description, p.repo_path, p.created_at, p.updated_at,"
        "       COUNT(DISTINCT m.id) AS meeting_count,"
        "       COUNT(DISTINCT CASE WHEN ai.status = 'aberto'"
        "           AND (ai.assigned_to IS NULL OR EXISTS ("
        "               SELECT 1 FROM json_each(ai.assigned_to) owner"
        "               WHERE lower(owner.value) = 'me'))"
        "           THEN ai.id END) AS open_task_count,"
        "       COUNT(DISTINCT CASE WHEN ai.status = 'feito'"
        "           AND (ai.assigned_to IS NULL OR EXISTS ("
        "               SELECT 1 FROM json_each(ai.assigned_to) owner"
        "               WHERE lower(owner.value) = 'me'))"
        "           THEN ai.id END) AS done_task_count,"
        "       MAX(m.date) AS last_meeting_date"
        " FROM projects p"
        " LEFT JOIN meetings m ON m.project_id = p.id"
        " LEFT JOIN action_items ai ON ai.meeting_id = m.id"
    )

    def create_project(
        self,
        name: str,
        description: str = "",
        repo_path: str = "",
    ) -> int:
        """Cria projeto; ValueError se nome duplicado (case-insensitive)."""
        name = name.strip()
        if not name:
            raise ValueError("Nome do projeto não pode ser vazio")
        now = _now()
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO projects (name, description, repo_path, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, description.strip(), repo_path.strip(), now, now),
                )
                return cur.lastrowid  # type: ignore[return-value]
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError(f"Projeto '{name}' já existe") from exc
            raise

    def get_project(self, project_id: int) -> ProjectRow | None:
        """Retorna projeto com contagens ou None."""
        row = self._conn.execute(
            self._PROJECT_STATS_SQL + " WHERE p.id = ? GROUP BY p.id",
            (project_id,),
        ).fetchone()
        return self._project_row(row) if row else None

    def list_projects(self) -> list[ProjectRow]:
        """Lista todos os projetos com contagens, ordenados por nome."""
        rows = self._conn.execute(
            self._PROJECT_STATS_SQL + " GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
        ).fetchall()
        return [self._project_row(r) for r in rows]

    def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        repo_path: str | None = None,
    ) -> bool:
        """Atualiza campos do projeto; retorna False se não existe. ValueError em nome duplicado."""
        if name is None and description is None and repo_path is None:
            return self.get_project(project_id) is not None
        fields: dict = {}
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Nome do projeto não pode ser vazio")
            fields["name"] = name
        if description is not None:
            fields["description"] = description.strip()
        if repo_path is not None:
            fields["repo_path"] = repo_path.strip()
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [project_id]
        try:
            with self._conn:
                cur = self._conn.execute(
                    f"UPDATE projects SET {set_clause} WHERE id = ?", vals
                )
                return cur.rowcount > 0
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError(f"Projeto '{name}' já existe") from exc
            raise

    def delete_project(self, project_id: int) -> bool:
        """Apaga projeto; desassocia reuniões (project_id = NULL). Nunca apaga reuniões."""
        row = self._conn.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return False
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE meetings SET project_id = NULL, updated_at = ?"
                " WHERE project_id = ?",
                (now, project_id),
            )
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return True

    def set_meeting_project(self, meeting_id: int, project_id: int | None) -> bool:
        """Associa ou desassocia reunião de um projeto; retorna False se reunião não existe."""
        row = self._conn.execute(
            "SELECT id FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if row is None:
            return False
        if project_id is not None:
            proj = self._conn.execute(
                "SELECT id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if proj is None:
                raise ValueError(f"Projeto {project_id} não encontrado")
        with self._conn:
            self._conn.execute(
                "UPDATE meetings SET project_id = ?, updated_at = ? WHERE id = ?",
                (project_id, _now(), meeting_id),
            )
        return True

    def bulk_set_meeting_project(
        self,
        meeting_ids: list[int],
        project_id: int | None,
    ) -> int:
        """Associa/desassocia várias reuniões; retorna número de linhas afetadas."""
        if not meeting_ids:
            return 0
        if project_id is not None:
            proj = self._conn.execute(
                "SELECT id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if proj is None:
                raise ValueError(f"Projeto {project_id} não encontrado")
        ph = ",".join("?" * len(meeting_ids))
        now = _now()
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE meetings SET project_id = ?, updated_at = ? WHERE id IN ({ph})",
                [project_id, now, *meeting_ids],
            )
        return cur.rowcount

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _index_meeting(
        self,
        meeting_id: int,
        segments: list[TranscriptSegment],
        action_items: list[ActionItem],
        facts: list[MeetingFact] | None = None,
    ) -> None:
        """Insere registros FTS para segments, action items e facts."""
        for seg in segments:
            self._conn.execute(
                "INSERT INTO search_index (content, meeting_id, kind) VALUES (?, ?, ?)",
                (seg.text, meeting_id, "segment"),
            )
        for item in action_items:
            content = " ".join(filter(None, [item.what, item.where, item.details]))
            self._conn.execute(
                "INSERT INTO search_index (content, meeting_id, kind) VALUES (?, ?, ?)",
                (content, meeting_id, "action_item"),
            )
        for fact in (facts or []):
            self._conn.execute(
                "INSERT INTO search_index (content, meeting_id, kind) VALUES (?, ?, ?)",
                (fact.text, meeting_id, "fact"),
            )

    def _reindex_meeting_fts(self, meeting_id: int) -> None:
        """Recarrega segments+action_items+facts do banco e reindexa FTS."""
        segs = [
            TranscriptSegment(
                start=r["start"],
                end=r["end"],
                text=r["text"],
                speaker=r["speaker"],
            )
            for r in self._conn.execute(
                "SELECT start, end, speaker, text FROM segments WHERE meeting_id = ?",
                (meeting_id,),
            )
        ]
        items = [
            ActionItem(
                what=r["what"],
                where=r["where_"],
                details=r["details"],
                requested_by=r["requested_by"],
                priority=r["priority"],
                status=r["status"],
                due=r["due"],
            )
            for r in self._conn.execute(
                "SELECT what, where_, details, requested_by, priority, status, due"
                " FROM action_items WHERE meeting_id = ?",
                (meeting_id,),
            )
        ]
        facts = [
            MeetingFact(kind=r["kind"], text=r["text"])
            for r in self._conn.execute(
                "SELECT kind, text FROM meeting_facts WHERE meeting_id = ?",
                (meeting_id,),
            )
        ]
        self._index_meeting(meeting_id, segs, items, facts)

    def _regen_md(self, meeting_id: int) -> None:
        """Regenera o arquivo .md da reunião a partir do banco."""
        from . import render as render_mod

        result = self.get_meeting(meeting_id)
        if result is None:
            return
        md_path = getattr(result, "md_path", None)
        if not md_path:
            return
        Path(md_path).write_text(render_mod.to_markdown(result), encoding="utf-8")
