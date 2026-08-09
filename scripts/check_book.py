#!/usr/bin/env python3
"""Read-only structure, evidence, and RAG-index checker for OneBookWiki.

Usage: check_book.py [project-root] [wiki-file ...]

The report is the interface. Findings do not change source or derived files.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from onebookwiki.markdown import Candidate, contains, extract_candidates, metadata, parse_document, raw_links_of, source_content
from onebookwiki.pdf_manifest import manifest_digest, validate_manifest

CHAPTER_FILE_RE = re.compile(r"^(\d+)[-_](.+)\.md$")
CHAPTER_META_RE = re.compile(r"^\d+$")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
NO_MATERIAL_RE = re.compile(r"^## \[[^\]]*\]\s*ingest\s*\|\s*no material:\s*`?(\S+?)`?\s*$", re.IGNORECASE)
SKIP_ARTICLES = {"index.md", "log.md", "book.md"}
INTERNAL_CITATION_RE = re.compile(r"(?<![\\w/])C\\d+E\\d+(?![\\w/])")

@dataclass(frozen=True)
class Chapter:
    path: Path
    number: int | None
    title: str
    raw_links: tuple[str, ...]


def relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def chapter_from_path(root: Path, path: Path) -> Chapter:
    text = path.read_text(encoding="utf-8")
    document = parse_document(text)
    values = metadata(document)
    raw_number = values.get("chapter", "")
    number = int(raw_number) if CHAPTER_META_RE.fullmatch(raw_number) else None
    title = (document.title or path.stem).removeprefix("# ")
    return Chapter(path, number, title, tuple(raw_links_of(text)))


def chapter_pages(root: Path) -> list[Chapter]:
    directory = root / "wiki" / "chapters"
    if not directory.is_dir():
        return []
    return [chapter_from_path(root, path) for path in sorted(directory.glob("*.md"))]


def evidence_for(chapter: Chapter, root: Path) -> tuple[list[str], list[str]]:
    text = chapter.path.read_text(encoding="utf-8")
    links = list(chapter.raw_links)
    errors: list[str] = []
    if len(links) != 1:
        errors.append("chapter must have exactly one Raw link")
        return [], errors
    raw_root = (root / "raw" / "chapters").resolve()
    target = (chapter.path.parent / links[0]).resolve()
    if not target.is_relative_to(raw_root):
        errors.append(f"Raw link escapes raw/chapters/: {links[0]}")
        return [], errors
    if not target.is_file():
        errors.append(f"unresolvable Raw link: {links[0]}")
        return [], errors
    raw = source_content(target)
    raw_chapter = metadata(parse_document(target.read_text(encoding="utf-8"))).get("chapter", "")
    if chapter.number is not None and raw_chapter != str(chapter.number):
        errors.append(f"Raw chapter metadata is {raw_chapter or 'missing'}, expected {chapter.number}")
    misses = [candidate.value for candidate in extract_candidates(text) if not contains(raw, Candidate(candidate.kind, candidate.value))]
    return misses, errors


def chapter_order(chapters: list[Chapter]) -> list[str]:
    problems: list[str] = []
    numbers = [chapter.number for chapter in chapters if chapter.number is not None]
    for chapter in chapters:
        match = CHAPTER_FILE_RE.match(chapter.path.name)
        if chapter.number is None:
            problems.append(f"{chapter.path.name}: missing or malformed Chapter metadata")
        elif not match:
            problems.append(f"{chapter.path.name}: filename lacks chapter number")
        elif int(match.group(1)) != chapter.number:
            problems.append(f"{chapter.path.name}: filename number does not match Chapter {chapter.number}")
    for number, count in Counter(numbers).items():
        if count > 1:
            problems.append(f"duplicate chapter number: {number}")
    if numbers:
        expected = set(range(1, max(numbers) + 1))
        for missing in sorted(expected - set(numbers)):
            problems.append(f"missing chapter number: {missing}")
    return problems


def map_problems(root: Path, chapters: list[Chapter]) -> list[str]:
    index = root / "wiki" / "index.md"
    if not index.is_file():
        return ["wiki/index.md is missing"]
    lines = index.read_text(encoding="utf-8").splitlines()
    listed: list[str] = []
    for line in lines:
        if "chapters/" not in line:
            continue
        listed.extend(target.split("#", 1)[0] for target in LINK_RE.findall(line) if target.startswith("chapters/"))
    expected = [f"chapters/{chapter.path.name}" for chapter in sorted(chapters, key=lambda item: item.number or 0)]
    problems = []
    for item in expected:
        if item not in listed:
            problems.append(f"reading map missing chapter row: {item}")
    for item in listed:
        if not (root / "wiki" / item).is_file():
            problems.append(f"reading map links to missing file: {item}")
    ordered = [item for item in listed if item in expected]
    if ordered != [item for item in expected if item in ordered]:
        problems.append("reading map chapter rows are not numerically ordered")
    return problems


def broken_links(root: Path) -> list[str]:
    problems: list[str] = []
    wiki = root / "wiki"
    for path in wiki.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            target = target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            candidate = (path.parent / target).resolve()
            if not candidate.is_file():
                problems.append(f"{relative(root, path)} -> {target}")
    return sorted(set(problems))


def relationship_coverage(root: Path, chapters: list[Chapter]) -> list[str]:
    known = {chapter.path.resolve() for chapter in chapters}
    problems: list[str] = []
    for group in ("themes", "concepts", "arguments"):
        directory = root / "wiki" / group
        if not directory.is_dir():
            continue
        for page in directory.glob("*.md"):
            targets = []
            for target in LINK_RE.findall(page.read_text(encoding="utf-8")):
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                candidate = (page.parent / target.split("#", 1)[0]).resolve()
                if candidate in known:
                    targets.append(candidate)
            if not targets:
                problems.append(f"{relative(root, page)} has no chapter evidence link")
    return problems


def no_material_paths(log: Path) -> set[str]:
    if not log.is_file():
        return set()
    paths: set[str] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        match = NO_MATERIAL_RE.match(line)
        if match:
            paths.add(match.group(1).strip("`,;."))
    return paths


def raw_inventory(root: Path, chapters: list[Chapter]) -> list[str]:
    referenced = set()
    for chapter in chapters:
        for link in chapter.raw_links:
            referenced.add((chapter.path.parent / link).resolve())
    ignored = no_material_paths(root / "wiki" / "log.md")
    missing = []
    for path in sorted((root / "raw" / "chapters").glob("*.md")):
        if path.resolve() not in referenced and relative(root, path) not in ignored:
            missing.append(relative(root, path))
    return missing


def rag_health(root: Path) -> list[str]:
    manifest_path = root / ".onebookwiki" / "manifest.json"
    if not manifest_path.is_file():
        return ["no manifest; run ingest_book.py index"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return ["manifest is not valid JSON"]
    chunks = manifest.get("chunks", {})
    problems = []
    for raw in sorted((root / "raw" / "chapters").glob("*.md")):
        key = relative(root, raw)
        chapter = manifest.get("chapters", {}).get(key)
        if not chapter:
            problems.append(f"raw chapter not indexed: {key}")
            continue
        actual = __import__("hashlib").sha256(raw.read_bytes()).hexdigest()
        if chapter.get("content_hash") != actual:
            problems.append(f"stale chapter hash: {key}")
        ids = chapter.get("chunk_ids", [])
        if not ids:
            problems.append(f"chapter has no chunks: {key}")
        for chunk_id in ids:
            chunk = chunks.get(chunk_id)
            if not chunk:
                problems.append(f"manifest references missing chunk: {chunk_id}")
            elif not chunk.get("start_line") or not chunk.get("end_line"):
                problems.append(f"chunk has no line range: {chunk_id}")
    return problems


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


def pdf_manifest_health(root: Path, source_metadata: dict[str, object], report: dict[str, object]) -> list[str]:
    """Verify optional PDF structure-manifest provenance without touching source files."""
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
    metadata_root = root.resolve()
    if not snapshot.is_relative_to(metadata_root) or not snapshot.is_file():
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


def structure_health(root: Path) -> list[str]:
    """Validate the canonical page graph emitted with generated wiki pages."""
    structure_path = root / "wiki" / "structure.json"
    artifacts = root / ".onebookwiki" / "artifacts"
    if not structure_path.is_file():
        return ["wiki/structure.json is missing"] if (artifacts / "book.json").is_file() else []
    try:
        value = json.loads(structure_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"wiki/structure.json is invalid JSON: {error}"]
    if not isinstance(value, dict):
        return ["wiki/structure.json must be a JSON object"]
    pages = value.get("pages")
    sections = value.get("sections")
    if not isinstance(pages, list) or not isinstance(sections, list):
        return ["wiki/structure.json requires pages and sections arrays"]
    problems: list[str] = []
    page_ids: set[str] = set()
    page_paths: set[str] = set()
    wiki_root = (root / "wiki").resolve()
    for index, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            problems.append(f"structure page {index} is not an object")
            continue
        page_id = page.get("id")
        page_path = page.get("path")
        if not isinstance(page_id, str) or not page_id:
            problems.append(f"structure page {index} has no id")
            continue
        if page_id in page_ids:
            problems.append(f"structure has duplicate page id: {page_id}")
        page_ids.add(page_id)
        if not isinstance(page_path, str) or not page_path:
            problems.append(f"structure page {page_id} has no path")
            continue
        if page_path in page_paths:
            problems.append(f"structure has duplicate page path: {page_path}")
        page_paths.add(page_path)
        target = (wiki_root / page_path).resolve()
        if not target.is_relative_to(wiki_root) or not target.is_file():
            problems.append(f"structure page {page_id} has missing or unsafe path: {page_path}")
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("id"), str):
            continue
        for related in page.get("relatedPages", []):
            if related not in page_ids:
                problems.append(f"structure page {page['id']} references unknown related page: {related}")

    def check_outline(nodes: object, prefix: str = "sourceOutline") -> None:
        if not isinstance(nodes, list):
            problems.append(f"{prefix} must be an array")
            return
        node_ids: set[str] = set()
        def visit(items: list[object], location: str) -> None:
            for index, node in enumerate(items, 1):
                node_location = f"{location}[{index}]"
                if not isinstance(node, dict):
                    problems.append(f"{node_location} is not an object")
                    continue
                node_id = node.get("id")
                if not isinstance(node_id, str) or not node_id:
                    problems.append(f"{node_location} has no id")
                elif node_id in node_ids:
                    problems.append(f"sourceOutline has duplicate node id: {node_id}")
                else:
                    node_ids.add(node_id)
                page_ids = node.get("pageIds", [])
                if not isinstance(page_ids, list):
                    problems.append(f"{node_location} pageIds must be an array")
                else:
                    for page_id in page_ids:
                        if page_id not in page_ids_all:
                            problems.append(f"{node_location} references unknown page: {page_id}")
                children = node.get("children", [])
                if not isinstance(children, list):
                    problems.append(f"{node_location} children must be an array")
                else:
                    visit(children, f"{node_location}.children")
        page_ids_all = page_ids
        visit(nodes, prefix)

    if "sourceOutline" in value:
        check_outline(value.get("sourceOutline"))
    section_ids: set[str] = set()
    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            problems.append(f"structure section {index} is not an object")
            continue
        section_id = section.get("id")
        if not isinstance(section_id, str) or not section_id:
            problems.append(f"structure section {index} has no id")
        elif section_id in section_ids:
            problems.append(f"structure has duplicate section id: {section_id}")
        else:
            section_ids.add(section_id)
        for page_id in section.get("pages", []):
            if page_id not in page_ids:
                problems.append(f"structure section {section_id or index} references unknown page: {page_id}")
    expected_paths = {
        path.relative_to(wiki_root).as_posix()
        for path in wiki_root.rglob("*.md")
        if path.name != "log.md"
    }
    for page_path in sorted(expected_paths - page_paths):
        problems.append(f"structure omits wiki page: {page_path}")
    for page_path in sorted(page_paths - expected_paths):
        problems.append(f"structure references non-page: {page_path}")
    report_path = root / ".onebookwiki" / "structure-report.json"
    source_path = root / ".onebookwiki" / "source.json"
    source_format = ""
    source_metadata: dict[str, object] | None = None
    if source_path.is_file():
        try:
            parsed_source = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(parsed_source, dict):
                problems.append(".onebookwiki/source.json must be a JSON object")
            else:
                source_metadata = parsed_source
                source_format = str(source_metadata.get("format", "")).upper()
        except (OSError, ValueError):
            problems.append(".onebookwiki/source.json is invalid JSON")
    if source_format == "PDF":
        if not report_path.is_file():
            problems.append("PDF source is missing .onebookwiki/structure-report.json")
        else:
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if not isinstance(report, dict):
                    problems.append("structure report must be a JSON object")
                elif report.get("ocr") != "disabled":
                    problems.append("structure report must declare OCR disabled")
                elif not isinstance(report.get("selected_method"), str):
                    problems.append("structure report has no selected_method")
                else:
                    postprocess = report.get("postprocess")
                    if postprocess is not None:
                        if not isinstance(postprocess, dict):
                            problems.append("structure report postprocess must be an object")
                        else:
                            mode = postprocess.get("mode")
                            if mode not in {"off", "auto", "strict"}:
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
                    if source_metadata is not None:
                        problems.extend(pdf_manifest_health(root, source_metadata, report))
            except (OSError, ValueError) as error:
                problems.append(f"structure report is invalid JSON: {error}")
    return sorted(set(problems))


def citation_health(root: Path) -> list[str]:
    """Validate the optional generated evidence index and visible citation labels."""
    problems: list[str] = []
    evidence_path = root / "wiki" / "evidence.json"
    if evidence_path.is_file():
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            records = payload.get("evidence", {})
            if not isinstance(records, dict):
                return ["wiki/evidence.json evidence must be an object"]
            for evidence_id, record in records.items():
                if not isinstance(record, dict):
                    problems.append(f"evidence record is not an object: {evidence_id}")
                    continue
                if not record.get("display_label"):
                    problems.append(f"evidence record has no display label: {evidence_id}")
                locator = record.get("locator")
                if not isinstance(locator, dict) or not locator.get("format"):
                    problems.append(f"evidence record has no locator: {evidence_id}")
        except (OSError, ValueError) as error:
            problems.append(f"wiki/evidence.json is invalid JSON: {error}")
    for page in (root / "wiki").rglob("*.md") if (root / "wiki").is_dir() else []:
        if INTERNAL_CITATION_RE.search(page.read_text(encoding="utf-8")):
            problems.append(f"visible internal citation id in {relative(root, page)}")
    return sorted(set(problems))


def generated_health(root: Path) -> list[str]:
    """Check generated artifacts and resumable generation state."""
    problems: list[str] = []
    artifact_root = root / ".onebookwiki" / "artifacts"
    book = artifact_root / "book.json"
    chapter_dir = artifact_root / "chapters"
    if artifact_root.exists() and not book.is_file():
        problems.append("generated artifacts missing book.json")
    if chapter_dir.is_dir():
        for path in chapter_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                problems.append(f"invalid generated artifact {relative(root, path)}: {error}")
                continue
            if not value.get("title") or not value.get("executive_summary"):
                problems.append(f"generated chapter is missing title or summary: {relative(root, path)}")
            if not value.get("evidence"):
                problems.append(f"generated chapter has no evidence: {relative(root, path)}")
            if "Error generating content" in json.dumps(value, ensure_ascii=False):
                problems.append(f"generated artifact contains an error placeholder: {relative(root, path)}")
    checkpoint = root / ".onebookwiki" / "checkpoints" / "latest.json"
    if checkpoint.is_file():
        try:
            latest = json.loads(checkpoint.read_text(encoding="utf-8"))
            run_id = latest.get("run_id")
            run_path = checkpoint.parent / f"run-{run_id}.json"
            state = json.loads(run_path.read_text(encoding="utf-8"))
            for node_id, node in state.get("nodes", {}).items():
                if node.get("status") in {"failed", "running"}:
                    problems.append(f"generation node {node_id} is {node.get('status')}")
        except (OSError, ValueError, TypeError) as error:
            problems.append(f"invalid generation checkpoint: {error}")
    usage = root / ".onebookwiki" / "usage.jsonl"
    if usage.is_file():
        for line_number, line in enumerate(usage.read_text(encoding="utf-8").splitlines(), 1):
            try:
                json.loads(line)
            except ValueError:
                problems.append(f"usage.jsonl:{line_number} is invalid JSON")
    return problems


def main(argv: list[str]) -> int:
    arguments = list(argv[1:])
    json_output = "--json" in arguments
    arguments = [argument for argument in arguments if argument != "--json"]
    root = Path(arguments[0]).resolve() if arguments else Path.cwd()
    if not (root / "wiki").is_dir() or not (root / "raw" / "chapters").is_dir():
        print(f"expected wiki/ and raw/chapters/ under {root}", file=sys.stderr)
        return 1
    chapters = chapter_pages(root)
    evidence_misses: list[str] = []
    evidence_errors: list[str] = []
    selected = chapters
    if len(arguments) > 1:
        requested = set()
        for argument in arguments[1:]:
            path = Path(argument)
            if not path.is_absolute(): path = root / path
            if not path.is_file():
                print(f"warning: file not found: {argument}", file=sys.stderr); continue
            requested.add(path.resolve())
        selected = [chapter for chapter in chapters if chapter.path.resolve() in requested]
    for chapter in selected:
        misses, errors = evidence_for(chapter, root)
        evidence_misses.extend(f"{relative(root, chapter.path)}: {value}" for value in misses)
        evidence_errors.extend(f"{relative(root, chapter.path)}: {value}" for value in errors)
    if not json_output:
        print("# OneBookWiki check\n")
    evidence_count = print_section("Evidence", evidence_misses + evidence_errors, json_output)
    order_count = print_section("Chapter order", chapter_order(chapters), json_output)
    map_count = print_section("Reading map", map_problems(root, chapters), json_output)
    link_count = print_section("Links", broken_links(root), json_output)
    coverage_count = print_section("Relationship coverage", relationship_coverage(root, chapters), json_output)
    rag_count = print_section("RAG index", rag_health(root), json_output)
    inventory_count = print_section("Unreferenced raw", raw_inventory(root, chapters), json_output)
    structure_count = print_section("Wiki structure", structure_health(root), json_output)
    generated_count = print_section("Generated artifacts", generated_health(root), json_output)
    citation_count = print_section("Citations", citation_health(root), json_output)
    total = evidence_count + order_count + map_count + link_count + coverage_count + rag_count + inventory_count + structure_count + generated_count + citation_count
    if json_output:
        print(json.dumps({"total": total, "status": "ok" if total == 0 else "issues"}, ensure_ascii=False))
    else:
        print(f"## Summary\n{total} issue(s)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
