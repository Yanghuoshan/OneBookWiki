"""Validated, optional structural hints for deterministic PDF imports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
import unicodedata

SCHEMA_VERSION = 1
STATUSES = {"pending-readable-source", "ready"}
ROW_FORMATS = {
    "dense-dotted-parenthesized",
    "dense_dotted",
    "slash-stream",
    "page-title-slash-stream",
    "title-page-slash-stream",
    "auto",
}
KINDS = {"front_matter", "part", "chapter", "section", "back_matter"}


def _signature(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).lower()
    return "".join(value.split())


def manifest_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"PDF structure manifest {name} must be an object")
    return value


def validate_manifest(value: dict[str, Any], *, source: Path | None = None, page_count: int | None = None, source_hash: str | None = None) -> dict[str, Any]:
    """Validate and return a copied manifest, optionally against a source identity."""
    manifest = _require_mapping(value, "root")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported PDF structure manifest schema: {manifest.get('schema_version')!r}")
    identifier = manifest.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("PDF structure manifest requires a non-empty id")
    status = manifest.get("status", "ready")
    if status not in STATUSES:
        raise ValueError(f"invalid PDF structure manifest status: {status!r}")

    identity = _require_mapping(manifest.get("source"), "source")
    filename = identity.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("PDF structure manifest source.filename is required")
    expected_pages = identity.get("page_count")
    if expected_pages is not None and (not isinstance(expected_pages, int) or expected_pages < 1):
        raise ValueError("PDF structure manifest source.page_count must be a positive integer")
    expected_hash = identity.get("sha256")
    if expected_hash is not None and (not isinstance(expected_hash, str) or len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash)):
        raise ValueError("PDF structure manifest source.sha256 must be a SHA-256 hex digest")
    if source is not None and source.name != filename:
        raise ValueError(f"PDF structure manifest source filename mismatch: expected {filename}, got {source.name}")
    if page_count is not None and expected_pages is not None and page_count != expected_pages:
        raise ValueError(f"PDF structure manifest page count mismatch: expected {expected_pages}, got {page_count}")
    if source_hash is not None and expected_hash is not None and source_hash.lower() != expected_hash.lower():
        raise ValueError("PDF structure manifest source hash mismatch")

    title_override = manifest.get("title_override")
    if title_override is not None and (not isinstance(title_override, str) or not title_override.strip()):
        raise ValueError("PDF structure manifest title_override must be a non-empty string")

    toc = manifest.get("toc", {})
    _require_mapping(toc, "toc")
    markers = toc.get("markers", [])
    if not isinstance(markers, list) or not all(isinstance(item, str) and item.strip() for item in markers):
        raise ValueError("PDF structure manifest toc.markers must be a list of strings")
    row_formats = toc.get("row_formats", [])
    if not isinstance(row_formats, list) or not all(item in ROW_FORMATS for item in row_formats):
        raise ValueError("PDF structure manifest toc.row_formats contains an unsupported format")
    entries = toc.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("PDF structure manifest toc.entries must be a list")
    seen_titles: set[str] = set()
    previous_printed: int | None = None
    for index, entry in enumerate(entries, 1):
        item = _require_mapping(entry, f"toc.entries[{index}]")
        item_title = item.get("title")
        if not isinstance(item_title, str) or not item_title.strip():
            raise ValueError(f"PDF structure manifest toc entry {index} has no title")
        key = _signature(item_title)
        if key in seen_titles:
            raise ValueError(f"PDF structure manifest has duplicate TOC title: {item_title}")
        seen_titles.add(key)
        printed = item.get("printed_page")
        if printed is not None:
            if not isinstance(printed, int) or printed < 1:
                raise ValueError(f"PDF structure manifest toc entry {index} has invalid printed_page")
            if previous_printed is not None and printed <= previous_printed:
                raise ValueError("PDF structure manifest TOC printed pages must be strictly increasing")
            previous_printed = printed
        level = item.get("level", 1)
        if not isinstance(level, int) or level < 1:
            raise ValueError(f"PDF structure manifest toc entry {index} has invalid level")
        kind = item.get("kind")
        if kind is not None and kind not in KINDS:
            raise ValueError(f"PDF structure manifest toc entry {index} has invalid kind")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(alias, str) and alias.strip() for alias in aliases):
            raise ValueError(f"PDF structure manifest toc entry {index} aliases must be strings")

    headings = manifest.get("headings", {})
    _require_mapping(headings, "headings")
    for key in ("prefixes", "end_markers"):
        values = headings.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
            raise ValueError(f"PDF structure manifest headings.{key} must be a list of strings")

    units = manifest.get("units")
    if units is not None:
        if not isinstance(units, list) or not units:
            raise ValueError("PDF structure manifest units must be a non-empty list")
        if expected_pages is None and page_count is None:
            raise ValueError("PDF structure manifest units require source.page_count")
        total_pages = page_count or expected_pages
        assert total_pages is not None
        expected_start = 1
        seen_starts: set[int] = set()
        for index, entry in enumerate(units, 1):
            item = _require_mapping(entry, f"units[{index}]")
            title = item.get("title")
            start = item.get("start")
            end = item.get("end")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"PDF structure manifest unit {index} has no title")
            if not isinstance(start, int) or not isinstance(end, int) or not (1 <= start <= end <= total_pages):
                raise ValueError(f"PDF structure manifest unit {index} has invalid page range")
            if start in seen_starts or start != expected_start:
                raise ValueError("PDF structure manifest units must form an exact partition from page 1")
            seen_starts.add(start)
            expected_start = end + 1
            level = item.get("level", 1)
            if not isinstance(level, int) or level < 1:
                raise ValueError(f"PDF structure manifest unit {index} has invalid level")
            kind = item.get("kind", "chapter")
            if kind not in KINDS:
                raise ValueError(f"PDF structure manifest unit {index} has invalid kind")
        if expected_start != total_pages + 1:
            raise ValueError("PDF structure manifest units must cover every physical page exactly once")

    coverage = manifest.get("coverage", "exact-partition")
    if coverage != "exact-partition":
        raise ValueError("PDF structure manifest coverage must be 'exact-partition'")
    return json.loads(json.dumps(manifest, ensure_ascii=False))


def load_manifest(path: Path, *, source: Path | None = None, page_count: int | None = None, source_hash: str | None = None) -> tuple[dict[str, Any], str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read PDF structure manifest {path}: {exc}") from exc
    validated = validate_manifest(value, source=source, page_count=page_count, source_hash=source_hash)
    return validated, manifest_digest(validated)
