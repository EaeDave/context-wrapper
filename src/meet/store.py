"""Persistência SQLite para o pipeline de reuniões."""

from __future__ import annotations

import sqlite3
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


class Store:
    """Banco de dados de reuniões (sqlite3, WAL)."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Reuniões
    # ------------------------------------------------------------------

    def save_meeting(self, result: MeetingResult, md_path: Path) -> int:
        """Persiste reunião completa; retorna o id gerado."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO meetings (date, title, source, duration, summary, md_path)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    result.date,
                    result.title,
                    result.source,
                    result.duration,
                    result.summary,
                    str(md_path),
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
        """Reconstrói MeetingResult; anexa atributo `.md_path: Path` ao objeto."""
        row = self._conn.execute(
            "SELECT id, date, title, source, duration, summary, md_path"
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
        result.md_path = Path(row["md_path"])  # type: ignore[attr-defined]
        return result

    def list_meetings(self) -> list[tuple[int, str, str]]:
        """(id, date, title) de todas as reuniões, mais recentes primeiro."""
        rows = self._conn.execute(
            "SELECT id, date, title FROM meetings ORDER BY date DESC, id DESC"
        ).fetchall()
        return [(r["id"], r["date"], r["title"]) for r in rows]

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

    def update_speaker(self, meeting_id: int, old: str, new: str) -> None:
        """Renomeia falante em segments e reindexa FTS da reunião."""
        with self._conn:
            self._conn.execute(
                "UPDATE segments SET speaker = ? WHERE meeting_id = ? AND speaker = ?",
                (new, meeting_id, old),
            )
            # Remove e reindexa todos os registros FTS da reunião
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
