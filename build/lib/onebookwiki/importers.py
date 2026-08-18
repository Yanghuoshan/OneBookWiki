"""Dependency-light EPUB and source metadata import helpers."""
from __future__ import annotations

import hashlib
import html
import importlib
import json
import posixpath
import re
import shutil
import sys
import unicodedata
import zipfile
from dataclasses import replace
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .chunking import count_tokens
from .source_structure import (
    DEFAULT_MAX_UNIT_TOKENS,
    PageBlock,
    PdfLine,
    PdfPage,
    PdfSpan,
    ImportOptions,
    ImportedSourceDocument,
    ImportedSourceNode,
    ImportedSourceUnit,
    SourceLocator,
    SourceNode,
    SourceUnit,
    _kind,
    analyse_pdf,
    imported_source_node_dict,
    split_unit_into_parts,
    validate_imported_source_document,
)
from .pdf_manifest import load_manifest
from .pdf_postprocess import postprocess_pdf_pages
from .pdf_ocr import PdfStructureOcrConfig, LocalPpOcrV5, candidate_pages, overlay_pages


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


def _toc_entries_by_href(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group all TOC entries by bare href, preserving per-fragment metadata."""
    result: dict[str, list[dict[str, Any]]] = {}
    for node in _flatten_outline(nodes):
        href = str(node.get("href", ""))
        if not href:
            continue
        result.setdefault(href, []).append(node)
    return result


def _extract_fragment_segments(html_source: str, fragments: list[str]) -> list[dict[str, Any]]:
    """Split XHTML text content at each TOC fragment anchor point.

    Returns one segment per fragment (plus a leading empty-fragment segment
    for content before the first anchor).  Each segment carries the fragment
    id and its extracted plain text.
    """
    target_ids: set[str] = set(fragments)

    class _Splitter(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.segments: list[dict[str, Any]] = []
            self._current_fragment = ""
            self._pending: list[str] = []
            self._skip = 0
            self._block = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "tr"}
            self._seen: set[str] = set()

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            tag = tag.lower()
            if tag in {"script", "style", "svg", "nav"}:
                self._skip += 1
                return
            if self._skip:
                return
            elem_id = dict(attrs).get("id", "")
            if elem_id in target_ids and elem_id not in self._seen:
                self._flush()
                self._current_fragment = elem_id
                self._seen.add(elem_id)
            if tag in self._block:
                self._pending.append("\n")

        def handle_endtag(self, tag: str) -> None:
            tag = tag.lower()
            if tag in {"script", "style", "svg", "nav"} and self._skip:
                self._skip -= 1
            if not self._skip and tag in self._block:
                self._pending.append("\n")

        def handle_data(self, data: str) -> None:
            if not self._skip:
                self._pending.append(data)

        def _flush(self) -> None:
            if not self._pending:
                return
            text = html.unescape("".join(self._pending))
            lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
            lines = [line for line in lines if line]
            clean = "\n\n".join(lines).strip()
            if clean:
                self.segments.append({"fragment": self._current_fragment, "text": clean})
            self._pending = []

        def finalize(self) -> list[dict[str, Any]]:
            self._flush()
            return self.segments

    splitter = _Splitter()
    splitter.feed(html_source)
    splitter.close()
    return splitter.finalize()


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
        toc_by_href = _toc_entries_by_href(toc_outline)
        entries: list[dict[str, Any]] = []
        for spine_index, itemref in enumerate(_findall(spine, "itemref") if spine is not None else [], 1):
            item_id = itemref.attrib.get("idref")
            item = manifest.get(item_id or "")
            if not item or "html" not in item["media_type"] and "xhtml" not in item["media_type"]:
                continue
            href_value, _, fragment = item["href"].partition("#")
            href = posixpath.normpath(posixpath.join(opf_dir, href_value))
            toc_for_href = toc_by_href.get(href, [])
            # Collect distinct non-empty fragments from TOC entries pointing to
            # this XHTML file.  When there are multiple distinct fragments the
            # file carries several sub-chapters — split it at each anchor.
            distinct_fragments: list[str] = list(dict.fromkeys(
                e.get("fragment") for e in toc_for_href if e.get("fragment")
            ))
            if len(distinct_fragments) > 1:
                raw_html = _read_text(archive, href)
                segments = _extract_fragment_segments(raw_html, distinct_fragments)
                for seg in segments:
                    seg_frag = str(seg.get("fragment", ""))
                    seg_text = str(seg.get("text", ""))
                    if not seg_text.strip():
                        continue
                    matching_toc = next((e for e in toc_for_href if e.get("fragment") == seg_frag), None)
                    if matching_toc:
                        seg_title = str(matching_toc.get("title", ""))
                        seg_breadcrumb = list(matching_toc.get("breadcrumb", [seg_title]))
                    elif not seg_frag:
                        parent_toc = next((e for e in toc_for_href if not e.get("fragment")), None)
                        seg_title = str(parent_toc.get("title")) if parent_toc else ""
                        seg_breadcrumb = list(parent_toc.get("breadcrumb", [seg_title])) if parent_toc else [seg_title]
                    else:
                        seg_title = ""
                        seg_breadcrumb = [seg_title]
                    if not seg_title:
                        body_lines = [line.strip() for line in seg_text.splitlines() if line.strip()]
                        seg_title = body_lines[0] if body_lines else f"Reading unit {spine_index}"
                        seg_breadcrumb = [seg_title]
                    entry_num = len(entries) + 1
                    entries.append({
                        "number": entry_num,
                        "title": seg_title,
                        "href": href,
                        "fragment": seg_frag,
                        "spine": item_id or "",
                        "spine_index": spine_index,
                        "breadcrumb": seg_breadcrumb,
                        "text": seg_text,
                    })
            else:
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
        import pymupdf as fitz
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
    pdf_ocr: str = "off",
    pdf_ocr_dpi: int | None = None,
    pdf_ocr_confidence: float | None = None,
) -> list[Path]:
    """Import a PDF as bounded source units without OCR or semantic cleanup.

    Structure analysis intentionally uses the native page view.  The selected
    processed page view is used only after physical page ranges are fixed.
    """
    if structure not in {"auto", "bookmarks", "toc", "headings", "ranges"}:
        raise ValueError(f"unsupported PDF structure mode: {structure}")
    if postprocess not in {"off", "auto", "strict"}:
        raise ValueError(f"unsupported PDF postprocess mode: {postprocess}")
    if pdf_ocr not in {"off", "assist"}:
        raise ValueError(f"unsupported PDF OCR mode: {pdf_ocr}")
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
    baseline_method = analysis.method
    baseline_confidence = analysis.confidence
    def report_ocr(message: str) -> None:
        print(f"[pdf-ocr] {message}", file=sys.stderr, flush=True)

    ocr_report: dict[str, Any] = {
        "mode": pdf_ocr,
        "role": "structure_auxiliary_only",
        "local_only": True,
        "status": "disabled" if pdf_ocr == "off" else "not_used",
        "trigger": "disabled" if pdf_ocr == "off" else "not_required",
        "candidate_pages": [],
        "processed_pages": [],
        "failed_pages": {},
        "evidence_pages": [],
        "selected": False,
    }
    if pdf_ocr == "assist":
        if pages is not None:
            ocr_report.update({"status": "skipped", "trigger": "synthetic_pages", "reason": "OCR requires a PDF-backed native page layout"})
            report_ocr("skipped: synthetic --pages input has no PDF rendering source")
        elif manifest and manifest.get("units"):
            ocr_report.update({"status": "skipped", "trigger": "explicit_manifest_units", "reason": "validated manifest units already define the page partition"})
            report_ocr("skipped: validated manifest units already define the page partition")
        elif analysis.method == "bookmarks" and analysis.confidence >= max(structure_min_confidence, 0.85):
            ocr_report.update({"status": "skipped", "trigger": "strong_bookmarks", "reason": "complete bookmark structure exceeds the confidence threshold"})
            report_ocr(f"skipped: complete bookmark structure (confidence={analysis.confidence:.2f})")
        else:
            config = PdfStructureOcrConfig.from_env(dpi=pdf_ocr_dpi, confidence=pdf_ocr_confidence)
            pages_to_scan = candidate_pages(native_pages, analysis.report, config.max_pages)
            trigger = "low_confidence" if analysis.confidence < max(structure_min_confidence, 0.82) else "ambiguous_structure"
            ocr_report.update({"config": config.report(), "trigger": trigger, "candidate_pages": pages_to_scan})
            report_ocr(
                f"triggered: {trigger}; baseline={analysis.method} ({analysis.confidence:.2f}); "
                f"candidate pages={','.join(str(page) for page in pages_to_scan) or 'none'}"
            )
            if pages_to_scan:
                try:
                    adapter = LocalPpOcrV5(config)
                    results = adapter.run(source, pages_to_scan)
                    ocr_report["processed_pages"] = sorted(results)
                    ocr_report["failed_pages"] = {str(page): error for page, error in sorted(adapter.failures.items())}
                    report_ocr(
                        f"processed={len(results)}/{len(pages_to_scan)} pages; "
                        f"failed={len(adapter.failures)}; local models only"
                    )
                    assisted_pages = overlay_pages(native_pages, results, minimum_confidence=config.confidence)
                    evidence_pages = [
                        page.number for page, assisted_page in zip(native_pages, assisted_pages)
                        if assisted_page is not page
                    ]
                    ocr_report["evidence_pages"] = evidence_pages
                    if evidence_pages:
                        assisted = analyse_pdf(
                            assisted_pages,
                            source,
                            explicit_title=title,
                            bookmarks=[],
                            mode=structure,
                            min_confidence=structure_min_confidence,
                            pages_per_chapter=pages_per_chapter,
                            chapter_count=chapter_count,
                            structure_manifest=None,
                        )
                        baseline_rank = (
                            2 if analysis.method == "toc" else 1 if analysis.method == "headings" else 0,
                            analysis.confidence,
                            len(analysis.units),
                        )
                        assisted_rank = (
                            2 if assisted.method == "toc" else 1 if assisted.method == "headings" else 0,
                            assisted.confidence,
                            len(assisted.units),
                        )
                        if assisted_rank > baseline_rank and assisted.method in {"toc", "headings"}:
                            analysis = assisted
                            ocr_report.update({
                                "status": "used",
                                "selected": True,
                                "adoption": {
                                    "baseline_method": baseline_method,
                                    "baseline_confidence": round(baseline_confidence, 4),
                                    "selected_method": assisted.method,
                                    "selected_confidence": round(assisted.confidence, 4),
                                },
                            })
                            report_ocr(f"adopted: {assisted.method} ({assisted.confidence:.2f}) improves native structure")
                        else:
                            ocr_report["status"] = "evaluated_not_selected"
                            report_ocr(f"not adopted: {assisted.method} ({assisted.confidence:.2f}) does not improve native structure")
                    else:
                        ocr_report["status"] = "no_usable_evidence"
                        report_ocr("not adopted: no new OCR lines passed confidence and deduplication")
                except ValueError as error:
                    ocr_report.update({"status": "unavailable", "error": str(error)})
                    report_ocr(f"unavailable: {error}")
    analysis.report["ocr"] = "disabled" if pdf_ocr == "off" else "pp-ocrv5-mobile-assist"
    analysis.report["ocr_assist"] = ocr_report
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

    parts: list[tuple[SourceUnit, Any]] = []
    for unit in analysis.units:
        parts.extend((unit, part) for part in split_unit_into_parts(unit, processed_pages, max_unit_tokens))
    part_ids: dict[str, list[str]] = {}
    neutral_units: list[ImportedSourceUnit] = []
    for original, part in parts:
        part_id = original.id if part.part_count == 1 else f"{original.id}-part-{part.part:02d}"
        part_ids.setdefault(original.id, []).append(part_id)
        grouped: dict[int, list[str]] = {}
        for page_number, fragment in part.page_fragments:
            grouped.setdefault(page_number, []).append(fragment)
        page_blocks = [
            f"## Page {page_number}\n\n{page_text}"
            for page_number, fragments in grouped.items()
            if (page_text := "\n\n".join(fragments).strip())
        ]
        locator = SourceLocator("PDF", "pdf_pages", {
            "physical_page_start": part.physical_page_start,
            "physical_page_end": part.physical_page_end,
        })
        neutral_units.append(ImportedSourceUnit(
            id=part_id,
            title=part.title,
            text="\n\n".join(page_blocks).strip(),
            locator=locator,
            kind=part.kind,
            breadcrumb=part.breadcrumb,
            confidence=part.confidence,
            source_title=original.title,
            part=part.part,
            part_count=part.part_count,
        ))

    def neutral_node(node: SourceNode) -> ImportedSourceNode:
        return ImportedSourceNode(
            id=node.id,
            title=node.title,
            kind=node.kind,
            breadcrumb=node.breadcrumb,
            confidence=node.confidence,
            unit_ids=[part_id for unit_id in node.unit_ids for part_id in part_ids.get(unit_id, [])],
            children=[neutral_node(child) for child in node.children],
        )

    document = ImportedSourceDocument(
        format="PDF",
        source_name=source.name,
        source_hash=source_hash,
        title=book_title,
        title_method=analysis.title.method,
        title_confidence=analysis.title.confidence,
        units=neutral_units,
        outline=[neutral_node(node) for node in analysis.outline],
        metadata={
            "structure_method": analysis.method,
            "structure_confidence": analysis.confidence,
            "_source_metadata_extra": {
                "page_count": len(native_pages),
                "pages_per_chapter": pages_per_chapter,
                "chapter_count": chapter_count,
            },
            "_source_structure_extra": {
                "report_path": report_path.relative_to(root).as_posix() if report_path.is_relative_to(root) else str(report_path),
                **({"structure_manifest": manifest_metadata} if manifest_metadata is not None else {}),
            },
        },
        processing={
            "format": "PDF",
            "structure_view": "native+ocr-assist" if ocr_report.get("selected") else "native",
            "structure_ocr": ocr_report,
            "payload_view": processed.report["payload_view"],
            "steps": [processed.report],
        },
        locator_policy="pdf-physical-page-1-based",
    )
    return write_import(document, root, ImportOptions(
        title=title,
        force=force,
        max_unit_tokens=max_unit_tokens,
    ))



# ---------------------------------------------------------------------------
# Format-neutral import contract
# ---------------------------------------------------------------------------

SUPPORTED_SOURCE_SUFFIXES = {
    ".pdf": "PDF",
    ".epub": "EPUB",
    ".mobi": "MOBI",
    ".azw": "AZW",
    ".azw3": "AZW3",
    ".txt": "TXT",
    ".doc": "DOC",
    ".docx": "DOCX",
    ".html": "HTML",
    ".htm": "HTML",
}


def detect_source_type(source: Path, override: str | None = None) -> str:
    """Return a supported input format without transforming its contents."""
    if override:
        value = override.strip().upper()
        if value in set(SUPPORTED_SOURCE_SUFFIXES.values()):
            return value
        raise ValueError(f"unsupported source type override: {override!r}")
    try:
        return SUPPORTED_SOURCE_SUFFIXES[source.suffix.lower()]
    except KeyError as exc:
        suffixes = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
        raise ValueError(f"unsupported source format {source.suffix or '(no suffix)'}; supported suffixes: {suffixes}") from exc


def _stable_source_id(prefix: str, identity: str) -> str:
    return f"{prefix.lower()}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def _clean_source_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def _split_text(value: str, budget: int) -> list[str]:
    """Split extracted prose at natural boundaries without dropping characters."""
    if count_tokens(value) <= budget:
        return [value]
    pieces = [part.strip() for part in re.split(r"\n\s*\n", value) if part.strip()]
    if len(pieces) <= 1:
        pieces = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s*", value) if part.strip()]
    result: list[str] = []
    pending = ""
    for piece in pieces:
        proposal = f"{pending}\n\n{piece}".strip() if pending else piece
        if pending and count_tokens(proposal) > budget:
            result.append(pending)
            pending = piece
        else:
            pending = proposal
        while pending and count_tokens(pending) > budget:
            low, high, best = 1, len(pending), 1
            while low <= high:
                middle = (low + high) // 2
                if count_tokens(pending[:middle]) <= budget:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            result.append(pending[:best].strip())
            pending = pending[best:].strip()
    if pending:
        result.append(pending)
    return result


def _filename_title(source: Path) -> str:
    return " ".join(source.stem.replace("_", " ").replace("-", " ").split()) or "Untitled book"


def _source_outline(units: list[ImportedSourceUnit]) -> list[ImportedSourceNode]:
    return [
        ImportedSourceNode(
            id=f"outline-{unit.id}", title=unit.title, kind=unit.kind,
            breadcrumb=unit.breadcrumb or (unit.title,), confidence=unit.confidence,
            unit_ids=[unit.id],
        )
        for unit in units
    ]


def _split_imported_units(
    document: ImportedSourceDocument,
    max_unit_tokens: int,
) -> ImportedSourceDocument:
    """Bound non-PDF reading units without losing text or source provenance."""
    if max_unit_tokens < 256:
        raise ValueError("max_unit_tokens must be at least 256")
    if document.format.upper() == "PDF":
        return document
    budget = max(128, int(max_unit_tokens * 0.70))
    split: list[ImportedSourceUnit] = []
    part_ids_by_unit: dict[str, list[str]] = {}
    changed = False
    for unit in document.units:
        if count_tokens(unit.text) <= budget:
            split.append(unit)
            part_ids_by_unit[unit.id] = [unit.id]
            continue
        changed = True
        parts = _split_text(unit.text, budget)
        part_count = len(parts)
        part_ids: list[str] = []
        for index, text in enumerate(parts, 1):
            title = unit.title if part_count == 1 else f"{unit.title} (Part {index})"
            identity = json.dumps(
                {"unit": unit.id, "part": index, "locator": unit.locator.to_dict()},
                ensure_ascii=False,
                sort_keys=True,
            )
            part_id = _stable_source_id(document.format, identity)
            part_ids.append(part_id)
            split.append(replace(
                unit,
                id=part_id,
                title=title,
                text=text,
                part=index,
                part_count=part_count,
                metadata={**unit.metadata, "split_from": unit.id},
            ))
        part_ids_by_unit[unit.id] = part_ids
    if not changed:
        return document

    def rewrite_node(node: ImportedSourceNode) -> ImportedSourceNode:
        return replace(
            node,
            unit_ids=[
                part_id
                for unit_id in node.unit_ids
                for part_id in part_ids_by_unit.get(unit_id, [unit_id])
            ],
            children=[rewrite_node(child) for child in node.children],
        )

    return replace(document, units=split, outline=[rewrite_node(node) for node in document.outline])


def _legacy_locator_fields(locator: dict[str, Any]) -> dict[str, Any]:
    source_format = str(locator.get("format", "")).upper()
    if source_format == "PDF":
        return {key: locator[key] for key in ("physical_page_start", "physical_page_end") if locator.get(key) is not None}
    if source_format == "EPUB":
        mapping = {"spine_id": "spine", "spine_index": "spine_index", "href": "href", "fragment": "fragment"}
        return {target: locator[source] for source, target in mapping.items() if locator.get(source) not in (None, "")}
    return {}


def _raw_locator_lines(locator: dict[str, Any]) -> list[str]:
    source_format = str(locator.get("format", "")).upper()
    result = [f"> Locator: {json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"]
    if source_format == "PDF" and locator.get("physical_page_start") is not None:
        result.append(f"> Pages: {locator['physical_page_start']}-{locator.get('physical_page_end', locator['physical_page_start'])}")
    elif source_format == "EPUB":
        for label, key in (("Spine", "spine_id"), ("Spine Index", "spine_index"), ("Href", "href"), ("Fragment", "fragment")):
            if locator.get(key) not in (None, ""):
                result.append(f"> {label}: {locator[key]}")
    elif source_format == "TXT" and locator.get("line_start") is not None:
        result.append(f"> Lines: {locator['line_start']}-{locator.get('line_end', locator['line_start'])}")
    elif source_format in {"DOC", "DOCX"} and locator.get("paragraph_start") is not None:
        result.append(f"> Paragraphs: {locator['paragraph_start']}-{locator.get('paragraph_end', locator['paragraph_start'])}")
    elif source_format == "HTML":
        for label, key in (("Href", "href"), ("Fragment", "fragment")):
            if locator.get(key):
                result.append(f"> {label}: {locator[key]}")
    return result


def write_import(document: ImportedSourceDocument, root: Path, options: ImportOptions | None = None) -> list[Path]:
    """Materialize a format-neutral source document using the schema-v3 contract."""
    options = options or ImportOptions()
    document = _split_imported_units(document, options.max_unit_tokens)
    validate_imported_source_document(document)
    if options.force:
        _clear_raw_chapters(root)
    output = root / "raw" / "chapters"
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    records: list[dict[str, Any]] = []
    page_ids_by_unit: dict[str, list[str]] = {}
    for chapter, unit in enumerate(document.units, 1):
        target = output / f"{chapter:02d}-{slugify(unit.title, f'chapter-{chapter}')}.md"
        raw_path = target.relative_to(root).as_posix()
        locator = unit.locator.to_dict()
        record = {
            "chapter": chapter,
            "source_unit_id": unit.id,
            "title": unit.title,
            "source_title": unit.source_title or unit.title,
            "kind": unit.kind,
            "breadcrumb": list(unit.breadcrumb or (unit.title,)),
            "confidence": unit.confidence,
            "part": unit.part,
            "part_count": unit.part_count,
            "raw_path": raw_path,
            "locator": locator,
            **_legacy_locator_fields(locator),
        }
        if not target.exists() or options.force:
            header = [
                f"# {unit.title}", "", f"> Book: {document.title}",
                f"> Chapter: {chapter}", f"> Source Unit: {unit.id}",
                f"> Breadcrumb: {' › '.join(record['breadcrumb'])}", f"> Type: {unit.kind}",
                f"> Confidence: {unit.confidence:.2f}", f"> Part: {unit.part}/{unit.part_count}",
                f"> Source: {document.source_name}", f"> Format: {document.format.upper()}",
                *_raw_locator_lines(locator), f"> Collected: {date.today().isoformat()}",
            ]
            if document.author:
                header.insert(3, f"> Author: {document.author}")
            target.write_text("\n".join(header) + f"\n\n{unit.text}\n", encoding="utf-8")
        written.append(target)
        records.append(record)
        page_ids_by_unit.setdefault(unit.id, []).append(f"chapter-{chapter:02d}")

    def serialize_node(node: ImportedSourceNode) -> dict[str, Any]:
        value = imported_source_node_dict(node)
        value["pageIds"] = [page_id for unit_id in value["unitIds"] for page_id in page_ids_by_unit.get(unit_id, [])]
        value["children"] = [serialize_node(child) for child in node.children]
        return value

    structure_extra = document.metadata.get("_source_structure_extra", {})
    if not isinstance(structure_extra, dict):
        raise ValueError("source structure extras must be an object")
    source_structure = {
        **structure_extra,
        "method": str(document.metadata.get("structure_method", "adapter")),
        "confidence": float(document.metadata.get("structure_confidence", 0.0) or 0.0),
        "outline": [serialize_node(node) for node in document.outline],
        "units": records,
    }
    metadata_extra = document.metadata.get("_source_metadata_extra", {})
    if not isinstance(metadata_extra, dict):
        raise ValueError("source metadata extras must be an object")
    source_metadata: dict[str, Any] = {
        **metadata_extra,
        "schema_version": 3,
        "title": document.title,
        "title_method": document.title_method,
        "title_confidence": document.title_confidence,
        "format": document.format.upper(),
        "source_name": document.source_name,
        "source_hash": document.source_hash,
        "locator_policy": document.locator_policy,
        "source_processing": document.processing,
        "source_structure": source_structure,
        "chapters": records,
    }
    if document.author:
        source_metadata["author"] = document.author
    if document.metadata:
        source_metadata["metadata"] = document.metadata
    if document.warnings:
        source_metadata["warnings"] = document.warnings
    write_source_metadata(root, source_metadata)
    return written


class _HtmlDocumentExtractor(HTMLParser):
    """Deterministic body and heading extractor for standalone HTML inputs."""

    ignored_tags = {"script", "style", "svg", "nav", "noscript", "template"}
    content_tags = {"p", "li", "blockquote", "pre", "td", "th", "figcaption"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.blocks: list[dict[str, Any]] = []
        self.headings: list[dict[str, Any]] = []
        self.meta: dict[str, str] = {}
        self._title_parts: list[str] = []
        self._in_title = False
        self._current: dict[str, Any] | None = None
        self._block_number = 0
        self._pending_anchor = ""
        self._active_links: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if tag in self.ignored_tags:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").lower()
            if key and values.get("content") and key not in self.meta:
                self.meta[key] = values["content"].strip()
            return
        if tag == "a":
            anchor = (values.get("id") or values.get("name") or "").strip()
            if anchor:
                self._pending_anchor = anchor
            href = values.get("href", "").strip()
            if href:
                self._active_links.append({
                    "target": href,
                    "text": [],
                    "block_start": self._current["start"] if self._current else self._block_number + 1,
                })
            return
        heading_level = int(tag[1]) if len(tag) == 2 and tag.startswith("h") and tag[1].isdigit() else None
        if heading_level is not None or tag in self.content_tags:
            self._finish_current()
            self._block_number += 1
            self._current = {
                "tag": tag,
                "text": [],
                "start": self._block_number,
                "end": self._block_number,
                "heading_level": heading_level,
                "anchor": values.get("id") or values.get("name") or self._pending_anchor,
            }

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.ignored_tags and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a" and self._active_links:
            link = self._active_links.pop()
            title = _clean_source_text("".join(link["text"]))
            if title:
                self.links.append({**link, "title": title})
            return
        if self._current and tag == self._current["tag"]:
            self._finish_current()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._current is not None:
            self._current["text"].append(data)
        for link in self._active_links:
            link["text"].append(data)

    def close(self) -> None:
        super().close()
        self._finish_current()

    def _finish_current(self) -> None:
        if self._current is None:
            return
        text = _clean_source_text("".join(self._current.pop("text")))
        if text:
            self._current["text"] = text
            if self._current.get("anchor") == self._pending_anchor:
                self._pending_anchor = ""
            if self._current.get("heading_level") is not None:
                self._current["heading_title"] = text
                self.headings.append({
                    "title": text,
                    "level": self._current["heading_level"],
                    "start": self._current["start"],
                    "confidence": 0.98,
                })
            self.blocks.append(self._current)
        self._current = None

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())


def _parse_html_document(source: Path) -> tuple[_HtmlDocumentExtractor, str]:
    raw = source.read_bytes()
    try:
        content, encoding = raw.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        content, encoding = raw.decode("utf-8", errors="replace"), "utf-8-replace"
    parser = _HtmlDocumentExtractor()
    parser.feed(content)
    parser.close()
    return parser, encoding


_KINDLE_FILEPOS_TARGET_RE = re.compile(r"^#(filepos\d+)$", re.IGNORECASE)
_KINDLE_FOOTNOTE_LINK_RE = re.compile(r"^[（(]\s*\d+\s*[）)]$")


def _kindle_toc_entries(parser: _HtmlDocumentExtractor) -> list[dict[str, Any]]:
    """Find the first substantial in-document Kindle table of contents."""
    block_index_by_anchor = {
        str(block.get("anchor", "")).lower(): index
        for index, block in enumerate(parser.blocks)
        if block.get("anchor")
    }
    candidates: list[dict[str, Any]] = []
    for link in parser.links:
        match = _KINDLE_FILEPOS_TARGET_RE.match(str(link.get("target", "")))
        title = " ".join(str(link.get("title", "")).split())
        if not match or not title or len(title) > 160 or _KINDLE_FOOTNOTE_LINK_RE.fullmatch(title):
            continue
        target = match.group(1).lower()
        target_index = block_index_by_anchor.get(target)
        if target_index is None:
            continue
        candidates.append({
            "title": title,
            "target": target,
            "target_index": target_index,
            "block_start": int(link.get("block_start", 0) or 0),
        })
    if not candidates:
        return []

    groups: list[list[dict[str, Any]]] = []
    for candidate in candidates:
        if groups and candidate["block_start"] <= groups[-1][-1]["block_start"] + 2:
            groups[-1].append(candidate)
        else:
            groups.append([candidate])
    valid_groups = [
        group for group in groups
        if len({entry["target"] for entry in group}) >= 2
        and all(left["target_index"] < right["target_index"] for left, right in zip(group, group[1:]))
    ]
    if not valid_groups:
        return []
    selected = max(
        valid_groups,
        key=lambda group: (len({entry["target"] for entry in group}), -group[0]["block_start"]),
    )
    result: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for entry in selected:
        if entry["target"] not in seen_targets:
            result.append(entry)
            seen_targets.add(entry["target"])
    return result


def _kindle_toc_document(
    source: Path,
    options: ImportOptions,
    parser: _HtmlDocumentExtractor,
    encoding: str,
) -> ImportedSourceDocument | None:
    entries = _kindle_toc_entries(parser)
    if not entries:
        return None
    first_content_index = entries[0]["target_index"]
    front_matter_title = next(
        (
            str(block["text"])
            for block in parser.blocks[:first_content_index]
            if 1 <= len(str(block["text"])) <= 160
        ),
        "",
    )
    explicit = " ".join((options.title or "").split())
    inferred = parser.title or parser.meta.get("og:title") or front_matter_title or _filename_title(source)
    method = "explicit" if explicit else "html_title" if parser.title or parser.meta.get("og:title") else "kindle_front_matter" if front_matter_title else "filename"
    confidence = 1.0 if explicit else 0.98 if method == "html_title" else 0.88 if method == "kindle_front_matter" else 0.35
    title = explicit or inferred
    units: list[ImportedSourceUnit] = []
    outline: list[ImportedSourceNode] = []
    for ordinal, entry in enumerate(entries, 1):
        start = int(entry["target_index"])
        end = int(entries[ordinal]["target_index"]) if ordinal < len(entries) else len(parser.blocks)
        selected = parser.blocks[start:end]
        if not selected:
            continue
        first, last = selected[0], selected[-1]
        unit_title = str(entry["title"])
        locator = SourceLocator("HTML", "html_anchor", {
            "href": source.name,
            "fragment": str(entry["target"]),
            "block_start": first["start"],
            "block_end": last["end"],
            "element": first.get("tag", ""),
        })
        identity = json.dumps(locator.to_dict(), ensure_ascii=False, sort_keys=True)
        unit_id = _stable_source_id("HTML", identity)
        units.append(ImportedSourceUnit(
            id=unit_id,
            title=unit_title,
            text="\n\n".join(str(block["text"]) for block in selected),
            locator=locator,
            kind=_kind(unit_title),
            breadcrumb=(unit_title,),
            confidence=0.98,
            source_title=unit_title,
            metadata={"start": first["start"], "end": last["end"], "kindle_toc_target": entry["target"]},
        ))
        outline.append(ImportedSourceNode(
            id=f"outline-kindle-toc-{ordinal:03d}",
            title=unit_title,
            kind=_kind(unit_title),
            breadcrumb=(unit_title,),
            confidence=0.98,
            unit_ids=[unit_id],
        ))
    if not units:
        return None
    return ImportedSourceDocument(
        format="HTML",
        source_name=source.name,
        source_hash=sha256_file(source),
        title=title,
        title_method=method,
        title_confidence=confidence,
        author=parser.meta.get("author", ""),
        units=units,
        outline=outline,
        metadata={
            "html_meta": parser.meta,
            "encoding": encoding,
            "structure_method": "kindle_toc_anchors",
            "structure_confidence": 0.98,
            "kindle_toc_entries": len(entries),
        },
        processing={"format": "HTML", "steps": ["stdlib-html-parser", "kindle-filepos-toc"]},
    )


def _outline_from_authored_headings(
    headings: list[dict[str, Any]],
    units: list[ImportedSourceUnit],
) -> list[ImportedSourceNode]:
    """Retain every authored heading, including levels that do not split prose."""
    if not headings:
        return _source_outline(units)
    unit_by_heading_start = {
        int(unit.metadata["heading_start"]): unit
        for unit in units
        if unit.metadata.get("heading_start") is not None
    }
    roots: list[ImportedSourceNode] = []
    stack: list[tuple[int, ImportedSourceNode]] = []
    seen_units: set[str] = set()
    for index, heading in enumerate(headings, 1):
        heading_title = " ".join(str(heading.get("title", "")).split())
        if not heading_title:
            continue
        level = max(1, min(int(heading.get("level", 1) or 1), 9))
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else None
        breadcrumb = (*parent.breadcrumb, heading_title) if parent else (heading_title,)
        unit = unit_by_heading_start.get(int(heading.get("start", 0) or 0))
        unit_ids = [unit.id] if unit is not None else []
        if unit is not None:
            unit.breadcrumb = breadcrumb
            seen_units.add(unit.id)
        node = ImportedSourceNode(
            id=f"outline-heading-{index:03d}",
            title=heading_title,
            kind=_kind(heading_title),
            breadcrumb=breadcrumb,
            confidence=float(heading.get("confidence", 0.98) or 0.98),
            unit_ids=unit_ids,
        )
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
        stack.append((level, node))
    for unit in units:
        if unit.id not in seen_units:
            roots.append(ImportedSourceNode(
                id=f"outline-{unit.id}",
                title=unit.title,
                kind=unit.kind,
                breadcrumb=unit.breadcrumb or (unit.title,),
                confidence=unit.confidence,
                unit_ids=[unit.id],
            ))
    return roots


def _document_from_blocks(
    source: Path,
    source_format: str,
    title: str,
    title_method: str,
    title_confidence: float,
    blocks: list[dict[str, Any]],
    headings: list[dict[str, Any]],
    locator_factory: Any,
    *,
    author: str = "",
    metadata: dict[str, Any] | None = None,
    processing: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> ImportedSourceDocument:
    normalized = [{**block, "text": _clean_source_text(str(block.get("text", "")))} for block in blocks]
    normalized = [block for block in normalized if block["text"]]
    if not normalized:
        raise ValueError(f"{source_format} source contains no readable text")
    starts = [
        index for index, block in enumerate(normalized)
        if block.get("heading_level") is not None and int(block["heading_level"]) <= 2
    ]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    units: list[ImportedSourceUnit] = []
    for ordinal, start in enumerate(starts, 1):
        end = starts[ordinal] if ordinal < len(starts) else len(normalized)
        selected = normalized[start:end]
        first, last = selected[0], selected[-1]
        heading_level = first.get("heading_level")
        unit_title = " ".join(str(first.get("heading_title") or "").split()) or (title if ordinal == 1 else f"Reading unit {ordinal}")
        locator = locator_factory(first, last)
        identity = json.dumps(locator.to_dict(), ensure_ascii=False, sort_keys=True)
        unit_id = _stable_source_id(source_format, identity)
        unit_metadata = {"start": first["start"], "end": last["end"]}
        if heading_level is not None:
            unit_metadata.update(heading_start=first["start"], heading_level=int(heading_level))
        units.append(ImportedSourceUnit(
            id=unit_id,
            title=unit_title,
            text="\n\n".join(str(block["text"]) for block in selected),
            locator=locator,
            kind=_kind(unit_title),
            breadcrumb=(unit_title,),
            confidence=0.98 if heading_level is not None else 0.55,
            source_title=unit_title,
            metadata=unit_metadata,
        ))
    return ImportedSourceDocument(
        format=source_format,
        source_name=source.name,
        source_hash=sha256_file(source),
        title=title,
        title_method=title_method,
        title_confidence=title_confidence,
        author=author,
        units=units,
        outline=_outline_from_authored_headings(headings, units),
        metadata=dict(metadata or {}),
        processing=dict(processing or {"format": source_format, "steps": []}),
        warnings=list(warnings or []),
    )


def _analyze_html_document(
    source: Path,
    options: ImportOptions,
    parser: _HtmlDocumentExtractor,
    encoding: str,
) -> ImportedSourceDocument:
    inferred = parser.title or parser.meta.get("og:title") or (parser.headings[0]["title"] if parser.headings else "") or _filename_title(source)
    explicit = " ".join((options.title or "").split())
    title = explicit or inferred
    method = "explicit" if explicit else "html_title" if parser.title or parser.meta.get("og:title") else "html_heading" if parser.headings else "filename"
    confidence = 1.0 if explicit else 0.98 if method == "html_title" else 0.88 if method == "html_heading" else 0.35

    def locator(first: dict[str, Any], last: dict[str, Any]) -> SourceLocator:
        values: dict[str, Any] = {"href": source.name, "block_start": first["start"], "block_end": last["end"], "element": first.get("tag", "")}
        if first.get("anchor"):
            values["fragment"] = first["anchor"]
        return SourceLocator("HTML", "html_anchor" if values.get("fragment") else "html_block", values)

    return _document_from_blocks(
        source, "HTML", title, method, confidence, parser.blocks, parser.headings, locator,
        author=parser.meta.get("author", ""),
        metadata={"html_meta": parser.meta, "encoding": encoding, "structure_method": "html_headings" if parser.headings else "html_blocks", "structure_confidence": 0.98 if parser.headings else 0.55},
        processing={"format": "HTML", "steps": ["stdlib-html-parser"]},
        warnings=[] if parser.headings else ["HTML had no authored headings; imported as a conservative reading unit."],
    )


def analyze_html(source: Path, options: ImportOptions | None = None) -> ImportedSourceDocument:
    options = options or ImportOptions()
    parser, encoding = _parse_html_document(source)
    return _analyze_html_document(source, options, parser, encoding)


def _analyze_kindle_html(source: Path, options: ImportOptions) -> ImportedSourceDocument:
    parser, encoding = _parse_html_document(source)
    return _kindle_toc_document(source, options, parser, encoding) or _analyze_html_document(source, options, parser, encoding)


def _decode_text_source(source: Path) -> tuple[str, str]:
    data = source.read_bytes()
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    try:
        match = importlib.import_module("charset_normalizer").from_bytes(data).best()
        if match is not None:
            return str(match), str(match.encoding or "charset-normalizer")
    except ImportError:
        pass
    for encoding in ("gb18030", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


_TEXT_HEADING_RE = re.compile(r"^(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)|[一二三四五六七八九十百千万]+[、.]|\d+(?:\.\d+)*[.、]?|chapter\s+\d+|part\s+\d+|section\s+\d+(?:\.\d+)*)\s*.*$", re.IGNORECASE)


def _plain_heading(line: str, next_line: str) -> tuple[str, int] | None:
    markdown = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.strip())
    if markdown:
        return markdown.group(2), len(markdown.group(1))
    if line.strip() and re.fullmatch(r"[=-]{3,}\s*", next_line):
        return line.strip(), 1 if next_line.lstrip().startswith("=") else 2
    if _TEXT_HEADING_RE.match(line.strip()):
        return line.strip(), 2
    return None


def analyze_text(source: Path, options: ImportOptions | None = None) -> ImportedSourceDocument:
    options = options or ImportOptions()
    content, encoding = _decode_text_source(source)
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    pending: list[str] = []
    start = 1
    skip_until = 0

    def flush(end: int) -> None:
        nonlocal pending
        text = "\n".join(pending).strip()
        if text:
            blocks.append({"text": text, "start": start, "end": end})
        pending = []

    for number, line in enumerate(lines, 1):
        if number <= skip_until:
            continue
        next_line = lines[number] if number < len(lines) else ""
        heading = _plain_heading(line, next_line)
        if heading:
            flush(number - 1)
            heading_title, level = heading
            end = number + 1 if line.strip() and re.fullmatch(r"[=-]{3,}\s*", next_line) else number
            skip_until = end
            blocks.append({"text": heading_title, "start": number, "end": end, "heading_level": level, "heading_title": heading_title})
            headings.append({"title": heading_title, "level": level, "start": number})
            start = end + 1
        elif not line.strip():
            flush(number - 1)
            start = number + 1
        else:
            if not pending:
                start = number
            pending.append(line)
    flush(len(lines))
    explicit = " ".join((options.title or "").split())
    title = explicit or _filename_title(source)

    def locator(first: dict[str, Any], last: dict[str, Any]) -> SourceLocator:
        return SourceLocator("TXT", "text_lines", {"line_start": first["start"], "line_end": last["end"], "encoding": encoding})

    return _document_from_blocks(
        source, "TXT", title, "explicit" if explicit else "filename", 1.0 if explicit else 0.35,
        blocks, headings, locator,
        metadata={"encoding": encoding, "structure_method": "text_headings" if headings else "text_blocks", "structure_confidence": 0.86 if headings else 0.45},
        processing={"format": "TXT", "steps": ["decode", "conservative-heading-inference"]},
        warnings=[] if headings else ["TXT had no recognized headings; imported as a conservative reading unit."],
    )


def _require_optional(module: str, extra: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ValueError(f"{module} support is optional; install it with: pip install -e '.[{extra}]'") from exc


def analyze_docx(source: Path, options: ImportOptions | None = None) -> ImportedSourceDocument:
    options = options or ImportOptions()
    docx = _require_optional("docx", "docx")
    paragraph_type = importlib.import_module("docx.text.paragraph").Paragraph
    table_type = importlib.import_module("docx.table").Table
    document = docx.Document(source)
    blocks: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    ordinal = 0
    for child in document.element.body.iterchildren():
        tag = _local_name(child.tag)
        if tag not in {"p", "tbl"}:
            continue
        ordinal += 1
        if tag == "p":
            paragraph = paragraph_type(child, document)
            text = _clean_source_text(paragraph.text)
            if not text:
                continue
            style = str(getattr(paragraph.style, "name", "") or "")
            match = re.match(r"Heading\s+([1-9])\b", style, re.IGNORECASE)
            level = int(match.group(1)) if match else None
            block = {"text": text, "start": ordinal, "end": ordinal}
            if level:
                block.update(heading_level=level, heading_title=text)
                headings.append({"title": text, "level": level, "start": ordinal})
            blocks.append(block)
        else:
            table = table_type(child, document)
            rows = [" | ".join(" ".join(cell.text.split()) for cell in row.cells) for row in table.rows]
            rows = [row for row in rows if row.strip(" |")]
            if rows:
                blocks.append({"text": "\n".join(rows), "start": ordinal, "end": ordinal})
    properties = document.core_properties
    document_title = " ".join(str(properties.title or "").split())
    author = " ".join(str(properties.author or "").split())
    explicit = " ".join((options.title or "").split())
    title = explicit or document_title or (headings[0]["title"] if headings else "") or _filename_title(source)
    method = "explicit" if explicit else "docx_core_properties" if document_title else "docx_heading" if headings else "filename"
    confidence = 1.0 if explicit else 0.98 if document_title else 0.88 if headings else 0.35

    def locator(first: dict[str, Any], last: dict[str, Any]) -> SourceLocator:
        return SourceLocator("DOCX", "docx_paragraphs", {"paragraph_start": first["start"], "paragraph_end": last["end"]})

    return _document_from_blocks(
        source, "DOCX", title, method, confidence, blocks, headings, locator,
        author=author,
        metadata={"structure_method": "docx_heading_styles" if headings else "docx_blocks", "structure_confidence": 0.98 if headings else 0.55},
        processing={"format": "DOCX", "steps": ["python-docx", "ordered-paragraph-table-extraction"]},
        warnings=[] if headings else ["DOCX had no Heading styles; outline was inferred from document order."],
    )


def _extract_doc_text(source: Path) -> str:
    olefile = _require_optional("olefile", "doc")
    try:
        if not olefile.isOleFile(str(source)):
            raise ValueError("DOC input is not a valid OLE compound document")
        with olefile.OleFileIO(str(source)) as ole:
            if ole.exists("EncryptedPackage") or ole.exists("EncryptionInfo"):
                raise ValueError("encrypted DOC input is unsupported; provide an unencrypted document")
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not inspect DOC OLE container: {exc}") from exc
    module = _require_optional("legacy_doc", "doc")
    extract_text = getattr(module, "extract_text", None)
    if not callable(extract_text):
        raise ValueError("legacy-doc does not provide the required extract_text(bytes) API")
    try:
        result = extract_text(source.read_bytes())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"legacy-doc could not extract readable text from this DOC input: {exc}") from exc
    value = getattr(result, "text", None)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError("legacy-doc returned no readable text for this DOC input")


def analyze_doc(source: Path, options: ImportOptions | None = None) -> ImportedSourceDocument:
    options = options or ImportOptions()
    text = _extract_doc_text(source)
    blocks = [{"text": line, "start": number, "end": number} for number, line in enumerate(_clean_source_text(text).splitlines(), 1) if line.strip()]
    explicit = " ".join((options.title or "").split())
    title = explicit or _filename_title(source)

    def locator(first: dict[str, Any], last: dict[str, Any]) -> SourceLocator:
        return SourceLocator("DOC", "doc_text_paragraphs", {"paragraph_start": first["start"], "paragraph_end": last["end"], "precision": "inferred"})

    return _document_from_blocks(
        source, "DOC", title, "explicit" if explicit else "filename", 1.0 if explicit else 0.35,
        blocks, [], locator,
        metadata={"structure_method": "legacy_doc_text", "structure_confidence": 0.35, "fidelity": "text_only"},
        processing={"format": "DOC", "steps": ["olefile-validation", "legacy-doc-text-extraction"]},
        warnings=["DOC import is best-effort text extraction; paragraph locations are inferred and layout/tables are not preserved."],
    )


def _epub_outline(
    toc_outline: list[dict[str, Any]],
    units: list[ImportedSourceUnit],
) -> list[ImportedSourceNode]:
    """Map authored EPUB navigation to units by (href, fragment).

    When the TOC contains multiple sibling entries that share one XHTML file
    but differ in fragment (e.g.  sub-chapter anchors inside a single spine
    item), each entry maps to its own fragment-specific unit so every outline
    node gets a distinct pageId.
    """
    units_by_href_fragment: dict[tuple[str, str], ImportedSourceUnit] = {}
    for unit in units:
        loc = unit.locator.to_dict()
        key = (str(loc.get("href", "")), str(loc.get("fragment", "")))
        units_by_href_fragment[key] = unit
    assigned_unit_ids: set[str] = set()
    position = [0]

    def node_from_toc(value: dict[str, Any]) -> ImportedSourceNode:
        position[0] += 1
        node_id = f"epub-outline-{position[0]:03d}"
        node_title = " ".join(str(value.get("title", "")).split()) or "Reading unit"
        breadcrumb = tuple(value.get("breadcrumb") or [node_title])
        children = [
            node_from_toc(child)
            for child in value.get("children", [])
            if isinstance(child, dict)
        ]
        href = str(value.get("href", ""))
        fragment = str(value.get("fragment", ""))
        unit = units_by_href_fragment.get((href, fragment))
        if unit is None and fragment:
            # Fall back to a fragment-less unit when the TOC fragment does not
            # exist in the XHTML (e.g.  a mis-referenced anchor).
            unit = units_by_href_fragment.get((href, ""))
        unit_ids = [unit.id] if unit is not None else []
        assigned_unit_ids.update(unit_ids)
        return ImportedSourceNode(
            id=node_id,
            title=node_title,
            kind=_kind(node_title),
            breadcrumb=breadcrumb,
            confidence=0.98,
            unit_ids=unit_ids,
            children=children,
        )

    result = [node_from_toc(node) for node in toc_outline if isinstance(node, dict)]
    for unit in units:
        if unit.id not in assigned_unit_ids:
            position[0] += 1
            result.append(ImportedSourceNode(
                id=f"epub-fallback-{position[0]:03d}",
                title=unit.title,
                kind=unit.kind,
                breadcrumb=unit.breadcrumb or (unit.title,),
                confidence=unit.confidence,
                unit_ids=[unit.id],
            ))
    return result


def analyze_epub(source: Path, options: ImportOptions | None = None) -> ImportedSourceDocument:
    options = options or ImportOptions()
    parsed = parse_epub(source, keep_front_matter=options.keep_front_matter)
    metadata = dict(parsed["metadata"])
    metadata_title = " ".join(str(metadata.get("title", "")).split())
    explicit = " ".join((options.title or "").split())
    title = explicit or metadata_title or _filename_title(source)
    method = "explicit" if explicit else "epub_metadata" if metadata_title else "filename"
    confidence = 1.0 if explicit else 0.98 if metadata_title else 0.35
    units: list[ImportedSourceUnit] = []
    for entry in parsed["entries"]:
        number = int(entry["number"])
        entry_title = " ".join(str(entry["title"]).split()) or f"Reading unit {number}"
        locator = SourceLocator("EPUB", "epub_spine", {
            "spine_id": entry["spine"], "spine_index": entry.get("spine_index"),
            "href": entry["href"], "fragment": entry.get("fragment", ""),
        })
        units.append(ImportedSourceUnit(
            id=f"epub-unit-{number:02d}",
            title=entry_title, text=_clean_source_text(str(entry["text"])), locator=locator,
            kind=_kind(entry_title), breadcrumb=tuple(entry.get("breadcrumb") or [entry_title]),
            confidence=0.98, source_title=entry_title,
        ))
    return ImportedSourceDocument(
        format="EPUB", source_name=source.name, source_hash=str(parsed["source_hash"]),
        title=title, title_method=method, title_confidence=confidence,
        author=str(metadata.get("creator", "")), units=units,
        outline=_epub_outline(list(parsed.get("toc_outline") or []), units),
        metadata={"epub_metadata": metadata, "structure_method": "epub_spine_toc", "structure_confidence": 0.98},
        processing={"format": "EPUB", "steps": ["stdlib-zip-xml-spine-parser"]},
        locator_policy="chapter-spine-href",
    )


def _extract_kindle(source: Path) -> tuple[Path, Path]:
    mobi = _require_optional("mobi", "kindle")
    try:
        result = mobi.extract(str(source))
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if re.search(r"drm|encrypt|protected|permission", message, re.IGNORECASE):
            raise ValueError("DRM/encrypted Kindle input is unsupported; provide a DRM-free file") from exc
        raise ValueError(f"could not extract DRM-free Kindle input: {message}") from exc
    if not isinstance(result, tuple) or len(result) < 2:
        raise ValueError("mobi extractor returned an unsupported result")
    return Path(result[0]), Path(result[1])


def analyze_kindle(source: Path, options: ImportOptions | None = None) -> ImportedSourceDocument:
    options = options or ImportOptions()
    source_format = detect_source_type(source, options.source_format)
    workspace, payload = _extract_kindle(source)
    try:
        if payload.suffix.lower() == ".epub":
            unpacked = analyze_epub(payload, options)
        elif payload.suffix.lower() in {".html", ".htm", ".xhtml"}:
            unpacked = _analyze_kindle_html(payload, options)
        else:
            raise ValueError(f"Kindle extractor produced unsupported payload: {payload.name}")
        units = []
        for unit in unpacked.units:
            values = {"original_format": source_format, "unpacked_locator": unit.locator.to_dict(), "section": unit.id}
            units.append(replace(unit, locator=SourceLocator(source_format, "kindle_section", values)))
        return replace(
            unpacked, format=source_format, source_name=source.name, source_hash=sha256_file(source), units=units,
            metadata={**unpacked.metadata, "unpacked_from": {"source_name": source.name, "format": source_format}},
            processing={"format": source_format, "steps": ["mobi-kindleunpack", f"{unpacked.format.lower()}-adapter"]},
            warnings=[*unpacked.warnings, "Kindle input was imported from a DRM-free extracted payload; original binary layout is not preserved."],
            locator_policy="kindle-unpacked-native-locator",
        )
    finally:
        if workspace.is_dir():
            shutil.rmtree(workspace, ignore_errors=True)


def analyze_source(source: Path, options: ImportOptions | None = None) -> ImportedSourceDocument:
    options = options or ImportOptions()
    source_format = detect_source_type(source, options.source_format)
    if source_format == "EPUB":
        return analyze_epub(source, options)
    if source_format == "TXT":
        return analyze_text(source, options)
    if source_format == "HTML":
        return analyze_html(source, options)
    if source_format == "DOCX":
        return analyze_docx(source, options)
    if source_format == "DOC":
        return analyze_doc(source, options)
    if source_format in {"MOBI", "AZW", "AZW3"}:
        return analyze_kindle(source, options)
    raise ValueError("PDF uses the existing layout/OCR-aware compatibility importer")


def validate_source_document(document: ImportedSourceDocument) -> None:
    validate_imported_source_document(document)


def import_document(source: Path, root: Path, options: ImportOptions | None = None) -> list[Path]:
    options = options or ImportOptions()
    source_format = detect_source_type(source, options.source_format)
    if source_format == "PDF":
        return import_pdf(
            source, root, options.title, options.pages_per_chapter, options.chapter_count, options.force,
            structure=options.structure, structure_min_confidence=options.structure_min_confidence,
            structure_report=options.structure_report, max_unit_tokens=options.max_unit_tokens,
            postprocess=options.postprocess, structure_manifest=options.structure_manifest,
            pdf_ocr=options.pdf_ocr, pdf_ocr_dpi=options.pdf_ocr_dpi,
            pdf_ocr_confidence=options.pdf_ocr_confidence,
        )
    document = analyze_source(source, options)
    return write_import(document, root, options)


def import_epub(source: Path, root: Path, title: str | None = None, force: bool = False, keep_front_matter: bool = False) -> list[Path]:
    options = ImportOptions(title=title, force=force, keep_front_matter=keep_front_matter)
    return write_import(analyze_epub(source, options), root, options)
