"""Read-only raw-index and published Grounded v2 projection checker.

Usage: check_book.py [project-root] [--json]

The published contract is ``wiki/structure.json`` plus ``wiki/evidence.json``.
Legacy rendered artifacts and CnEn citation records are not a source of truth.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from onebookwiki.markdown import metadata, parse_document
from onebookwiki.pdf_manifest import manifest_digest, validate_manifest

CONTRACT_VERSION = "grounded-v2"
EVIDENCE_ID_RE = re.compile(r"^evr-[0-9a-f]{32}$")
BOOK_REVISION_ID_RE = re.compile(r"^book-[0-9a-f]{32}$")
STATEMENT_REVISION_ID_RE = re.compile(r"^str-[0-9a-f]{32}$")
COMPOSITION_REVISION_ID_RE = re.compile(r"^cmpr-[0-9a-f]{32}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ONEBOOKWIKI_EVIDENCE_RE = re.compile(r"onebookwiki://evidence/([^\s)`]>]+)")
CNEN_RE = re.compile(r"(?<![A-Za-z0-9_/])C\d+E\d+(?![A-Za-z0-9_/])")
NO_MATERIAL_RE = re.compile(r"^## \[[^\]]*\]\s*ingest\s*\|\s*no material:\s*`?(\S+?)`?\s*$", re.IGNORECASE)
SUPPORTED_SOURCE_FORMATS = {"PDF", "EPUB", "MOBI", "AZW", "AZW3", "TXT", "DOC", "DOCX", "HTML"}


def relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read_json(path: Path, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, [f"{label} is missing"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, [f"{label} is invalid JSON: {error}"]
    if not isinstance(value, dict):
        return None, [f"{label} must be a JSON object"]
    return value, []


def _nonempty_string(value: object, label: str, problems: list[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{label} must be a non-empty string")
        return False
    return True


def _string_array(value: object, label: str, problems: list[str]) -> list[str] | None:
    if not isinstance(value, list):
        problems.append(f"{label} must be an array")
        return None
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            problems.append(f"{label}[{index}] must be a non-empty string")
            continue
        if item in seen:
            problems.append(f"{label} contains duplicate value: {item}")
        seen.add(item)
        result.append(item)
    return result


def _positive_int(value: object, label: str, problems: list[str]) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        problems.append(f"{label} must be a positive integer")
        return False
    return True


def _safe_relative_path(root: Path, value: object, label: str, problems: list[str], suffix: str | None = None) -> Path | None:
    if not isinstance(value, str) or not value or "\\" in value:
        problems.append(f"{label} is not a safe relative path")
        return None
    parts = value.split("/")
    if not parts or value.startswith("/") or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        problems.append(f"{label} is not a safe relative path: {value}")
        return None
    if suffix is not None and not value.lower().endswith(suffix.lower()):
        problems.append(f"{label} must end with {suffix}: {value}")
        return None
    target = (root / Path(*parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        problems.append(f"{label} escapes its allowed root: {value}")
        return None
    if not target.is_file():
        problems.append(f"{label} is missing: {value}")
        return None
    return target


def _revision_array(value: object, label: str, pattern: re.Pattern[str], problems: list[str]) -> list[str] | None:
    values = _string_array(value, label, problems)
    if values is None:
        return None
    for item in values:
        if not pattern.fullmatch(item):
            problems.append(f"{label} contains invalid revision id: {item}")
    return values


def _raw_chapter_number(path: Path) -> int | None:
    try:
        values = metadata(parse_document(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        values = {}
    chapter = values.get("chapter", "").strip()
    if chapter.isdigit():
        return int(chapter)
    match = re.match(r"^(\d+)[-_]", path.name)
    return int(match.group(1)) if match else None


def raw_chapter_health(root: Path) -> list[str]:
    """Check only raw chapter naming/order; wiki Markdown is not consulted."""
    raw_dir = root / "raw" / "chapters"
    chapters = sorted(raw_dir.glob("*.md")) if raw_dir.is_dir() else []
    numbers = [_raw_chapter_number(path) for path in chapters]
    problems: list[str] = []
    for path, number in zip(chapters, numbers):
        match = re.match(r"^(\d+)[-_]", path.name)
        if number is None:
            problems.append(f"{relative(root, path)}: raw chapter has missing or malformed Chapter metadata")
        elif not match or int(match.group(1)) != number:
            problems.append(f"{relative(root, path)}: raw filename number does not match Chapter {number}")
    known = [number for number in numbers if number is not None]
    for number, count in Counter(known).items():
        if count > 1:
            problems.append(f"duplicate raw chapter number: {number}")
    if known:
        expected = set(range(1, max(known) + 1))
        for missing in sorted(expected - set(known)):
            problems.append(f"missing raw chapter number: {missing}")
    return problems


def broken_links(root: Path) -> list[str]:
    problems: list[str] = []
    wiki = root / "wiki"
    if not wiki.is_dir():
        return []
    for path in wiki.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f"{relative(root, path)} is unreadable: {error}")
            continue
        for target in LINK_RE.findall(text):
            target = target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            candidate = (path.parent / target).resolve()
            if not candidate.is_file():
                problems.append(f"{relative(root, path)} -> {target}")
    return sorted(set(problems))


def no_material_paths(log: Path) -> set[str]:
    if not log.is_file():
        return set()
    paths: set[str] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        match = NO_MATERIAL_RE.match(line)
        if match:
            paths.add(match.group(1).strip("`,;."))
    return paths


def rag_health(root: Path) -> list[str]:
    """Validate the independent raw chapter index and Grounded v2 manifest."""
    manifest_path = root / ".onebookwiki" / "manifest.json"
    if not manifest_path.is_file():
        return ["no manifest; run onebookwiki-ingest index"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"manifest is not valid JSON: {error}"]
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    problems: list[str] = []
    if manifest.get("schema_version") != 5:
        problems.append("manifest schema_version must be 5")
    if manifest.get("contract_version") != CONTRACT_VERSION:
        problems.append("manifest contract_version must be grounded-v2")
    if manifest.get("schema_integrity") != CONTRACT_VERSION:
        problems.append("manifest schema_integrity must be grounded-v2")
    _nonempty_string(manifest.get("book_revision_id"), "manifest book_revision_id", problems)
    if not isinstance(manifest.get("book_revision_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("book_revision_hash"))):
        problems.append("manifest book_revision_hash must be 64 lowercase hex characters")
    chapters = manifest.get("chapters")
    chunks = manifest.get("chunks")
    if not isinstance(chapters, dict):
        problems.append("manifest chapters must be an object")
        chapters = {}
    if not isinstance(chunks, dict):
        problems.append("manifest chunks must be an object")
        chunks = {}
    publication = manifest.get("publication_health")
    if not isinstance(publication, dict):
        problems.append("manifest publication_health must be an object")
    evidence_revisions = manifest.get("evidence_revisions")
    if not isinstance(evidence_revisions, dict):
        problems.append("manifest evidence_revisions must be an object")
    raw_dir = root / "raw" / "chapters"
    for raw in sorted(raw_dir.glob("*.md")) if raw_dir.is_dir() else []:
        key = relative(root, raw)
        chapter = chapters.get(key)
        if not isinstance(chapter, dict):
            problems.append(f"raw chapter not indexed: {key}")
            continue
        actual = hashlib.sha256(raw.read_bytes()).hexdigest()
        if chapter.get("content_hash") != actual:
            problems.append(f"stale chapter hash: {key}")
        ids = chapter.get("chunk_ids", [])
        if not isinstance(ids, list) or not ids:
            problems.append(f"chapter has no chunks: {key}")
            ids = []
        line_count = len(raw.read_text(encoding="utf-8").splitlines())
        for chunk_id in ids:
            chunk = chunks.get(chunk_id)
            if not isinstance(chunk, dict):
                problems.append(f"manifest references missing chunk: {chunk_id}")
                continue
            start, end = chunk.get("start_line"), chunk.get("end_line")
            if not _positive_int(start, f"chunk {chunk_id} start_line", problems) or not _positive_int(end, f"chunk {chunk_id} end_line", problems):
                continue
            if end < start or end > line_count:
                problems.append(f"chunk has invalid line range: {chunk_id}")
    return sorted(set(problems))


def source_metadata_health(root: Path, source_metadata: dict[str, object] | None) -> list[str]:
    """Check schema-v3 source metadata independently of the Wiki projection."""
    if source_metadata is None:
        return []
    problems: list[str] = []
    source_format = str(source_metadata.get("format", "")).upper()
    if source_format and source_format not in SUPPORTED_SOURCE_FORMATS:
        problems.append(f"source metadata has unsupported format: {source_format}")
    schema_version = source_metadata.get("schema_version")
    if schema_version is not None and schema_version != 3:
        problems.append("source metadata schema_version must be 3")
    structure = source_metadata.get("source_structure")
    if not isinstance(structure, dict):
        return problems
    units = structure.get("units") or source_metadata.get("chapters") or []
    if not isinstance(units, list):
        return [*problems, "source structure units must be an array"]
    known_ids: set[str] = set()
    raw_root = (root / "raw" / "chapters").resolve()
    expected_chapter = 1
    for position, unit in enumerate(units, 1):
        if not isinstance(unit, dict):
            problems.append(f"source unit {position} is not an object")
            continue
        if unit.get("chapter") != expected_chapter:
            problems.append(f"source unit {position} has non-contiguous chapter number")
        expected_chapter += 1
        unit_id = unit.get("source_unit_id")
        if not isinstance(unit_id, str) or not unit_id or unit_id in known_ids:
            problems.append(f"source unit {position} has missing or duplicate source_unit_id")
        else:
            known_ids.add(unit_id)
        raw_path = unit.get("raw_path")
        if not isinstance(raw_path, str) or not raw_path:
            problems.append(f"source unit {position} has no raw_path")
        else:
            candidate = (root / raw_path).resolve()
            if not candidate.is_relative_to(raw_root) or not candidate.is_file():
                problems.append(f"source unit {position} has unsafe or missing raw_path")
        locator = unit.get("locator")
        if locator is not None:
            if not isinstance(locator, dict) or not locator.get("format") or not locator.get("kind"):
                problems.append(f"source unit {position} has invalid locator")
            elif source_format and str(locator.get("format", "")).upper() != source_format:
                problems.append(f"source unit {position} locator format differs from source format")
    referenced: set[str] = set()

    def visit(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for unit_id in node.get("unitIds", []) or []:
                if isinstance(unit_id, str):
                    referenced.add(unit_id)
                else:
                    problems.append("source outline has invalid unitId")
            visit(node.get("children", []))

    visit(structure.get("outline", []))
    has_canonical_locators = any(isinstance(unit, dict) and isinstance(unit.get("locator"), dict) for unit in units)
    if known_ids and (referenced or has_canonical_locators):
        missing = sorted(known_ids - referenced)
        if missing:
            problems.append(f"source outline does not cover unit(s): {', '.join(missing)}")
        unknown = sorted(referenced - known_ids)
        if unknown:
            problems.append(f"source outline refers to unknown unit(s): {', '.join(unknown)}")
    return problems


def pdf_manifest_health(root: Path, source_metadata: dict[str, object], report: dict[str, object]) -> list[str]:
    source_structure = source_metadata.get("source_structure")
    source_provenance = source_structure.get("structure_manifest") if isinstance(source_structure, dict) else None
    report_provenance = report.get("structure_manifest")
    if source_provenance is None and report_provenance is None:
        return []
    if not isinstance(source_provenance, dict) or not isinstance(report_provenance, dict):
        return ["PDF structure manifest provenance must appear in both source metadata and structure report"]
    required = ("id", "status", "hash", "source_match", "application_mode", "snapshot_path")
    if any(source_provenance.get(key) != report_provenance.get(key) for key in required):
        return ["PDF structure manifest provenance differs between source metadata and structure report"]
    snapshot_path = source_provenance.get("snapshot_path")
    if not isinstance(snapshot_path, str) or not snapshot_path:
        return ["PDF structure manifest provenance has no snapshot_path"]
    snapshot = (root / snapshot_path).resolve()
    if not snapshot.is_relative_to(root.resolve()) or not snapshot.is_file():
        return ["PDF structure manifest snapshot is missing or unsafe"]
    try:
        manifest = json.loads(snapshot.read_text(encoding="utf-8"))
        expected_pages = source_metadata.get("page_count")
        page_count = expected_pages if isinstance(expected_pages, int) else None
        validated = validate_manifest(manifest, page_count=page_count)
    except (OSError, ValueError) as error:
        return [f"PDF structure manifest snapshot is invalid: {error}"]
    problems: list[str] = []
    if validated["id"] != source_provenance.get("id"):
        problems.append("PDF structure manifest snapshot id differs from provenance")
    if manifest_digest(validated) != source_provenance.get("hash"):
        problems.append("PDF structure manifest snapshot hash differs from provenance")
    expected_mode = "explicit_units" if validated.get("units") else "rules_only"
    if expected_mode != source_provenance.get("application_mode"):
        problems.append("PDF structure manifest application mode differs from snapshot")
    expected_name = source_metadata.get("source_name")
    if isinstance(expected_name, str) and validated["source"]["filename"] != expected_name:
        problems.append("PDF structure manifest snapshot source filename differs from source metadata")
    expected_hash = source_metadata.get("source_hash")
    manifest_hash = validated["source"].get("sha256")
    if isinstance(expected_hash, str) and isinstance(manifest_hash, str) and manifest_hash.lower() != expected_hash.lower():
        problems.append("PDF structure manifest snapshot source hash differs from source metadata")
    source_match = source_provenance.get("source_match")
    if not isinstance(source_match, dict) or source_match != {"filename": True, "page_count": True, "sha256": True}:
        problems.append("PDF structure manifest provenance lacks verified source identity")
    return problems


def pdf_ocr_health(report: dict[str, object], source_metadata: dict[str, object] | None) -> list[str]:
    mode = report.get("ocr")
    if mode == "disabled":
        return []
    if mode != "pp-ocrv5-mobile-assist":
        return ["structure report has invalid OCR mode"]
    assist = report.get("ocr_assist")
    if not isinstance(assist, dict):
        return ["OCR assist report must be an object"]
    problems: list[str] = []
    if assist.get("mode") != "assist":
        problems.append("OCR assist report has invalid mode")
    if assist.get("role") != "structure_auxiliary_only":
        problems.append("OCR assist report must be structure auxiliary only")
    if assist.get("local_only") is not True:
        problems.append("OCR assist report must declare local_only")
    if assist.get("status") not in {"not_used", "skipped", "used", "evaluated_not_selected", "no_usable_evidence", "unavailable"}:
        problems.append("OCR assist report has invalid status")
    for key in ("candidate_pages", "processed_pages", "evidence_pages"):
        value = assist.get(key)
        if not isinstance(value, list) or any(not isinstance(page, int) or page < 1 for page in value):
            problems.append(f"OCR assist report has invalid {key}")
    failed_pages = assist.get("failed_pages")
    if not isinstance(failed_pages, dict) or any(not str(page).isdigit() or not isinstance(error, str) for page, error in failed_pages.items()):
        problems.append("OCR assist report has invalid failed_pages")
    if not isinstance(assist.get("selected"), bool):
        problems.append("OCR assist report has invalid selected flag")
    if source_metadata is not None:
        processing = source_metadata.get("source_processing")
        provenance = processing.get("structure_ocr") if isinstance(processing, dict) else None
        if provenance != assist:
            problems.append("OCR assist provenance differs between source metadata and structure report")
    return problems


def pdf_health(root: Path) -> list[str]:
    """Validate PDF structure/OCR/manifest provenance without requiring a projection."""
    source_path = root / ".onebookwiki" / "source.json"
    source_metadata: dict[str, object] | None = None
    problems: list[str] = []
    if source_path.is_file():
        try:
            value = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                problems.append(".onebookwiki/source.json must be a JSON object")
            else:
                source_metadata = value
        except (OSError, ValueError):
            problems.append(".onebookwiki/source.json is invalid JSON")
    problems.extend(source_metadata_health(root, source_metadata))
    if not source_metadata or str(source_metadata.get("format", "")).upper() != "PDF":
        return problems
    report_path = root / ".onebookwiki" / "structure-report.json"
    if not report_path.is_file():
        return [*problems, "PDF source is missing .onebookwiki/structure-report.json"]
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [*problems, f"structure report is invalid JSON: {error}"]
    if not isinstance(report, dict):
        return [*problems, "structure report must be a JSON object"]
    problems.extend(pdf_ocr_health(report, source_metadata))
    if not isinstance(report.get("selected_method"), str):
        problems.append("structure report has no selected_method")
    postprocess = report.get("postprocess")
    if postprocess is not None:
        if not isinstance(postprocess, dict):
            problems.append("structure report postprocess must be an object")
        else:
            if postprocess.get("mode") not in {"off", "auto", "strict"}:
                problems.append("structure report postprocess has invalid mode")
            if postprocess.get("engine") != "pymupdf-existing-text":
                problems.append("structure report postprocess must use existing PDF text")
            if postprocess.get("semantic_model") != "none":
                problems.append("structure report postprocess must declare no semantic model")
            processed_pages = postprocess.get("pages_processed", 0)
            changed_pages = postprocess.get("pages_changed", 0)
            if not isinstance(processed_pages, int) or processed_pages < 0:
                problems.append("structure report postprocess has invalid pages_processed")
            if not isinstance(changed_pages, int) or changed_pages < 0:
                problems.append("structure report postprocess has invalid pages_changed")
            if isinstance(processed_pages, int) and isinstance(changed_pages, int) and changed_pages > processed_pages:
                problems.append("structure report postprocess pages_changed exceeds pages_processed")
    problems.extend(pdf_manifest_health(root, source_metadata, report))
    return sorted(set(problems))


def _validate_outline(nodes: object, page_ids: set[str], problems: list[str], prefix: str = "sourceOutline", seen: set[str] | None = None) -> None:
    if not isinstance(nodes, list):
        problems.append(f"{prefix} must be an array")
        return
    seen = seen if seen is not None else set()
    for index, node in enumerate(nodes, 1):
        location = f"{prefix}[{index}]"
        if not isinstance(node, dict):
            problems.append(f"{location} is not an object")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not SAFE_ID_RE.fullmatch(node_id):
            problems.append(f"{location} has invalid id")
        elif node_id in seen:
            problems.append(f"sourceOutline has duplicate node id: {node_id}")
        else:
            seen.add(node_id)
        page_refs = _string_array(node.get("pageIds", []), f"{location}.pageIds", problems)
        if page_refs:
            for page_id in page_refs:
                if page_id not in page_ids:
                    problems.append(f"{location} references unknown page: {page_id}")
        for key in ("unitIds",):
            if key in node:
                _string_array(node.get(key), f"{location}.{key}", problems)
        if "chapters" in node:
            chapters = node.get("chapters")
            if not isinstance(chapters, list) or any(not _positive_int(item, f"{location}.chapters", problems) for item in chapters):
                problems.append(f"{location}.chapters must contain positive integers")
        _validate_outline(node.get("children", []), page_ids, problems, f"{location}.children", seen)


def structure_health(root: Path) -> list[str]:
    """Validate the published Grounded v2 page graph and provenance references."""
    value, problems = _read_json(root / "wiki" / "structure.json", "wiki/structure.json")
    if value is None:
        return problems
    if value.get("contractVersion") != CONTRACT_VERSION:
        problems.append("wiki/structure.json contractVersion must be grounded-v2")
    if value.get("projectionStatus") != "healthy":
        problems.append("wiki/structure.json projectionStatus must be healthy")
    revision = value.get("bookRevisionId")
    if not _nonempty_string(revision, "wiki/structure.json bookRevisionId", problems):
        revision = None
    elif BOOK_REVISION_ID_RE.fullmatch(str(revision)) is None:
        problems.append("wiki/structure.json bookRevisionId has invalid format")
    pages = value.get("pages")
    sections = value.get("sections")
    if not isinstance(pages, list) or not pages:
        problems.append("wiki/structure.json requires a non-empty pages array")
        pages = []
    if not isinstance(sections, list) or not sections:
        problems.append("wiki/structure.json requires a non-empty sections array")
        sections = []
    page_ids: set[str] = set()
    page_paths: set[str] = set()
    page_evidence: list[tuple[str, list[str]]] = []
    for index, page in enumerate(pages, 1):
        label = f"structure page {index}"
        if not isinstance(page, dict):
            problems.append(f"{label} is not an object")
            continue
        page_id = page.get("id")
        page_path = page.get("path")
        if not isinstance(page_id, str) or not SAFE_ID_RE.fullmatch(page_id):
            problems.append(f"{label} has invalid safe id")
            page_id = None
        elif page_id in page_ids:
            problems.append(f"structure has duplicate page id: {page_id}")
        else:
            page_ids.add(page_id)
        if not isinstance(page_path, str) or page_path in page_paths:
            if page_path in page_paths:
                problems.append(f"structure has duplicate page path: {page_path}")
            else:
                problems.append(f"{label} has no path")
        else:
            page_paths.add(page_path)
            _safe_relative_path(root / "wiki", page_path, f"structure page {page_id or index} path", problems, ".md")
        if revision is not None and page.get("bookRevisionId") != revision:
            problems.append(f"structure page {page_id or index} has mismatched bookRevisionId")
        related = _string_array(page.get("relatedPages", []), f"structure page {page_id or index}.relatedPages", problems)
        if related:
            for related_id in related:
                if related_id not in page_ids:
                    # Page IDs may be declared later; defer the final check below.
                    pass
        for key, pattern in (("statementRevisionIds", STATEMENT_REVISION_ID_RE), ("compositionRevisionIds", COMPOSITION_REVISION_ID_RE), ("evidenceRevisionIds", EVIDENCE_ID_RE)):
            if key in page:
                refs = _revision_array(page.get(key), f"structure page {page_id or index}.{key}", pattern, problems)
                if key == "evidenceRevisionIds" and refs is not None:
                    page_evidence.append((str(page_id or index), refs))
        for key in ("rawSources", "breadcrumb"):
            if key in page:
                _string_array(page.get(key), f"structure page {page_id or index}.{key}", problems)
        for key in ("part", "partCount", "chapter", "spineIndex"):
            if key in page and page.get(key) is not None:
                _positive_int(page.get(key), f"structure page {page_id or index}.{key}", problems)
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("id"), str):
            continue
        related = _string_array(page.get("relatedPages", []), f"structure page {page['id']}.relatedPages", problems) or []
        for related_id in related:
            if related_id not in page_ids:
                problems.append(f"structure page {page['id']} references unknown related page: {related_id}")
    section_ids: set[str] = set()
    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            problems.append(f"structure section {index} is not an object")
            continue
        section_id = section.get("id")
        if not isinstance(section_id, str) or not SAFE_ID_RE.fullmatch(section_id):
            problems.append(f"structure section {index} has invalid id")
        elif section_id in section_ids:
            problems.append(f"structure has duplicate section id: {section_id}")
        else:
            section_ids.add(section_id)
        page_refs = _string_array(section.get("pages", []), f"structure section {section_id or index}.pages", problems) or []
        for page_id in page_refs:
            if page_id not in page_ids:
                problems.append(f"structure section {section_id or index} references unknown page: {page_id}")
    _validate_outline(value.get("sourceOutline"), page_ids, problems)
    evidence_value, evidence_errors = _read_json(root / "wiki" / "evidence.json", "wiki/evidence.json")
    if evidence_value is not None and not evidence_errors:
        records = evidence_value.get("evidence")
        if isinstance(records, dict):
            known_evidence = set(records)
            for page_id, refs in page_evidence:
                for evidence_id in refs:
                    if evidence_id not in known_evidence:
                        problems.append(f"structure page {page_id} references unknown evidence revision: {evidence_id}")
    return sorted(set(problems))


def _validate_locator(locator: object, label: str, start: int, end: int, problems: list[str]) -> None:
    if not isinstance(locator, dict) or not isinstance(locator.get("format"), str) or not str(locator.get("format")).strip():
        problems.append(f"{label} must declare a usable locator format")
        return
    for key in ("source_line_start", "source_line_end", "line_start", "line_end"):
        if key in locator and not _positive_int(locator.get(key), f"{label}.{key}", problems):
            continue
    locator_start = locator.get("source_line_start", locator.get("line_start"))
    locator_end = locator.get("source_line_end", locator.get("line_end"))
    if isinstance(locator_start, int) and isinstance(locator_end, int):
        if locator_start > locator_end or locator_start < start or locator_end > end:
            problems.append(f"{label} line range is outside the evidence range")


def evidence_health(root: Path) -> list[str]:
    """Validate the published immutable evidence projection, not legacy CnEn records."""
    value, problems = _read_json(root / "wiki" / "evidence.json", "wiki/evidence.json")
    if value is None:
        return problems
    if value.get("contractVersion") != CONTRACT_VERSION:
        problems.append("wiki/evidence.json contractVersion must be grounded-v2")
    if value.get("projectionStatus") != "healthy":
        problems.append("wiki/evidence.json projectionStatus must be healthy")
    revision = value.get("bookRevisionId")
    if not _nonempty_string(revision, "wiki/evidence.json bookRevisionId", problems):
        revision = None
    elif BOOK_REVISION_ID_RE.fullmatch(str(revision)) is None:
        problems.append("wiki/evidence.json bookRevisionId has invalid format")
    records = value.get("evidence")
    if not isinstance(records, dict) or not records:
        problems.append("wiki/evidence.json evidence must be a non-empty object")
        return sorted(set(problems))
    for evidence_id, record in records.items():
        label = f"evidence record {evidence_id}"
        if not isinstance(evidence_id, str) or not EVIDENCE_ID_RE.fullmatch(evidence_id):
            problems.append(f"{label} has invalid key; expected evr- plus 32 lowercase hex characters")
            continue
        if not isinstance(record, dict):
            problems.append(f"{label} is not an object")
            continue
        if record.get("evidence_id") != evidence_id or record.get("evidenceRevisionId") != evidence_id:
            problems.append(f"{label} key/self ID mismatch")
        if revision is not None and record.get("bookRevisionId") != revision:
            problems.append(f"{label} has mismatched bookRevisionId")
        source_path = record.get("source_path")
        _safe_relative_path(root / "raw" / "chapters", source_path.removeprefix("raw/chapters/") if isinstance(source_path, str) and source_path.startswith("raw/chapters/") else source_path, f"{label}.source_path", problems, ".md")
        start, end = record.get("start_line"), record.get("end_line")
        valid_start = _positive_int(start, f"{label}.start_line", problems)
        valid_end = _positive_int(end, f"{label}.end_line", problems)
        if valid_start and valid_end and end < start:
            problems.append(f"{label} line range is invalid")
        if "excerpt_start_line" in record and record.get("excerpt_start_line") != start:
            problems.append(f"{label} excerpt_start_line does not match start_line")
        if "excerpt_end_line" in record and record.get("excerpt_end_line") != end:
            problems.append(f"{label} excerpt_end_line does not match end_line")
        quote = record.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            problems.append(f"{label} quote must be usable and non-empty")
        excerpt = record.get("excerpt")
        if excerpt is not None and (not isinstance(excerpt, str) or not excerpt.strip()):
            problems.append(f"{label} excerpt must be usable when present")
        if valid_start and valid_end:
            _validate_locator(record.get("locator"), f"{label}.locator", int(start), int(end), problems)
        for key, pattern in (("statementRevisionIds", STATEMENT_REVISION_ID_RE), ("compositionRevisionIds", COMPOSITION_REVISION_ID_RE)):
            refs = _revision_array(record.get(key, []), f"{label}.{key}", pattern, problems)
            if refs is not None and not refs:
                # Empty reverse closures are valid for an unreferenced registry record.
                pass
        if "display_label" in record:
            _nonempty_string(record.get("display_label"), f"{label}.display_label", problems)
        if "source_hash" in record and (not isinstance(record.get("source_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("source_hash")))):
            problems.append(f"{label}.source_hash must be 64 lowercase hex characters")
    if revision is not None:
        structure, _ = _read_json(root / "wiki" / "structure.json", "wiki/structure.json")
        if structure is not None and structure.get("bookRevisionId") != revision:
            problems.append("structure and evidence bookRevisionId values differ")
    return sorted(set(problems))


def citation_health(root: Path) -> list[str]:
    """Resolve every Grounded v2 evidence URI in Markdown and reject CnEn."""
    problems: list[str] = []
    evidence_path = root / "wiki" / "evidence.json"
    records: dict[str, Any] = {}
    if evidence_path.is_file():
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("evidence"), dict):
                records = payload["evidence"]
        except (OSError, ValueError):
            pass
    wiki = root / "wiki"
    for page in wiki.rglob("*.md") if wiki.is_dir() else []:
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in ONEBOOKWIKI_EVIDENCE_RE.finditer(text):
            evidence_id = match.group(1).rstrip(".,;:，。；：")
            if not EVIDENCE_ID_RE.fullmatch(evidence_id):
                problems.append(f"invalid Grounded v2 evidence URI in {relative(root, page)}: {evidence_id}")
            elif evidence_id not in records:
                problems.append(f"unresolved Grounded v2 evidence URI in {relative(root, page)}: {evidence_id}")
        if CNEN_RE.search(text):
            problems.append(f"visible legacy CnEn citation in {relative(root, page)}")
    return sorted(set(problems))


def print_section(title: str, values: list[str], quiet: bool = False) -> int:
    if quiet:
        return len(values)
    print(f"## {title}")
    if values:
        for value in values:
            print(f"- {value}")
    else:
        print("(none)")
    print()
    return len(values)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    arguments = [argument for argument in argv[1:] if argument != "--json"]
    json_output = "--json" in argv[1:]
    root = Path(arguments[0]).resolve() if arguments else Path.cwd()
    if not (root / "raw" / "chapters").is_dir():
        print(f"expected raw/chapters/ under {root}", file=sys.stderr)
        return 1
    if not json_output:
        print("# OneBookWiki check\n")
    raw_count = print_section("Raw chapters", raw_chapter_health(root), json_output)
    rag_count = print_section("RAG index", rag_health(root), json_output)
    link_count = print_section("Links", broken_links(root), json_output)
    pdf_count = print_section("PDF structure", pdf_health(root), json_output)
    structure_count = print_section("Wiki structure", structure_health(root), json_output)
    evidence_count = print_section("Wiki evidence", evidence_health(root), json_output)
    citation_count = print_section("Citations", citation_health(root), json_output)
    total = raw_count + rag_count + link_count + pdf_count + structure_count + evidence_count + citation_count
    if json_output:
        print(json.dumps({"total": total, "status": "ok" if total == 0 else "issues"}, ensure_ascii=False))
    else:
        print(f"## Summary\n{total} issue(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
