"""OneBookWiki FastAPI application entry point."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server.database import init_db, migrate_existing_books, migrate_token_usage, reset_stuck_books
from server.routes import books, upload

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOOKS_ROOT = PROJECT_ROOT / "books"
DB_PATH = BOOKS_ROOT / "onebookwiki.db"

# Load environment variables from .env file (before any pipeline imports)
_load_dotenv = load_dotenv(PROJECT_ROOT / ".env")
if _load_dotenv:
    print("[onebookwiki] Environment variables loaded from .env")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup, close on shutdown."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(DB_PATH))
    count = migrate_existing_books(conn, BOOKS_ROOT)
    stuck = reset_stuck_books(conn)
    token_count = migrate_token_usage(conn, BOOKS_ROOT)
    print(f"[onebookwiki] Database initialized at {DB_PATH}")
    print(f"[onebookwiki] Migrated {count} existing book(s)")
    if stuck:
        print(f"[onebookwiki] Reset {stuck} stuck book(s) to failed state (interrupted processing)")
    if token_count:
        print(f"[onebookwiki] Migrated token usage for {token_count} book(s)")
    app.state.db = conn
    yield
    if hasattr(app.state, "db"):
        app.state.db.close()
        print("[onebookwiki] Database connection closed")


app = FastAPI(
    title="OneBookWiki API",
    description="Backend API for OneBookWiki — ebook upload, processing pipeline, and book metadata management.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router)
app.include_router(books.router)

from server.routes import admin  # noqa: E402

app.include_router(admin.router)

# Serve book static files (wiki pages, covers, etc.)
if BOOKS_ROOT.is_dir():
    app.mount("/book", StaticFiles(directory=str(BOOKS_ROOT), html=False), name="book")

@app.post("/api/books/{book_id}/process")
def retry_processing_endpoint(
    book_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Retry the pipeline for a failed book."""
    from server.database import get_book, update_phase

    book = get_book(request.app.state.db, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    if book["phase"] != "failed":
        raise HTTPException(status_code=409, detail="Book is not in failed state")

    update_phase(request.app.state.db, book_id, "queued")

    from server.database import insert_operation_log
    from server.pipeline import run_pipeline

    insert_operation_log(
        request.app.state.db, book_id, "retry", "success", "queued",
        detail="Retry triggered from failed state",
        book_title=book.get("title", book_id),
    )

    background_tasks.add_task(
        run_pipeline,
        book_id=book_id,
        books_root=str(BOOKS_ROOT),
        conn=request.app.state.db,
    )

    return JSONResponse({"bookId": book_id, "phase": "queued"}, status_code=202)


@app.get("/api/health")
def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "version": "0.1.0"}


# ---- Production mode: serve frontend static files + SPA fallback ----
_is_production = os.getenv("ONEBOOKWIKI_ENV", "").lower() == "production"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

if _is_production and FRONTEND_DIST.is_dir():
    print("[onebookwiki] Production mode: serving frontend from frontend/dist/")

    from fastapi.responses import FileResponse

    _assets_dir = FRONTEND_DIST / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="frontend-assets")

    @app.middleware("http")
    async def spa_fallback(request: Request, call_next):
        path = request.url.path
        # API, frontend assets, docs — let them through unchanged
        if path.startswith(("/api", "/assets", "/docs", "/openapi.json")):
            return await call_next(request)
        # /book/<id>/<file...> real file requests (wiki/*.json, *.md, covers) → StaticFiles;
        # bare /book, /book/, and /book/<bookId> are SPA navigation routes → index.html
        if path.startswith("/book/"):
            rest = path[len("/book/"):]
            if "/" in rest:  # deeper path → likely a file
                return await call_next(request)
        # Everything else (incl. /book, /book/, /book/<bookId>) → index.html
        index_path = FRONTEND_DIST / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return await call_next(request)
