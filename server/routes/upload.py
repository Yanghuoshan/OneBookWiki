"""File upload endpoint."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["upload"])

BOOKS_ROOT = Path(__file__).resolve().parent.parent.parent / "books"

SUPPORTED_EXTENSIONS = {
    ".pdf", ".epub", ".mobi", ".azw", ".azw3",
    ".txt", ".doc", ".docx", ".html", ".htm",
}
SUPPORTED_LABEL = "PDF, EPUB, MOBI, AZW, AZW3, TXT, DOC, DOCX, HTML"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB


def _db(request: Request):
    return request.app.state.db


def _slugify(filename_stem: str) -> str:
    """Generate a clean book ID from a filename stem."""
    # Normalize unicode to ASCII
    normalized = unicodedata.normalize("NFKD", filename_stem).encode(
        "ascii", "ignore"
    ).decode("ascii")
    # Replace non-alphanumeric chars with hyphens
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", normalized).strip("_")
    if not slug or not re.match(r"^[A-Za-z0-9]", slug):
        slug = "book_" + (slug or "untitled")
    return slug


def _unique_book_id(conn, base_slug: str) -> str:
    """Ensure a unique book ID by appending _2, _3, etc. if needed."""
    from server.database import book_id_exists

    candidate = base_slug
    if not book_id_exists(conn, candidate):
        return candidate

    counter = 2
    while book_id_exists(conn, f"{base_slug}_{counter}"):
        counter += 1
    return f"{base_slug}_{counter}"


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

    # Generate book ID
    stem = Path(file.filename).stem
    raw_slug = _slugify(stem)
    book_id = _unique_book_id(_db(request), raw_slug)

    # Create directory structure
    source_dir = BOOKS_ROOT / book_id / "raw" / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    # Save the file
    safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename)
    dest_path = source_dir / safe_filename
    dest_path.write_bytes(content)

    # Compute hash
    file_hash = _sha256_file(dest_path)

    # Write minimal source.json
    import json
    import time

    source_json = {
        "schema_version": 3,
        "title": stem,
        "source_name": file.filename,
        "format": ext.lstrip(".").upper(),
        "source_hash": file_hash,
        "locator_policy": "pending",
    }
    onebookwiki_dir = BOOKS_ROOT / book_id / ".onebookwiki"
    onebookwiki_dir.mkdir(parents=True, exist_ok=True)
    (onebookwiki_dir / "source.json").write_text(
        json.dumps(source_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Insert placeholder in database
    from server.database import insert_operation_log, insert_placeholder

    insert_placeholder(
        _db(request),
        book_id=book_id,
        title=stem,
        format=ext.lstrip(".").upper(),
        source_name=file.filename,
        source_hash=file_hash,
    )

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
