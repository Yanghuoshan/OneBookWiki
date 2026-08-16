"""Admin dashboard endpoints: stats, book management, operation logs, token usage."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/admin", tags=["admin"])

BOOKS_ROOT = Path(__file__).resolve().parent.parent.parent / "books"


def _db(request: Request):
    return request.app.state.db


# ── Stats ─────────────────────────────────────────────────────────────────────


@router.get("/stats")
def admin_stats(request: Request):
    """Return dashboard summary statistics."""
    from server.database import get_admin_stats, refresh_token_usage

    refresh_token_usage(_db(request), BOOKS_ROOT)
    stats = get_admin_stats(_db(request))
    return JSONResponse(stats)


# ── Book Delete ───────────────────────────────────────────────────────────────


@router.delete("/books/{book_id}")
def delete_book_endpoint(book_id: int, request: Request):
    """Delete a book from database and its files on disk."""
    from server.database import delete_book, get_book, insert_operation_log

    book = get_book(_db(request), book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    book_title = book.get("title") or book_id

    # Log BEFORE delete — operation_logs FK references books(id)
    insert_operation_log(
        _db(request),
        book_id,
        "delete",
        "success",
        detail=f"Deleted book '{book_title}'",
        book_title=book_title,
    )

    deleted = delete_book(_db(request), book_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete book record")

    # Remove book directory from disk
    book_dir = BOOKS_ROOT / str(book_id)
    if book_dir.is_dir():
        shutil.rmtree(book_dir)

    return JSONResponse({"deleted": True, "bookId": book_id})


# ── Book Metadata Update ──────────────────────────────────────────────────────


@router.patch("/books/{book_id}")
def update_book_endpoint(book_id: int, body: dict, request: Request):
    """Update editable book metadata (title, author, description)."""
    from server.database import get_book, insert_operation_log, update_book_metadata

    book = get_book(_db(request), book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    allowed_fields = {"title", "author", "description"}
    updates = {k: v for k, v in body.items() if k in allowed_fields and v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    updated = update_book_metadata(
        _db(request),
        book_id,
        title=updates.get("title"),
        author=updates.get("author"),
        description=updates.get("description"),
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update book")

    insert_operation_log(
        _db(request),
        book_id,
        "update_metadata",
        "success",
        detail=f"Updated fields: {', '.join(updates.keys())}",
        book_title=book.get("title", book_id),
    )

    # Return updated book
    from server.database import get_book as do_get

    return JSONResponse(do_get(_db(request), book_id))


# ── Operation Logs ────────────────────────────────────────────────────────────


@router.get("/operations")
def list_operations(
    request: Request,
    book_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Return paginated operation logs, optionally filtered by book."""
    from server.database import list_operation_logs

    logs, total = list_operation_logs(
        _db(request), book_id=book_id, limit=limit, offset=offset
    )
    return JSONResponse({"logs": logs, "total": total, "limit": limit, "offset": offset})


# ── Token Usage ───────────────────────────────────────────────────────────────


@router.get("/tokens")
def get_all_token_usage(request: Request):
    """Return aggregated token usage across all books."""
    from server.database import refresh_token_usage

    conn = _db(request)
    refresh_token_usage(conn, BOOKS_ROOT)
    rows = conn.execute(
        """SELECT id, title, prompt_tokens, completion_tokens, total_tokens
           FROM books WHERE phase != 'empty' AND total_tokens > 0
           ORDER BY total_tokens DESC"""
    ).fetchall()

    books = [
        {
            "book_id": row["id"],
            "title": row["title"],
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
            "total_tokens": row["total_tokens"] or 0,
        }
        for row in rows
    ]

    prompt_total = sum(b["prompt_tokens"] for b in books)
    completion_total = sum(b["completion_tokens"] for b in books)
    total_all = sum(b["total_tokens"] for b in books)

    return JSONResponse({
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "total_tokens": total_all,
        "book_count": len(books),
        "books": books,
    })


@router.get("/tokens/{book_id}")
def get_book_token_usage(book_id: int, request: Request):
    """Return detailed token usage for a single book."""
    from server.database import get_book, refresh_token_usage

    refresh_token_usage(_db(request), BOOKS_ROOT, book_id)
    book = get_book(_db(request), book_id)
    usage_file = BOOKS_ROOT / str(book_id) / ".onebookwiki" / "usage.jsonl"

    result = {
        "book_id": book_id,
        "book_title": book.get("title", book_id) if book else book_id,
        "prompt_tokens": book.get("prompt_tokens", 0) or 0 if book else 0,
        "completion_tokens": book.get("completion_tokens", 0) or 0 if book else 0,
        "total_tokens": book.get("total_tokens", 0) or 0 if book else 0,
        "entries": [],
    }

    if usage_file.is_file():
        try:
            entries: list[dict] = []
            with open(usage_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            result["entries"] = entries
        except OSError:
            pass

    return JSONResponse(result)
