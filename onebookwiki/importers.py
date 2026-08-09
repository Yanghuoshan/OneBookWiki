"""Dependency-light EPUB and source metadata import helpers."""
from __future__ import annotations

import hashlib
import html
import json
import posixpath
import re
import unicodedata
import zipfile
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .source_structure import (
    DEFAULT_MAX_UNIT_TOKENS,
    PageBlock,
    PdfLine,
    PdfPage,
    PdfSpan,
    _kind,
    analyse_pdf,
    reading_part_dict,
    source_node_dict,
    split_unit_into_parts,
)
from .pdf_manifest import load_manifest
from .pdf_postprocess import postprocess_pdf_pages


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(value: str, fallback: str = "book") -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    result = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return result or fallback


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(root: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next((node for node in root.iter() if _local_name(node.tag) == name), None)


def _findall(root: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [node for node in root.iter() if _local_name(node.tag) == name]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.block = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "tr"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "nav"}:
            self.skip += 1
        if not self.skip and tag in self.block:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "nav"} and self.skip:
            self.skip -= 1
        if not self.skip and tag in self.block:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    text = html.unescape("".join(parser.parts))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n\n".join(lines)


def _read_text(archive: zipfile.ZipFile, path: str) -> str:
    return archive.read(path).decode("utf-8", errors="replace")


def _container_rootfile(archive: zipfile.ZipFile) -> str:
    root = ElementTree.fromstring(_read_text(archive, "META-INF/container.xml"))
    node = _find(root, "rootfile")
    if node is None or not node.attrib.get("full-path"):
        raise ValueError("EPUB container has no OPF rootfile")
    return node.attrib["full-path"]


def _resolved_epub_href(opf_dir: str, href: str) -> tuple[str, str]:
    path, _, fragment = href.partition("#")
    return posixpath.normpath(posixpath.join(opf_dir, path)), fragment


def _direct_children(node: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(node) if _local_name(child.tag) == name]


def _ncx_outline(node: ElementTree.Element, opf_dir: str, breadcrumb: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for point in _direct_children(node, "navPoint"):
        label = _find(point, "text")
        content = _find(point, "content")
        title = " ".join("".join(label.itertext()).split()) if label is not None else ""
        source = content.attrib.get("src", "") if content is not None else ""
        if not title or not source:
            continue
        href, fragment = _resolved_epub_href(opf_dir, source)
        result.append({
            "title": title,
            "href": href,
            "fragment": fragment,
            "breadcrumb": [*breadcrumb, title],
            "children": _ncx_outline(point, opf_dir, (*breadcrumb, title)),
        })
    return result


def _nav_outline(node: ElementTree.Element, opf_dir: str, breadcrumb: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Parse one EPUB3 navigation list, retaining its authored hierarchy."""
    result: list[dict[str, Any]] = []
    for item in _direct_children(node, "li"):
        link = next((child for child in list(item) if _local_name(child.tag) == "a" and child.attrib.get("href")), None)
        if link is None:
            continue
        title = " ".join("".join(link.itertext()).split())
        if not title:
            continue
        href, fragment = _resolved_epub_href(opf_dir, link.attrib["href"])
        nested_list = next((child for child in list(item) if _local_name(child.tag) == "ol"), None)
        result.append({
            "title": title,
            "href": href,
            "fragment": fragment,
            "breadcrumb": [*breadcrumb, title],
            "children": _nav_outline(nested_list, opf_dir, (*breadcrumb, title)) if nested_list is not None else [],
        })
    return result


def _toc_outline(archive: zipfile.ZipFile, opf_dir: str, href: str, media_type: str = "") -> list[dict[str, Any]]:
    path = posixpath.normpath(posixpath.join(opf_dir, href))
    try:
        root = ElementTree.fromstring(_read_text(archive, path))
    except (KeyError, ElementTree.ParseError):
        return []
    if "ncx" in media_type or _find(root, "navMap") is not None:
        nav_map = _find(root, "navMap")
        return _ncx_outline(nav_map, opf_dir) if nav_map is not None else []
    for nav in _findall(root, "nav"):
        nav_type = " ".join(value for key, value in nav.attrib.items() if key.endswith("type") or key == "role").lower()
        if "toc" not in nav_type:
            continue
        ordered_list = next((child for child in list(nav) if _local_name(child.tag) == "ol"), None)
        if ordered_list is not None:
            return _nav_outline(ordered_list, opf_dir)
    return []


def _flatten_outline(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [node for root in nodes for node in [root, *_flatten_outline(list(root.get("children") or []))]]


def _toc_lookup(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Prefer a leaf navigation label when parent and child share one XHTML file."""
    result: dict[str, dict[str, Any]] = {}
    for node in _flatten_outline(nodes):
        href = str(node.get("href", ""))
        if not href:
            continue
        previous = result.get(href)
        if previous is None or (previous.get("children") and not node.get("children")):
            result[href] = node
    return result


def parse_epub(path: Path, keep_front_matter: bool = False) -> dict[str, Any]:
    """Return ordered EPUB spine entries and metadata without writing files."""
    with zipfile.ZipFile(path) as archive:
        opf_path = _container_rootfile(archive)
        opf_dir = posixpath.dirname(opf_path)
        root = ElementTree.fromstring(_read_text(archive, opf_path))
        metadata_node = _find(root, "metadata")
        metadata: dict[str, str] = {}
        if metadata_node is not None:
            for node in list(metadata_node):
                name = _local_name(node.tag)
                text = " ".join("".join(node.itertext()).split())
                if text and name not in metadata:
                    metadata[name] = text
        manifest: dict[str, dict[str, str]] = {}
        for item in _findall(root, "item"):
            item_id = item.attrib.get("id")
            href = item.attrib.get("href")
            if item_id and href:
                manifest[item_id] = {"href": href, "media_type": item.attrib.get("media-type", "")}
        spine = _find(root, "spine")
        toc_id = spine.attrib.get("toc") if spine is not None else None
        toc_outline: list[dict[str, Any]] = []
        if toc_id and toc_id in manifest:
            toc_outline = _toc_outline(archive, opf_dir, manifest[toc_id]["href"], manifest[toc_id]["media_type"])
        if not toc_outline:
            for item in manifest.values():
                if item["media_type"] == "application/x-dtbncx+xml":
                    toc_outline = _toc_outline(archive, opf_dir, item["href"], item["media_type"])
                elif "html" in item["media_type"] or "xhtml" in item["media_type"]:
                    candidate = _toc_outline(archive, opf_dir, item["href"], item["media_type"])
                    if candidate:
                        toc_outline = candidate
                if toc_outline:
                    break
        toc_entries = _toc_lookup(toc_outline)
        entries: list[dict[str, Any]] = []
        for spine_index, itemref in enumerate(_findall(spine, "itemref") if spine is not None else [], 1):
            item_id = itemref.attrib.get("idref")
            item = manifest.get(item_id or "")
            if not item or "html" not in item["media_type"] and "xhtml" not in item["media_type"]:
                continue
            href_value, _, fragment = item["href"].partition("#")
            href = posixpath.normpath(posixpath.join(opf_dir, href_value))
            body = html_to_text(_read_text(archive, href))
            body_lines = [line.strip() for line in body.splitlines() if line.strip()]
            toc_entry = toc_entries.get(href)
            title = str(toc_entry.get("title")) if toc_entry else (body_lines[0] if body_lines else f"Reading unit {spine_index}")
            if not body or (not keep_front_matter and spine_index == 1 and len(body.split()) < 40):
                continue
            entries.append({
                "number": len(entries) + 1,
                "title": title,
                "href": href,
                "fragment": str(toc_entry.get("fragment", fragment)) if toc_entry else fragment,
                "spine": item_id or "",
                "spine_index": spine_index,
                "breadcrumb": list(toc_entry.get("breadcrumb") or [title]) if toc_entry else [title],
                "text": body,
            })
        return {
            "metadata": metadata,
            "entries": entries,
            "toc_outline": toc_outline,
            "source_hash": sha256_file(path),
            "format": "EPUB",
        }


def write_source_metadata(root: Path, source: dict[str, Any]) -> Path:
    target = root / ".onebookwiki" / "source.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    import json
    target.write_text(json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def extract_pdf_pages(source: Path) -> list[str]:
    """Extract native PDF page text through the optional PyMuPDF dependency."""
    return [page.text for page in extract_pdf_layout(source)[0]]


def extract_pdf_layout(source: Path) -> tuple[list[PdfPage], list[dict[str, Any]]]:
    """Extract the established native view and a coordinate tree without OCR."""
    try:
        import fitz
    except ImportError as exc:
        raise ValueError("Install PyMuPDF with: pip install pymupdf") from exc
    document = fitz.open(source)
    pages: list[PdfPage] = []
    for number, page in enumerate(document, 1):
        blocks: list[PageBlock] = []
        page_lines: list[PdfLine] = []
        layout = page.get_text("dict", sort=True)
        for block in layout.get("blocks", []):
            if block.get("type") != 0:
                continue
            block_lines: list[PdfLine] = []
            for raw_line in block.get("lines", []):
                raw_spans = raw_line.get("spans", [])
                spans = tuple(PdfSpan(
                    text=str(span.get("text", "")),
                    x0=float(span.get("bbox", (0, 0, 0, 0))[0]),
                    y0=float(span.get("bbox", (0, 0, 0, 0))[1]),
                    x1=float(span.get("bbox", (0, 0, 0, 0))[2]),
                    y1=float(span.get("bbox", (0, 0, 0, 0))[3]),
                    size=float(span.get("size", 0.0)),
                    flags=int(span.get("flags", 0)),
                    font=str(span.get("font", "")),
                ) for span in raw_spans if str(span.get("text", "")).strip())
                text = " ".join(span.text.strip() for span in spans if span.text.strip())
                if not text:
                    continue
                bbox = raw_line.get("bbox", (0, 0, 0, 0))
                line = PdfLine(
                    text=text,
                    x0=float(bbox[0]), y0=float(bbox[1]), x1=float(bbox[2]), y1=float(bbox[3]),
                    size=max((span.size for span in spans), default=0.0), spans=spans,
                )
                block_lines.append(line)
                page_lines.append(line)
            if not block_lines:
                continue
            bbox = block.get("bbox", (0, 0, 0, 0))
            blocks.append(PageBlock(
                text="\n".join(line.text for line in block_lines),
                x0=float(bbox[0]), y0=float(bbox[1]), x1=float(bbox[2]), y1=float(bbox[3]),
                size=max((line.size for line in block_lines), default=0.0),
                lines=tuple(block_lines),
            ))
        pages.append(PdfPage(
            number=number,
            # Preserve the pre-existing native text view for analyse_pdf().  The
            # coordinate tree below is exclusively for the processed payload.
            text=page.get_text("text", sort=True),
            blocks=tuple(blocks),
            height=float(page.rect.height),
            width=float(page.rect.width),
            lines=tuple(page_lines),
        ))
    bookmarks: list[dict[str, Any]] = []
    for entry in document.get_toc(simple=True) or []:
        if len(entry) < 3:
            continue
        level, title, page_number = entry[:3]
        if isinstance(page_number, int) and page_number > 0:
            bookmarks.append({"level": int(level), "title": str(title), "page": page_number})
    return pages, bookmarks


def pdf_chapter_ranges(page_count: int, pages_per_chapter: int | None, chapter_count: int | None) -> list[tuple[int, int]]:
    if pages_per_chapter:
        return [(start, min(page_count, start + pages_per_chapter)) for start in range(0, page_count, pages_per_chapter)]
    if chapter_count:
        size = max(1, (page_count + chapter_count - 1) // chapter_count)
        return [(start, min(page_count, start + size)) for start in range(0, page_count, size)]
    return [(0, page_count)]


def _clear_raw_chapters(root: Path) -> None:
    """Remove stale imported units before a forced source rebuild."""
    output = root / "raw" / "chapters"
    if output.is_dir():
        for path in output.glob("*.md"):
            path.unlink()


def import_pdf(
    source: Path,
    root: Path,
    title: str | None = None,
    pages_per_chapter: int | None = None,
    chapter_count: int | None = None,
    force: bool = False,
    pages: list[str] | None = None,
    *,
    structure: str = "auto",
    structure_min_confidence: float = 0.72,
    structure_report: Path | None = None,
    max_unit_tokens: int = DEFAULT_MAX_UNIT_TOKENS,
    postprocess: str = "auto",
    structure_manifest: Path | None = None,
) -> list[Path]:
    """Import a PDF as bounded source units without OCR or semantic cleanup.

    Structure analysis intentionally uses the native page view.  The selected
    processed page view is used only after physical page ranges are fixed.
    """
    if structure not in {"auto", "bookmarks", "toc", "headings", "ranges"}:
        raise ValueError(f"unsupported PDF structure mode: {structure}")
    if postprocess not in {"off", "auto", "strict"}:
        raise ValueError(f"unsupported PDF postprocess mode: {postprocess}")
    if pages is None:
        native_pages, bookmarks = extract_pdf_layout(source)
    else:
        native_pages = [PdfPage(number=index, text=text) for index, text in enumerate(pages, 1)]
        bookmarks = []
    source_hash = sha256_file(source)
    manifest = None
    manifest_hash = None
    if structure_manifest is not None:
        manifest, manifest_hash = load_manifest(
            structure_manifest,
            source=source,
            page_count=len(native_pages),
            source_hash=source_hash,
        )
    analysis = analyse_pdf(
        native_pages,
        source,
        explicit_title=title,
        bookmarks=bookmarks,
        mode=structure,
        min_confidence=structure_min_confidence,
        pages_per_chapter=pages_per_chapter,
        chapter_count=chapter_count,
        structure_manifest=manifest,
    )
    processed = postprocess_pdf_pages(native_pages, mode=postprocess)
    processed_pages = list(processed.pages)
    analysis.report["postprocess"] = processed.report
    manifest_metadata = None
    if manifest is not None and manifest_hash is not None:
        snapshot = root / ".onebookwiki" / "structure-manifest.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_metadata = {
            "id": manifest["id"],
            "status": manifest.get("status", "ready"),
            "hash": manifest_hash,
            "source_match": {"filename": True, "page_count": True, "sha256": True},
            "application_mode": "explicit_units" if manifest.get("units") else "rules_only",
            "snapshot_path": snapshot.relative_to(root).as_posix(),
        }
        analysis.report["structure_manifest"] = manifest_metadata
    book_title = analysis.title.value
    if force:
        _clear_raw_chapters(root)
    output = root / "raw" / "chapters"
    output.mkdir(parents=True, exist_ok=True)
    report_path = structure_report or root / ".onebookwiki" / "structure-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(analysis.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    written: list[Path] = []
    reading_units: list[dict[str, Any]] = []
    chapter = 1
    for unit in analysis.units:
        for part in split_unit_into_parts(unit, processed_pages, max_unit_tokens):
            fallback = f"unit-{chapter:02d}"
            filename = f"{chapter:02d}-{slugify(part.title, fallback)}.md"
            target = output / filename
            raw_path = target.relative_to(root).as_posix()
            if not target.exists() or force:
                grouped: dict[int, list[str]] = {}
                for page_number, fragment in part.page_fragments:
                    grouped.setdefault(page_number, []).append(fragment)
                page_blocks = [
                    f"## Page {page_number}\n\n{page_text}"
                    for page_number, fragments in grouped.items()
                    if (page_text := "\n\n".join(fragments).strip())
                ]
                body = "\n\n".join(page_blocks)
                breadcrumb = " › ".join(part.breadcrumb)
                target.write_text(
                    f"# {part.title}\n\n"
                    f"> Book: {book_title}\n> Chapter: {chapter}\n> Source Unit: {part.source_unit_id}\n"
                    f"> Breadcrumb: {breadcrumb}\n> Type: {part.kind}\n> Confidence: {part.confidence:.2f}\n"
                    f"> Part: {part.part}/{part.part_count}\n> Pages: {part.physical_page_start}-{part.physical_page_end}\n"
                    f"> Source: {source.name}\n> Collected: {date.today().isoformat()}\n> Format: PDF\n\n{body}\n",
                    encoding="utf-8",
                )
            written.append(target)
            reading_units.append(reading_part_dict(part, chapter, raw_path))
            chapter += 1

    by_source: dict[str, list[str]] = {}
    for record in reading_units:
        by_source.setdefault(str(record["source_unit_id"]), []).append(f"chapter-{int(record['chapter']):02d}")
    def outline_pages(node: dict[str, Any]) -> dict[str, Any]:
        value = dict(node)
        value["pageIds"] = [page_id for unit_id in value.pop("unitIds", []) for page_id in by_source.get(unit_id, [])]
        value["children"] = [outline_pages(child) for child in value.get("children", [])]
        return value

    outline = [outline_pages(source_node_dict(node)) for node in analysis.outline]
    write_source_metadata(root, {
        "schema_version": 3,
        "title": book_title,
        "title_method": analysis.title.method,
        "title_confidence": analysis.title.confidence,
        "format": "PDF",
        "source_name": source.name,
        "source_hash": source_hash,
        "page_count": len(native_pages),
        "pages_per_chapter": pages_per_chapter,
        "chapter_count": chapter_count,
        "locator_policy": "pdf-physical-page-1-based",
        "source_processing": {
            "format": "PDF",
            "structure_view": "native",
            "payload_view": processed.report["payload_view"],
            "steps": [processed.report],
        },
        "source_structure": {
            "method": analysis.method,
            "confidence": analysis.confidence,
            "outline": outline,
            "units": reading_units,
            "report_path": report_path.relative_to(root).as_posix() if report_path.is_relative_to(root) else str(report_path),
            **({"structure_manifest": manifest_metadata} if manifest_metadata is not None else {}),
        },
        "chapters": reading_units,
    })
    return written


def import_epub(source: Path, root: Path, title: str | None = None, force: bool = False, keep_front_matter: bool = False) -> list[Path]:
    """Import EPUB spine entries using the same semantic unit protocol as PDFs."""
    parsed = parse_epub(source, keep_front_matter=keep_front_matter)
    metadata = parsed["metadata"]
    metadata_title = " ".join(str(metadata.get("title", "")).split())
    explicit_title = " ".join(title.split()) if title else ""
    book_title = explicit_title or metadata_title or source.stem.replace("_", " ")
    title_method = "explicit" if explicit_title else "epub_metadata" if metadata_title else "filename"
    title_confidence = 1.0 if title_method == "explicit" else 0.98 if title_method == "epub_metadata" else 0.35
    if force:
        _clear_raw_chapters(root)
    output = root / "raw" / "chapters"
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    reading_units: list[dict[str, Any]] = []
    units_by_href: dict[str, dict[str, Any]] = {}
    for entry in parsed["entries"]:
        number = int(entry["number"])
        entry_title = " ".join(str(entry["title"]).split()) or f"Reading unit {number}"
        fallback = f"chapter-{number}"
        filename = f"{number:02d}-{slugify(entry_title, fallback)}.md"
        target = output / filename
        raw_path = target.relative_to(root).as_posix()
        source_unit_id = f"epub-unit-{number:02d}"
        kind = _kind(entry_title)
        record = {
            "chapter": number,
            "source_unit_id": source_unit_id,
            "title": entry_title,
            "source_title": entry_title,
            "kind": kind,
            "breadcrumb": list(entry.get("breadcrumb") or [entry_title]),
            "confidence": 0.98,
            "part": 1,
            "part_count": 1,
            "raw_path": raw_path,
            "spine": entry["spine"],
            "spine_index": entry.get("spine_index"),
            "href": entry["href"],
            "fragment": entry.get("fragment", ""),
        }
        if not target.exists() or force:
            content = (
                f"# {entry_title}\n\n"
                f"> Book: {book_title}\n> Author: {metadata.get('creator', 'Unknown')}\n"
                f"> Edition: {metadata.get('edition', 'Unknown')}\n> Chapter: {number}\n"
                f"> Source Unit: {source_unit_id}\n> Breadcrumb: {' › '.join(record['breadcrumb'])}\n> Type: {kind}\n"
                f"> Confidence: 0.98\n> Source: {source.name}\n> Format: EPUB\n> Spine: {entry['spine']}\n"
                f"> Spine Index: {entry.get('spine_index', '')}\n> Href: {entry['href']}\n"
                f"> Fragment: {entry.get('fragment', '')}\n> Collected: {date.today().isoformat()}\n\n"
                f"{entry['text']}\n"
            )
            target.write_text(content, encoding="utf-8")
        written.append(target)
        reading_units.append(record)
        units_by_href.setdefault(str(entry["href"]), record)

    def outline_node(node: dict[str, Any], position: list[int], assigned_page_ids: set[str]) -> dict[str, Any]:
        position[0] += 1
        node_id = f"epub-outline-{position[0]:03d}"
        node_title = " ".join(str(node.get("title", "")).split()) or "Reading unit"
        breadcrumb = list(node.get("breadcrumb") or [node_title])
        children = [
            outline_node(child, position, assigned_page_ids)
            for child in list(node.get("children") or [])
            if isinstance(child, dict)
        ]
        href = str(node.get("href", ""))
        unit = units_by_href.get(href)
        page_id = f"chapter-{int(unit['chapter']):02d}" if unit else ""
        # A section parent commonly points to the first child document. Bind that
        # page to the deepest authored node only, rather than duplicating it in
        # the reader tree and making both entries open the same article.
        page_ids = [page_id] if page_id and page_id not in assigned_page_ids else []
        assigned_page_ids.update(page_ids)
        return {
            "id": node_id,
            "title": node_title,
            "kind": _kind(node_title),
            "breadcrumb": breadcrumb,
            "confidence": 0.98,
            "pageIds": page_ids,
            "children": children,
        }

    toc_outline = list(parsed.get("toc_outline") or [])
    outline_position = [0]
    assigned_outline_page_ids: set[str] = set()
    outline = [outline_node(node, outline_position, assigned_outline_page_ids) for node in toc_outline if isinstance(node, dict)]
    if not outline:
        outline = [{
            "id": str(record["source_unit_id"]),
            "title": str(record["title"]),
            "kind": str(record["kind"]),
            "breadcrumb": list(record["breadcrumb"]),
            "confidence": float(record["confidence"]),
            "pageIds": [f"chapter-{int(record['chapter']):02d}"],
            "children": [],
        } for record in reading_units]
    write_source_metadata(root, {
        "schema_version": 3,
        "title": book_title,
        "title_method": title_method,
        "title_confidence": title_confidence,
        "author": metadata.get("creator", "Unknown"),
        "format": "EPUB",
        "source_name": source.name,
        "source_hash": parsed["source_hash"],
        "locator_policy": "chapter-spine-href",
        "source_processing": {
            "format": "EPUB",
            "steps": [],
        },
        "source_structure": {
            "method": "epub_spine_toc",
            "confidence": 0.98,
            "outline": outline,
            "units": reading_units,
        },
        "chapters": reading_units,
    })
    return written
