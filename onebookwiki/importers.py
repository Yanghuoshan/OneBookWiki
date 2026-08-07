"""Dependency-light EPUB and source metadata import helpers."""
from __future__ import annotations

import hashlib
import html
import posixpath
import re
import unicodedata
import zipfile
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


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


def _toc_titles(archive: zipfile.ZipFile, opf_dir: str, href: str) -> dict[str, str]:
    path = posixpath.normpath(posixpath.join(opf_dir, href))
    try:
        root = ElementTree.fromstring(_read_text(archive, path))
    except (KeyError, ElementTree.ParseError):
        return {}
    result: dict[str, str] = {}
    for point in _findall(root, "navPoint"):
        label = _find(point, "text")
        content = _find(point, "content")
        if label is not None and content is not None and content.attrib.get("src"):
            result[posixpath.normpath(posixpath.join(opf_dir, content.attrib["src"].split("#", 1)[0]))] = " ".join("".join(label.itertext()).split())
    for link in _findall(root, "a"):
        href_value = link.attrib.get("href")
        if href_value:
            result[posixpath.normpath(posixpath.join(opf_dir, href_value.split("#", 1)[0]))] = " ".join("".join(link.itertext()).split())
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
        toc_titles: dict[str, str] = {}
        if toc_id and toc_id in manifest:
            toc_titles.update(_toc_titles(archive, opf_dir, manifest[toc_id]["href"]))
        for item in manifest.values():
            if item["media_type"] == "application/x-dtbncx+xml":
                toc_titles.update(_toc_titles(archive, opf_dir, item["href"]))
        entries: list[dict[str, Any]] = []
        for spine_index, itemref in enumerate(_findall(spine, "itemref") if spine is not None else [], 1):
            item_id = itemref.attrib.get("idref")
            item = manifest.get(item_id or "")
            if not item or "html" not in item["media_type"] and "xhtml" not in item["media_type"]:
                continue
            href_value, _, fragment = item["href"].partition("#")
            href = posixpath.normpath(posixpath.join(opf_dir, href_value))
            title = toc_titles.get(href) or f"Chapter {spine_index}"
            body = html_to_text(_read_text(archive, href))
            if not body or (not keep_front_matter and spine_index == 1 and len(body.split()) < 40):
                continue
            entries.append({
                "number": len(entries) + 1,
                "title": title,
                "href": href,
                "fragment": fragment,
                "spine": item_id or "",
                "spine_index": spine_index,
                "text": body,
            })
        return {"metadata": metadata, "entries": entries, "source_hash": sha256_file(path), "format": "EPUB"}


def write_source_metadata(root: Path, source: dict[str, Any]) -> Path:
    target = root / ".onebookwiki" / "source.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    import json
    target.write_text(json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def extract_pdf_pages(source: Path) -> list[str]:
    """Extract page text through the optional PyMuPDF dependency."""
    try:
        import fitz
    except ImportError as exc:
        raise ValueError("Install PyMuPDF with: pip install pymupdf") from exc
    document = fitz.open(source)
    return [page.get_text("text") for page in document]


def pdf_chapter_ranges(page_count: int, pages_per_chapter: int | None, chapter_count: int | None) -> list[tuple[int, int]]:
    if pages_per_chapter:
        return [(start, min(page_count, start + pages_per_chapter)) for start in range(0, page_count, pages_per_chapter)]
    if chapter_count:
        size = max(1, (page_count + chapter_count - 1) // chapter_count)
        return [(start, min(page_count, start + size)) for start in range(0, page_count, size)]
    return [(0, page_count)]


def import_pdf(source: Path, root: Path, title: str | None = None, pages_per_chapter: int | None = None, chapter_count: int | None = None, force: bool = False, pages: list[str] | None = None) -> list[Path]:
    """Import PDF pages as immutable chapter Markdown with page provenance."""
    pages = pages if pages is not None else extract_pdf_pages(source)
    book_title = title or source.stem
    output = root / "raw" / "chapters"
    output.mkdir(parents=True, exist_ok=True)
    ranges = pdf_chapter_ranges(len(pages), pages_per_chapter, chapter_count)
    written: list[Path] = []
    for number, (start, end) in enumerate(ranges, 1):
        target = output / f"{number:02d}-{slugify(book_title)}.md"
        if target.exists() and not force:
            written.append(target)
            continue
        body = "\n\n".join(
            f"## Page {page_number}\n\n{text.strip()}"
            for page_number, text in enumerate(pages[start:end], start + 1)
            if text.strip()
        )
        target.write_text(
            f"# Chapter {number}: {book_title}\n\n"
            f"> Book: {book_title}\n> Edition: Unknown\n> Chapter: {number}\n"
            f"> Pages: {start + 1}-{end}\n> Source: {source.name}\n"
            f"> Collected: {date.today().isoformat()}\n> Published: Unknown\n> Format: PDF\n\n{body}\n",
            encoding="utf-8",
        )
        written.append(target)
    write_source_metadata(root, {
        "schema_version": 2,
        "title": book_title,
        "format": "PDF",
        "source_name": source.name,
        "source_hash": sha256_file(source),
        "page_count": len(pages),
        "pages_per_chapter": pages_per_chapter,
        "chapter_count": chapter_count,
        "locator_policy": "pdf-physical-page-1-based",
        "chapters": [
            {
                "number": number,
                "raw_path": f"raw/chapters/{number:02d}-{slugify(book_title)}.md",
                "physical_page_start": start + 1,
                "physical_page_end": end,
            }
            for number, (start, end) in enumerate(ranges, 1)
        ],
    })
    return written


def import_epub(source: Path, root: Path, title: str | None = None, force: bool = False, keep_front_matter: bool = False) -> list[Path]:
    parsed = parse_epub(source, keep_front_matter=keep_front_matter)
    metadata = parsed["metadata"]
    book_title = title or metadata.get("title") or source.stem
    output = root / "raw" / "chapters"
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for entry in parsed["entries"]:
        fallback = f"chapter-{entry['number']}"
        filename = f"{entry['number']:02d}-{slugify(entry['title'], fallback)}.md"
        target = output / filename
        if target.exists() and not force:
            written.append(target)
            continue
        content = (
            f"# Chapter {entry['number']}: {entry['title']}\n\n"
            f"> Book: {book_title}\n> Author: {metadata.get('creator', 'Unknown')}\n"
            f"> Edition: {metadata.get('edition', 'Unknown')}\n> Chapter: {entry['number']}\n"
            f"> Source: {source.name}\n> Format: EPUB\n> Spine: {entry['spine']}\n"
            f"> Spine Index: {entry.get('spine_index', '')}\n> Href: {entry['href']}\n"
            f"> Fragment: {entry.get('fragment', '')}\n> Collected: {date.today().isoformat()}\n\n"
            f"{entry['text']}\n"
        )
        target.write_text(content, encoding="utf-8")
        written.append(target)
    write_source_metadata(root, {
        "schema_version": 2,
        "title": book_title,
        "author": metadata.get("creator", "Unknown"),
        "format": "EPUB",
        "source_name": source.name,
        "source_hash": parsed["source_hash"],
        "locator_policy": "chapter-spine-href",
        "chapters": [
            {
                "number": item["number"],
                "title": item["title"],
                "href": item["href"],
                "fragment": item.get("fragment", ""),
                "spine": item["spine"],
                "spine_index": item.get("spine_index"),
            }
            for item in parsed["entries"]
        ],
    })
    return written
