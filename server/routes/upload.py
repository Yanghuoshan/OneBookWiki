"""File upload endpoint."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from server.config import books_root

router = APIRouter(prefix="/api", tags=["upload"])

BOOKS_ROOT = books_root()

SUPPORTED_EXTENSIONS = {
    ".pdf", ".epub", ".mobi", ".azw", ".azw3",
    ".txt", ".doc", ".docx", ".html", ".htm",
}
SUPPORTED_LABEL = "PDF, EPUB, MOBI, AZW, AZW3, TXT, DOC, DOCX, HTML"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB


def _db(request: Request):
    return request.app.state.db


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@router.post("/upload")
async def upload_book(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """Upload an ebook file and trigger the analysis pipeline."""
    # Validate filename
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate extension
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported format. Supported: {SUPPORTED_LABEL}",
        )

    # Read file content (with size check)
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="File is empty")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum: 500 MB",
        )

    stem = Path(file.filename).stem
    source_format = ext.lstrip(".").upper()

    # Reserve the numeric ID before creating its matching on-disk book root.
    from server.database import create_placeholder, delete_book, insert_operation_log

    book_id = create_placeholder(
        _db(request),
        title=stem,
        format=source_format,
        source_name=file.filename,
    )
    book_dir = BOOKS_ROOT / str(book_id)

    try:
        source_dir = book_dir / "raw" / "source"
        source_dir.mkdir(parents=True, exist_ok=False)

        safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename)
        dest_path = source_dir / safe_filename
        dest_path.write_bytes(content)
        file_hash = _sha256_file(dest_path)

        source_json = {
            "schema_version": 3,
            "title": stem,
            "source_name": file.filename,
            "format": source_format,
            "source_hash": file_hash,
            "locator_policy": "pending",
        }
        onebookwiki_dir = book_dir / ".onebookwiki"
        onebookwiki_dir.mkdir(exist_ok=True)
        (onebookwiki_dir / "source.json").write_text(
            json.dumps(source_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _db(request).execute(
            "UPDATE books SET source_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (file_hash, book_id),
        )
        _db(request).commit()
    except Exception:
        shutil.rmtree(book_dir, ignore_errors=True)
        delete_book(_db(request), book_id)
        raise

    # Log upload operation
    insert_operation_log(
        _db(request),
        book_id,
        "upload",
        "success",
        detail=f"Uploaded {file.filename} ({len(content)} bytes)",
        book_title=stem,
    )

    # Trigger background pipeline
    from server.pipeline import run_pipeline

    background_tasks.add_task(
        run_pipeline,
        book_id=book_id,
        books_root=str(BOOKS_ROOT),
        conn=_db(request),
    )

    return JSONResponse(
        {"bookId": book_id, "title": stem, "phase": "queued"},
        status_code=201,
    )
