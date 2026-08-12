"""Background pipeline orchestration for book processing."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

# Ensure the project root is on sys.path so we can import onebookwiki modules
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_pipeline_semaphore = threading.Semaphore(2)


def _find_source_file(source_dir: Path) -> Path | None:
    """Find the source file in raw/source/ directory."""
    if not source_dir.is_dir():
        return None
    for path in source_dir.iterdir():
        if path.is_file() and path.suffix.lower() in {
            ".pdf", ".epub", ".mobi", ".azw", ".azw3",
            ".txt", ".doc", ".docx", ".html", ".htm",
        }:
            return path
    return None


def _update_phase_safe(
    conn: sqlite3.Connection, book_id: str, phase: str, error_message: str | None = None
) -> None:
    """Thread-safe phase update, reconnecting if needed."""
    try:
        from server.database import update_phase

        update_phase(conn, book_id, phase, error_message)
    except sqlite3.ProgrammingError:
        # Connection may have been closed; re-open
        db_path = conn.execute("PRAGMA database_list").fetchone()
        if db_path:
            new_conn = sqlite3.connect(db_path["file"])
            from server.database import update_phase

            update_phase(new_conn, book_id, phase, error_message)
            new_conn.close()


def run_pipeline(
    book_id: str,
    books_root: str | Path,
    conn: sqlite3.Connection,
    backend: str = "bge-m3",
) -> None:
    """
    Execute the full book processing pipeline in the background.

    Phases: queued → importing → indexing → generating → rendering → complete
                                                                     ↘ failed
    """
    from server.database import update_phase as db_update_phase
    from server.database import insert_operation_log, update_cover, update_from_import, update_from_render
    from server.cover import extract_cover

    books_path = Path(books_root)
    book_dir = books_path / book_id
    source_dir = book_dir / "raw" / "source"

    try:
        _pipeline_semaphore.acquire()
        try:
            # ---- Phase 1: Import ----
            db_update_phase(conn, book_id, "importing")

            source_file = _find_source_file(source_dir)
            if source_file is None:
                raise FileNotFoundError(f"No source file found in {source_dir}")

            from onebookwiki.importers import ImportOptions, import_document

            imported = import_document(source_file, book_dir, ImportOptions())
            if not imported:
                raise RuntimeError("Import produced no chapter files")

            # Update metadata from source.json
            source_json = book_dir / ".onebookwiki" / "source.json"
            update_from_import(conn, book_id, source_json)

            # Log import success
            ch_count = len(list((book_dir / "raw" / "chapters").glob("*.md")))
            insert_operation_log(
                conn, book_id, "import", "success", "importing",
                detail=f"Imported {ch_count} chapters from {source_file.name}",
            )

            # Extract cover after import
            cover_dir = book_dir / ".onebookwiki"
            cover_path = extract_cover(source_file, cover_dir)
            if cover_path:
                update_cover(conn, book_id, cover_path)

            # ---- Phase 2: Index ----
            db_update_phase(conn, book_id, "indexing")

            # Import the indexing functions from the scripts
            sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
            from ingest_book import index_cloud as do_index  # type: ignore[import-not-found]

            do_index(book_dir, backend)

            # Log index success
            insert_operation_log(
                conn, book_id, "index", "success", "indexing",
                detail=f"Vector index built with backend {backend}",
            )

            # ---- Phase 3: Generate ----
            db_update_phase(conn, book_id, "generating")

            from onebookwiki.generation import GenerationOptions, generate_wiki
            from onebookwiki.providers import GenerationConfig

            gen_config = GenerationConfig.from_env()
            options = GenerationOptions(
                provider=gen_config.provider,
                model=gen_config.model,
                language="zh-CN",
                retries=4,
            )
            generate_wiki(book_dir, options)

            # Log generate success
            insert_operation_log(
                conn, book_id, "generate", "success", "generating",
                detail="Wiki generation completed",
            )

            # ---- Phase 4: Render ----
            db_update_phase(conn, book_id, "rendering")

            from onebookwiki.rendering import render_artifacts

            render_artifacts(book_dir)

            # Update from structure.json
            structure_json = book_dir / "wiki" / "structure.json"
            update_from_render(conn, book_id, structure_json)

            # Log render success
            page_count = 0
            if structure_json.is_file():
                try:
                    data = json.loads(structure_json.read_text(encoding="utf-8"))
                    page_count = len(data.get("pages", []))
                except Exception:
                    pass
            insert_operation_log(
                conn, book_id, "render", "success", "rendering",
                detail=f"Rendered {page_count} wiki pages",
            )

            # ---- Done ----
            db_update_phase(conn, book_id, "complete")

        finally:
            _pipeline_semaphore.release()

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        try:
            db_update_phase(conn, book_id, "failed", error_msg)
            # Log the failure
            insert_operation_log(
                conn, book_id, "pipeline", "failed",
                detail=error_msg,
            )
        except Exception:
            pass  # Best effort
