"""SQLite database layer for book metadata management."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    author         TEXT,
    format         TEXT,
    source_name    TEXT,
    source_hash    TEXT,
    cover_path     TEXT,
    phase          TEXT NOT NULL DEFAULT 'empty',
    page_count     INTEGER,
    chapter_count  INTEGER,
    description    TEXT,
    error_message  TEXT,
    prompt_tokens  INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens   INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id        INTEGER NOT NULL,
    book_title     TEXT,
    operation      TEXT NOT NULL,
    phase          TEXT,
    status         TEXT NOT NULL DEFAULT 'success',
    detail         TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oplogs_book_id ON operation_logs(book_id);
CREATE INDEX IF NOT EXISTS idx_oplogs_created_at ON operation_logs(created_at);

CREATE TABLE IF NOT EXISTS chat_conversations (
    id                       TEXT PRIMARY KEY,
    book_id                  INTEGER NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'active',
    generation_provider      TEXT NOT NULL,
    generation_model         TEXT NOT NULL,
    generation_max_tokens    INTEGER NOT NULL,
    generation_config_hash   TEXT NOT NULL,
    generation_config_json   TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_conversations_book ON chat_conversations(book_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_turns (
    id                       TEXT PRIMARY KEY,
    conversation_id          TEXT NOT NULL,
    turn_no                  INTEGER NOT NULL,
    question                 TEXT NOT NULL,
    answer                   TEXT,
    status                   TEXT NOT NULL DEFAULT 'queued',
    refusal_code             TEXT,
    error_code               TEXT,
    error_message            TEXT,
    citations_json           TEXT NOT NULL DEFAULT '[]',
    retrieval_json           TEXT NOT NULL DEFAULT '{}',
    prompt_hash              TEXT,
    provider_request_id      TEXT,
    prompt_tokens            INTEGER NOT NULL DEFAULT 0,
    completion_tokens        INTEGER NOT NULL DEFAULT 0,
    total_tokens             INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL,
    started_at               TEXT,
    finished_at              TEXT,
    FOREIGN KEY(conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE,
    UNIQUE(conversation_id, turn_no)
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_conversation ON chat_turns(conversation_id, turn_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_turns_one_active ON chat_turns(conversation_id)
    WHERE status IN ('queued', 'retrieving', 'generating');

CREATE TABLE IF NOT EXISTS chat_jobs (
    turn_id                  TEXT PRIMARY KEY,
    status                   TEXT NOT NULL DEFAULT 'queued',
    attempt                  INTEGER NOT NULL DEFAULT 0,
    claimed_by               TEXT,
    lease_until              TEXT,
    last_error               TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY(turn_id) REFERENCES chat_turns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_jobs_status ON chat_jobs(status, lease_until, created_at);
"""

_book_creation_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(db_path: str) -> sqlite3.Connection:
    """Create/open database, run schema, return connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.commit()
    return conn


def create_placeholder(
    conn: sqlite3.Connection,
    title: str,
    format: str | None = None,
    source_name: str | None = None,
    source_hash: str | None = None,
) -> int:
    """Reserve and return the next numeric book ID for an upload."""
    now = _now()
    with _book_creation_lock:
        cursor = conn.execute(
            """INSERT INTO books (title, format, source_name, source_hash, phase, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
            (title, format, source_name, source_hash, now, now),
        )
        conn.commit()
        return int(cursor.lastrowid)


def update_phase(
    conn: sqlite3.Connection,
    book_id: int,
    phase: str,
    error_message: str | None = None,
) -> None:
    """Update processing phase and optional error."""
    now = _now()
    conn.execute(
        "UPDATE books SET phase = ?, error_message = ?, updated_at = ? WHERE id = ?",
        (phase, error_message, now, book_id),
    )
    conn.commit()


def update_from_import(
    conn: sqlite3.Connection,
    book_id: int,
    source_json_path: Path,
) -> None:
    """Update book metadata from source.json after import completes."""
    if not source_json_path.is_file():
        return
    try:
        text = source_json_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return

    title = data.get("title") or data.get("source_name", "")
    author = data.get("author") or data.get("metadata", {}).get("author", "")
    source_format = data.get("format", "")
    source_name = data.get("source_name", "")
    source_hash = data.get("source_hash", "")
    now = _now()

    conn.execute(
        """UPDATE books SET title = ?, author = ?, format = ?, source_name = ?,
           source_hash = ?, updated_at = ? WHERE id = ?""",
        (title, author, source_format, source_name, source_hash, now, book_id),
    )

    # Count raw chapters
    chapters_dir = source_json_path.parent.parent / "raw" / "chapters"
    if chapters_dir.is_dir():
        chapter_count = len(list(chapters_dir.glob("*.md")))
        conn.execute(
            "UPDATE books SET chapter_count = ? WHERE id = ?",
            (chapter_count, book_id),
        )

    conn.commit()


def update_from_render(
    conn: sqlite3.Connection,
    book_id: int,
    structure_json_path: Path,
) -> None:
    """Update book metadata from structure.json after rendering completes."""
    if not structure_json_path.is_file():
        return
    try:
        text = structure_json_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return

    title = data.get("title", "")
    description = data.get("description", "")
    page_count = len(data.get("pages", []))
    now = _now()

    conn.execute(
        """UPDATE books SET title = CASE WHEN ? != '' THEN ? ELSE title END,
           description = ?, page_count = ?, updated_at = ? WHERE id = ?""",
        (title, title, description, page_count, now, book_id),
    )
    conn.commit()


def update_cover(
    conn: sqlite3.Connection,
    book_id: int,
    cover_relative_path: str,
) -> None:
    """Update cover image path."""
    now = _now()
    conn.execute(
        "UPDATE books SET cover_path = ?, updated_at = ? WHERE id = ?",
        (cover_relative_path, now, book_id),
    )
    conn.commit()


def list_books(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all non-empty books ordered by creation time descending."""
    rows = conn.execute(
        "SELECT * FROM books WHERE phase != 'empty' ORDER BY created_at DESC"
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_book(conn: sqlite3.Connection, book_id: int) -> dict[str, Any] | None:
    """Return a single book or None."""
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return _row_to_dict(row) if row else None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _conversation_id() -> str:
    """Return a URL-safe, non-sequential base62 identifier."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _turn_id() -> str:
    return uuid.uuid4().hex


def create_chat_conversation(
    conn: sqlite3.Connection,
    book_id: int,
    question: str,
    generation_config: dict[str, Any],
) -> tuple[str, str]:
    """Create a conversation, first queued turn, and durable job atomically."""
    now = _now()
    conversation_id = _conversation_id()
    turn_id = _turn_id()
    provider = str(generation_config["provider"])
    model = str(generation_config["model"])
    max_tokens = int(generation_config["max_output_tokens"])
    config_hash = str(generation_config["config_hash"])
    payload = json.dumps(generation_config, ensure_ascii=False, sort_keys=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone() is None:
            raise KeyError("book_not_found")
        conn.execute(
            """INSERT INTO chat_conversations
               (id, book_id, generation_provider, generation_model, generation_max_tokens,
                generation_config_hash, generation_config_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (conversation_id, book_id, provider, model, max_tokens, config_hash, payload, now, now),
        )
        conn.execute(
            """INSERT INTO chat_turns (id, conversation_id, turn_no, question, status, created_at)
               VALUES (?, ?, 1, ?, 'queued', ?)""",
            (turn_id, conversation_id, question, now),
        )
        conn.execute(
            "INSERT INTO chat_jobs (turn_id, status, created_at, updated_at) VALUES (?, 'queued', ?, ?)",
            (turn_id, now, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conversation_id, turn_id


def append_chat_turn(conn: sqlite3.Connection, conversation_id: str, question: str, max_turns: int) -> str:
    """Append a queued turn unless the conversation has a pending answer."""
    now = _now()
    turn_id = _turn_id()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conversation = conn.execute(
            "SELECT id FROM chat_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise KeyError("conversation_not_found")
        active = conn.execute(
            """SELECT 1 FROM chat_turns WHERE conversation_id = ?
               AND status IN ('queued', 'retrieving', 'generating')""",
            (conversation_id,),
        ).fetchone()
        if active is not None:
            raise RuntimeError("conversation_busy")
        current = conn.execute(
            "SELECT COUNT(*) FROM chat_turns WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()[0]
        if current >= max_turns:
            raise OverflowError("conversation_turn_limit")
        turn_no = int(current) + 1
        conn.execute(
            """INSERT INTO chat_turns (id, conversation_id, turn_no, question, status, created_at)
               VALUES (?, ?, ?, ?, 'queued', ?)""",
            (turn_id, conversation_id, turn_no, question, now),
        )
        conn.execute(
            "INSERT INTO chat_jobs (turn_id, status, created_at, updated_at) VALUES (?, 'queued', ?, ?)",
            (turn_id, now, now),
        )
        conn.execute(
            "UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return turn_id


def get_chat_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any] | None:
    """Return a public conversation only when the full opaque ID is known."""
    row = conn.execute(
        """SELECT c.*, b.title AS book_title, b.phase AS book_phase
           FROM chat_conversations c JOIN books b ON b.id = c.book_id WHERE c.id = ?""",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    conversation = _row_to_dict(row)
    conversation.pop("generation_config_json", None)
    turns = conn.execute(
        """SELECT id, turn_no, question, answer, status, refusal_code, error_code,
                  error_message, citations_json, created_at, started_at, finished_at
           FROM chat_turns WHERE conversation_id = ? ORDER BY turn_no""",
        (conversation_id,),
    ).fetchall()
    conversation["turns"] = []
    for turn in turns:
        value = _row_to_dict(turn)
        try:
            value["citations"] = json.loads(value.pop("citations_json", "[]"))
        except (TypeError, ValueError):
            value["citations"] = []
        conversation["turns"].append(value)
    return conversation


def claim_chat_job(conn: sqlite3.Connection, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
    """Atomically claim one queued job, reclaiming expired worker leases first."""
    now = _now()
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + lease_seconds))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE chat_turns SET status = 'queued', started_at = NULL
               WHERE id IN (
                 SELECT turn_id FROM chat_jobs
                 WHERE status = 'claimed' AND lease_until IS NOT NULL AND lease_until < ?
               ) AND status IN ('retrieving', 'generating')""",
            (now,),
        )
        conn.execute(
            """UPDATE chat_jobs SET status = 'queued', claimed_by = NULL, lease_until = NULL,
               updated_at = ? WHERE status = 'claimed' AND lease_until IS NOT NULL AND lease_until < ?""",
            (now, now),
        )
        row = conn.execute(
            """SELECT j.turn_id FROM chat_jobs j JOIN chat_turns t ON t.id = j.turn_id
               WHERE j.status = 'queued' AND t.status = 'queued'
               ORDER BY j.created_at LIMIT 1"""
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        turn_id = str(row["turn_id"])
        cursor = conn.execute(
            """UPDATE chat_jobs SET status = 'claimed', attempt = attempt + 1, claimed_by = ?,
               lease_until = ?, updated_at = ? WHERE turn_id = ? AND status = 'queued'""",
            (worker_id, expires, now, turn_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE chat_turns SET status = 'retrieving', started_at = ? WHERE id = ? AND status = 'queued'",
            (now, turn_id),
        )
        job = conn.execute(
            """SELECT t.*, j.attempt, j.claimed_by, j.lease_until,
                      c.book_id, c.generation_provider, c.generation_model,
                      c.generation_max_tokens, c.generation_config_hash, c.generation_config_json
               FROM chat_turns t JOIN chat_jobs j ON j.turn_id = t.id
               JOIN chat_conversations c ON c.id = t.conversation_id
               WHERE t.id = ?""",
            (turn_id,),
        ).fetchone()
        conn.commit()
        return _row_to_dict(job) if job else None
    except Exception:
        conn.rollback()
        raise


def complete_chat_turn(
    conn: sqlite3.Connection,
    turn_id: str,
    *,
    status: str,
    answer: str | None = None,
    refusal_code: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    retrieval: dict[str, Any] | None = None,
    prompt_hash: str | None = None,
    provider_request_id: str | None = None,
    usage: dict[str, int] | None = None,
    claimed_by: str | None = None,
) -> bool:
    """Write a terminal chat result without recreating a deleted turn."""
    if status not in {"succeeded", "refused", "failed"}:
        raise ValueError(f"invalid terminal chat status: {status}")
    now = _now()
    usage = usage or {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT conversation_id FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        if existing is None:
            conn.rollback()
            return False
        if claimed_by is not None:
            owned = conn.execute(
                "SELECT 1 FROM chat_jobs WHERE turn_id = ? AND status = 'claimed' AND claimed_by = ?",
                (turn_id, claimed_by),
            ).fetchone()
            if owned is None:
                conn.rollback()
                return False
        conn.execute(
            """UPDATE chat_turns SET answer = ?, status = ?, refusal_code = ?, error_code = ?,

               error_message = ?, citations_json = ?, retrieval_json = ?, prompt_hash = ?,
               provider_request_id = ?, prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
               finished_at = ? WHERE id = ?""",
            (
                answer, status, refusal_code, error_code, error_message,
                json.dumps(citations or [], ensure_ascii=False), json.dumps(retrieval or {}, ensure_ascii=False),
                prompt_hash, provider_request_id, int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)), int(usage.get("total_tokens", 0)), now, turn_id,
            ),
        )
        conn.execute(
            "UPDATE chat_jobs SET status = 'done', lease_until = NULL, updated_at = ? WHERE turn_id = ?",
            (now, turn_id),
        )
        conn.execute(
            "UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (now, existing["conversation_id"]),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def mark_chat_turn_generating(conn: sqlite3.Connection, turn_id: str, claimed_by: str) -> bool:
    """Advance a turn only while the requesting worker still owns its lease."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        owned = conn.execute(
            "SELECT 1 FROM chat_jobs WHERE turn_id = ? AND status = 'claimed' AND claimed_by = ?",
            (turn_id, claimed_by),
        ).fetchone()
        if owned is None:
            conn.rollback()
            return False
        cursor = conn.execute(
            "UPDATE chat_turns SET status = 'generating' WHERE id = ? AND status = 'retrieving'", (turn_id,)
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def reset_stuck_chat_jobs(conn: sqlite3.Connection) -> int:
    """Make expired chat leases eligible after a worker crash or restart."""
    now = _now()
    cursor = conn.execute(
        """UPDATE chat_jobs SET status = 'queued', claimed_by = NULL, lease_until = NULL, updated_at = ?
           WHERE status = 'claimed' AND lease_until IS NOT NULL AND lease_until < ?""",
        (now, now),
    )
    conn.execute(
        """UPDATE chat_turns SET status = 'queued', started_at = NULL
           WHERE status IN ('retrieving', 'generating') AND id IN (
             SELECT turn_id FROM chat_jobs WHERE status = 'queued'
           )"""
    )
    conn.commit()
    return cursor.rowcount


def list_chat_audit(
    conn: sqlite3.Connection,
    *,
    book_id: int | None = None,
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return administrator-only persisted question/audit records."""
    filters: list[str] = []
    values: list[Any] = []
    if book_id is not None:
        filters.append("c.book_id = ?")
        values.append(book_id)
    if query:
        filters.append("t.question LIKE ?")
        values.append(f"%{query}%")
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    sql = (
        "SELECT t.id AS turn_id, t.turn_no, t.question, t.status, t.refusal_code, t.error_code, "
        "t.created_at, t.finished_at, c.id AS conversation_id, c.book_id, b.title AS book_title "
        "FROM chat_turns t JOIN chat_conversations c ON c.id = t.conversation_id "
        "JOIN books b ON b.id = c.book_id" + where
    )
    rows = conn.execute(sql + " ORDER BY t.created_at DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM (" + sql + ")", values).fetchone()[0]
    return [_row_to_dict(row) for row in rows], int(total)


def reset_stuck_books(conn: sqlite3.Connection) -> int:
    """Reset books stuck in intermediate processing phases after a crash/force-stop.

    Phases like 'importing', 'indexing', 'generating', 'rendering' indicate the
    pipeline was interrupted mid-run.  Reset them to 'failed' so the user can
    retry via the /api/books/{id}/process endpoint.
    Returns the number of books that were reset.
    """
    stuck_phases = ("importing", "indexing", "generating", "rendering")
    placeholders = ", ".join("?" for _ in stuck_phases)
    now = _now()
    cursor = conn.execute(
        f"UPDATE books SET phase = 'failed',"
        f" error_message = 'Server was interrupted during processing — please retry',"
        f" updated_at = ?"
        f" WHERE phase IN ({placeholders})",
        (now, *stuck_phases),
    )
    conn.commit()
    return cursor.rowcount



# ── Operation Logs ────────────────────────────────────────────────────────────


def insert_operation_log(
    conn: sqlite3.Connection,
    book_id: int,
    operation: str,
    status: str = "success",
    phase: str | None = None,
    detail: str | None = None,
    book_title: str | None = None,
) -> None:
    """Record an operation event."""
    now = _now()
    conn.execute(
        """INSERT INTO operation_logs (book_id, book_title, operation, phase, status, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (book_id, book_title, operation, phase, status, detail, now),
    )
    conn.commit()


def list_operation_logs(
    conn: sqlite3.Connection,
    book_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return paginated operation logs, optionally filtered by book_id."""
    if book_id:
        rows = conn.execute(
            "SELECT * FROM operation_logs WHERE book_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (book_id, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM operation_logs WHERE book_id = ?", (book_id,)
        ).fetchone()[0]
    else:
        rows = conn.execute(
            "SELECT * FROM operation_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
    return ([_row_to_dict(row) for row in rows], total)


# ── Admin Mutations ───────────────────────────────────────────────────────────


def delete_book(conn: sqlite3.Connection, book_id: int) -> bool:
    """Delete a book and cascade its persistent conversations, turns, and jobs."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        conn.rollback()
        raise


def update_book_metadata(
    conn: sqlite3.Connection,
    book_id: int,
    title: str | None = None,
    author: str | None = None,
    description: str | None = None,
) -> bool:
    """Update editable metadata fields. Returns True if a row was updated."""
    now = _now()
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if author is not None:
        fields.append("author = ?")
        values.append(author)
    if description is not None:
        fields.append("description = ?")
        values.append(description)
    if not fields:
        return False
    fields.append("updated_at = ?")
    values.append(now)
    values.append(book_id)
    query = f"UPDATE books SET {', '.join(fields)} WHERE id = ?"
    cursor = conn.execute(query, values)
    conn.commit()
    return cursor.rowcount > 0


# ── Admin Stats ───────────────────────────────────────────────────────────────


def get_admin_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return dashboard summary statistics."""
    total = conn.execute(
        "SELECT COUNT(*) FROM books WHERE phase != 'empty'"
    ).fetchone()[0]
    complete = conn.execute(
        "SELECT COUNT(*) FROM books WHERE phase = 'complete'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM books WHERE phase = 'failed'"
    ).fetchone()[0]
    processing = conn.execute(
        "SELECT COUNT(*) FROM books WHERE phase IN ('queued','importing','indexing','generating','rendering')"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM books WHERE phase = 'pending'"
    ).fetchone()[0]
    total_pages = conn.execute(
        "SELECT COALESCE(SUM(page_count), 0) FROM books"
    ).fetchone()[0]
    total_chapters = conn.execute(
        "SELECT COALESCE(SUM(chapter_count), 0) FROM books"
    ).fetchone()[0]
    total_tokens = conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0) FROM books"
    ).fetchone()[0]
    recent_ops = conn.execute(
        "SELECT COUNT(*) FROM operation_logs WHERE created_at >= datetime('now', '-24 hours')"
    ).fetchone()[0]
    return {
        "total_books": total,
        "complete_books": complete,
        "failed_books": failed,
        "processing_books": processing,
        "pending_books": pending,
        "total_pages": total_pages,
        "total_chapters": total_chapters,
        "total_tokens": total_tokens,
        "recent_operations_24h": recent_ops,
    }


# ── Token Usage ────────────────────────────────────────────────────────────────


def refresh_token_usage(conn: sqlite3.Connection, books_root: Path, book_id: int | None = None) -> int:
    """Refresh token aggregates from usage.jsonl without requiring a restart."""
    rows = conn.execute(
        "SELECT id, prompt_tokens, completion_tokens, total_tokens FROM books WHERE phase != 'empty'"
        + (" AND id = ?" if book_id is not None else ""),
        (book_id,) if book_id is not None else (),
    ).fetchall()
    updated = 0
    for row in rows:
        current_id = int(row["id"])
        usage_file = books_root / str(current_id) / ".onebookwiki" / "usage.jsonl"
        prompt = completion = total = 0
        if usage_file.is_file():
            try:
                for line in usage_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            continue
                        prompt += int(entry.get("prompt_tokens", 0) or 0)
                        completion += int(entry.get("completion_tokens", 0) or 0)
                        total += int(entry.get("total_tokens", 0) or 0)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
            except OSError:
                continue
        previous = (
            int(row["prompt_tokens"] or 0),
            int(row["completion_tokens"] or 0),
            int(row["total_tokens"] or 0),
        )
        if (prompt, completion, total) == previous:
            continue
        conn.execute(
            "UPDATE books SET prompt_tokens = ?, completion_tokens = ?, total_tokens = ?, updated_at = ? WHERE id = ?",
            (prompt, completion, total, _now(), current_id),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated
