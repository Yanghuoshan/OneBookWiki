"""SQLite database layer for book metadata management."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id             TEXT PRIMARY KEY,
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
    book_id        TEXT NOT NULL,
    book_title     TEXT,
    operation      TEXT NOT NULL,
    phase          TEXT,
    status         TEXT NOT NULL DEFAULT 'success',
    detail         TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oplogs_book_id ON operation_logs(book_id);
CREATE INDEX IF NOT EXISTS idx_oplogs_created_at ON operation_logs(created_at);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(db_path: str) -> sqlite3.Connection:
    """Create/open database, run schema, return connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    # Migrate: add token columns if they don't exist (for databases created before v0.2)
    for col in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            conn.execute(f"ALTER TABLE books ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.commit()
    return conn


def insert_placeholder(
    conn: sqlite3.Connection,
    book_id: str,
    title: str,
    format: str | None = None,
    source_name: str | None = None,
    source_hash: str | None = None,
) -> None:
    """Insert a queued placeholder record after upload."""
    now = _now()
    conn.execute(
        """INSERT OR REPLACE INTO books (id, title, format, source_name, source_hash, phase, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (book_id, title, format, source_name, source_hash, now, now),
    )
    conn.commit()


def update_phase(
    conn: sqlite3.Connection,
    book_id: str,
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
    book_id: str,
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
    book_id: str,
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
    book_id: str,
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


def get_book(conn: sqlite3.Connection, book_id: str) -> dict[str, Any] | None:
    """Return a single book or None."""
    row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    return _row_to_dict(row) if row else None


def book_id_exists(conn: sqlite3.Connection, book_id: str) -> bool:
    """Check if a book id already exists in the database."""
    row = conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone()
    return row is not None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


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


def migrate_existing_books(conn: sqlite3.Connection, books_root: Path) -> int:
    """Scan books/ directory and write existing books into DB. Returns count."""
    if not books_root.is_dir():
        return 0

    count = 0
    now = _now()

    for book_dir in sorted(books_root.iterdir()):
        if not book_dir.is_dir():
            continue
        book_id = book_dir.name
        if book_id == "onebookwiki.db":
            continue

        # Skip if already in DB
        existing = conn.execute(
            "SELECT id FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if existing:
            continue

        structure = book_dir / "wiki" / "structure.json"
        source = book_dir / ".onebookwiki" / "source.json"
        cover = book_dir / ".onebookwiki" / "cover.jpg"
        raw_chapters = book_dir / "raw" / "chapters"

        if structure.is_file():
            # Complete book
            try:
                data = json.loads(structure.read_text(encoding="utf-8"))
                title = data.get("title", book_id)
                description = data.get("description", "")
                page_count = len(data.get("pages", []))
            except (json.JSONDecodeError, OSError):
                title = book_id
                description = ""
                page_count = 0

            chapter_count = (
                len(list(raw_chapters.glob("*.md"))) if raw_chapters.is_dir() else 0
            )
            cover_path = ".onebookwiki/cover.jpg" if cover.is_file() else None

            conn.execute(
                """INSERT INTO books (id, title, description, phase, page_count, chapter_count,
                   cover_path, created_at, updated_at)
                   VALUES (?, ?, ?, 'complete', ?, ?, ?, ?, ?)""",
                (book_id, title, description, page_count, chapter_count, cover_path, now, now),
            )
        elif source.is_file() and raw_chapters.is_dir():
            # Pending book (imported but not yet generated)
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                title = data.get("title") or data.get("source_name", book_id)
                author = data.get("author", "")
                fmt = data.get("format", "")
                source_name = data.get("source_name", "")
                source_hash = data.get("source_hash", "")
            except (json.JSONDecodeError, OSError):
                title = book_id
                author = ""
                fmt = ""
                source_name = ""
                source_hash = ""

            chapter_count = (
                len(list(raw_chapters.glob("*.md"))) if raw_chapters.is_dir() else 0
            )

            conn.execute(
                """INSERT INTO books (id, title, author, format, source_name, source_hash,
                   phase, chapter_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (book_id, title, author, fmt, source_name, source_hash, chapter_count, now, now),
            )
        else:
            continue

        count += 1

    conn.commit()
    return count


# ── Operation Logs ────────────────────────────────────────────────────────────


def insert_operation_log(
    conn: sqlite3.Connection,
    book_id: str,
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
    book_id: str | None = None,
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


def delete_book(conn: sqlite3.Connection, book_id: str) -> bool:
    """Delete a book record. Returns True if a row was deleted."""
    cursor = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.commit()
    return cursor.rowcount > 0


def update_book_metadata(
    conn: sqlite3.Connection,
    book_id: str,
    title: str | None = None,
    author: str | None = None,
    description: str | None = None,
) -> bool:
    """Update editable metadata fields. Returns True if a row was updated."""
    now = _now()
    fields: list[str] = []
    values: list[str] = []
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


# ── Token Migration ───────────────────────────────────────────────────────────


def migrate_token_usage(conn: sqlite3.Connection, books_root: Path) -> int:
    """Scan usage.jsonl for each book once, store aggregated counts in books table.

    Only scans books whose total_tokens column is still 0 (never been migrated).
    Returns the number of books whose token counts were updated.
    """
    if not books_root.is_dir():
        return 0

    updated = 0
    for book_dir in sorted(books_root.iterdir()):
        if not book_dir.is_dir():
            continue
        book_id = book_dir.name
        if book_id == "onebookwiki.db":
            continue

        usage_file = book_dir / ".onebookwiki" / "usage.jsonl"
        if not usage_file.is_file():
            continue

        # Check if already migrated
        row = conn.execute(
            "SELECT total_tokens FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row and (row["total_tokens"] or 0) > 0:
            continue

        prompt = completion = total = 0
        try:
            with open(usage_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        prompt += entry.get("prompt_tokens", 0) or 0
                        completion += entry.get("completion_tokens", 0) or 0
                        total += entry.get("total_tokens", 0) or 0
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

        if total > 0:
            conn.execute(
                "UPDATE books SET prompt_tokens = ?, completion_tokens = ?, total_tokens = ? WHERE id = ?",
                (prompt, completion, total, book_id),
            )
            updated += 1

    if updated:
        conn.commit()
    return updated
