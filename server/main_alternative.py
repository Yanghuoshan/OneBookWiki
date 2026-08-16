"""OneBookWiki FastAPI application entry point - Alternative version with different mounting strategy."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server.config import PROJECT_ROOT

# Load environment variables before resolving any runtime paths or provider settings.
_load_dotenv = load_dotenv(PROJECT_ROOT / ".env")
if _load_dotenv:
    print("[onebookwiki] Environment variables loaded from .env")

from server.config import books_root, db_path  # noqa: E402
from server.database import init_db, reset_stuck_books, reset_stuck_chat_jobs  # noqa: E402
from server.routes import books, chat, upload  # noqa: E402

BOOKS_ROOT = books_root()
DB_PATH = db_path()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup, close on shutdown."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(DB_PATH))
    stuck = reset_stuck_books(conn)
    recovered_chat_jobs = reset_stuck_chat_jobs(conn)
    print(f"[onebookwiki] Database initialized at {DB_PATH}")
    if stuck:
        print(f"[onebookwiki] Reset {stuck} stuck book(s) to failed state (interrupted processing)")
    if recovered_chat_jobs:
        print(f"[onebookwiki] Requeued {recovered_chat_jobs} expired chat job(s)")
    app.state.db = conn
    app.state.books_root = BOOKS_ROOT
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
app.include_router(chat.router)

from server.routes import admin  # noqa: E402

app.include_router(admin.router)

# Serve book static files (wiki pages, covers, etc.)
if BOOKS_ROOT.is_dir():
    app.mount("/book", StaticFiles(directory=str(BOOKS_ROOT), html=False), name="book")

@app.post("/api/books/{book_id}/process")
def retry_processing_endpoint(
    book_id: int,
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
def health_check(request: Request) -> dict:
    """Health check endpoint."""
    queued = request.app.state.db.execute("SELECT COUNT(*) FROM chat_jobs WHERE status = 'queued'").fetchone()[0]
    return {"status": "ok", "version": "0.1.0", "chat": {"queued_jobs": queued}}


# ---- Production mode: serve frontend static files + SPA fallback ----
_is_production = os.getenv("ONEBOOKWIKI_ENV", "").lower() == "production"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

if _is_production:
    if not FRONTEND_DIST.is_dir():
        print(f"[onebookwiki] WARNING: Production mode enabled but frontend/dist not found at {FRONTEND_DIST}")
        print(f"[onebookwiki] Please run: cd frontend && npm run build")
    else:
        print(f"[onebookwiki] Production mode: serving frontend from {FRONTEND_DIST}")

        from fastapi.responses import FileResponse

        # IMPORTANT: Mount /assets FIRST, before any middleware or fallback routes
        _assets_dir = FRONTEND_DIST / "assets"
        if _assets_dir.is_dir():
            print(f"[onebookwiki] Mounting /assets from {_assets_dir}")
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="frontend-assets")
        else:
            print(f"[onebookwiki] WARNING: Assets directory not found at {_assets_dir}")

        # Serve index.html for SPA routes (must come AFTER /assets mount)
        @app.get("/{full_path:path}")
        async def spa_fallback(request: Request, full_path: str):
            """Serve index.html for all unmatched routes (SPA fallback)."""
            # Skip if this is an API call or static asset that already 404'd
            if full_path.startswith(("api/", "docs", "openapi.json")):
                raise HTTPException(status_code=404, detail="Not found")

            # For /book/<numeric-id>/wiki/* paths, let the book static files handler try first
            if full_path.startswith("book/"):
                rest = full_path[5:]  # Remove "book/"
                if rest:
                    book_id, separator, relative_path = rest.partition("/")
                    if book_id.isdigit() and separator and relative_path.startswith("wiki/"):
                        # This is a book static file request, let it 404 naturally
                        raise HTTPException(status_code=404, detail="Book file not found")

            # Everything else gets the SPA entry point
            index_path = FRONTEND_DIST / "index.html"
            if index_path.is_file():
                return FileResponse(index_path)

            raise HTTPException(status_code=404, detail="Frontend not built")

        print("[onebookwiki] SPA fallback route registered")
