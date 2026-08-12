"""Book listing, status, cover, and retry endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/api", tags=["books"])

BOOKS_ROOT = Path(__file__).resolve().parent.parent.parent / "books"


def _db(request: Request):
    """Get database connection from app state."""
    return request.app.state.db


@router.get("/books")
def list_books(request: Request):
    """Return all non-empty books ordered by creation time."""
    from server.database import list_books as do_list

    books = do_list(_db(request))
    return JSONResponse({"books": books})


@router.get("/books/{book_id}")
def get_book_detail(book_id: str, request: Request):
    """Return a single book's full information."""
    from server.database import get_book

    book = get_book(_db(request), book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return JSONResponse(book)


@router.get("/books/{book_id}/status")
def get_book_status(book_id: str, request: Request):
    """Return the current processing phase and any error."""
    from server.database import get_book

    book = get_book(_db(request), book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return JSONResponse({
        "bookId": book["id"],
        "title": book["title"],
        "phase": book["phase"],
        "error": book.get("error_message"),
    })


@router.get("/books/{book_id}/cover")
def get_book_cover(book_id: str, request: Request):
    """Serve the cover image file."""
    from server.database import get_book

    book = get_book(_db(request), book_id)
    if book is None or not book.get("cover_path"):
        raise HTTPException(status_code=404, detail="Cover not found")

    cover_full_path = BOOKS_ROOT / book_id / book["cover_path"]
    if not cover_full_path.is_file():
        raise HTTPException(status_code=404, detail="Cover file missing")

    return FileResponse(cover_full_path, media_type="image/jpeg")


