"""Persistência SQLite para o pipeline de reuniões."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import ActionItem, MeetingResult, TranscriptSegment


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
    priority     TEXT    NOT NULL DEFAULT 'media'
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
"""

# Colunas novas (migração idempotente via PRAGMA table_info)
_MEETING_EXTRA_COLS: list[tuple[str, str]] = [
    ("source_origin", "TEXT NOT NULL DEFAULT ''"),
    ("media_managed", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
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


class Store:
    """Banco de dados de reuniões (sqlite3, WAL)."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(meetings)").fetchall()
        }
        with self._conn:
            for name, decl in _MEETING_EXTRA_COLS:
                if name not in cols:
                    self._conn.execute(
                        f"ALTER TABLE meetings ADD COLUMN {name} {decl}"
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

    # ------------------------------------------------------------------
    # Reuniões
    # ------------------------------------------------------------------

    def save_meeting(self, result: MeetingResult, md_path: Path) -> int:
        """Persiste reunião completa; retorna o id gerado."""
        now = _now()
        origin = result.source
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO meetings"
                " (date, title, source, duration, summary, md_path,"
                "  source_origin, media_managed, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
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
                ),
            )
            meeting_id: int = cur.lastrowid  # type: ignore[assignment]

            for item in result.action_items:
                self._conn.execute(
                    "INSERT INTO action_items"
                    " (meeting_id, what, where_, details, requested_by, priority)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        meeting_id,
                        item.what,
                        item.where,
                        item.details,
                        item.requested_by,
                        item.priority,
                    ),
                )

            for seg in result.segments:
                self._conn.execute(
                    "INSERT INTO segments (meeting_id, start, end, speaker, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (meeting_id, seg.start, seg.end, seg.speaker, seg.text),
                )

            self._index_meeting(meeting_id, result.segments, result.action_items)

        return meeting_id

    def get_meeting(self, meeting_id: int) -> MeetingResult | None:
        """Reconstrói MeetingResult; anexa attrs de mídia/md_path."""
        row = self._conn.execute(
            "SELECT id, date, title, source, duration, summary, md_path,"
            "       source_origin, media_managed, created_at, updated_at"
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
            )
            for r in self._conn.execute(
                "SELECT start, end, speaker, text FROM segments"
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
            )
            for r in self._conn.execute(
                "SELECT what, where_, details, requested_by, priority"
                " FROM action_items WHERE meeting_id = ?",
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
            segments=segments,
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

    def list_meeting_rows(self) -> list[MeetingRow]:
        """Listagem rica com status de mídia."""
        rows = self._conn.execute(
            "SELECT id, date, title, source, source_origin, media_managed, duration"
            " FROM meetings ORDER BY date DESC, id DESC"
        ).fetchall()
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
                )
            )
        return out

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 full-text search; retorna dicts com meeting_id, date, title, kind, snippet."""
        sql = """\
            SELECT si.meeting_id,
                   m.date,
                   m.title,
                   si.kind,
                   snippet(search_index, 0, '[', ']', '...', 10) AS snippet
            FROM   search_index si
            JOIN   meetings m ON m.id = CAST(si.meeting_id AS INTEGER)
            WHERE  search_index MATCH ?
            ORDER  BY rank
            LIMIT  ?
        """
        rows = self._conn.execute(sql, (query, limit)).fetchall()
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

    def adopt_media(self, meeting_id: int, data_dir: Path, origin: Path) -> Path:
        """Importa origin para media/{id}/ e atualiza o registro."""
        from . import media as media_mod

        origin = Path(origin).expanduser()
        dest = media_mod.import_original(data_dir, meeting_id, origin)
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

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _index_meeting(
        self,
        meeting_id: int,
        segments: list[TranscriptSegment],
        action_items: list[ActionItem],
    ) -> None:
        """Insere registros FTS para segments e action items de uma reunião."""
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
