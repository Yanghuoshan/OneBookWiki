"""Deterministic layout cleanup for PDFs that already contain extractable text.

This module never invokes OCR or a model.  It produces a second, immutable page view
for raw Markdown while callers retain the native view for structure detection.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import re
import statistics
import unicodedata
from typing import Any, Iterable

from .source_structure import PageBlock, PdfLine, PdfPage, PdfSpan


CJK_RE = re.compile(r"[⺀-⿿　-〿㐀-䶿一-鿿豈-﫿]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
PAGE_LABEL_RE = re.compile(
    r"^(?:[-—–]\s*)?(?:\d{1,4})(?:\s*[-—–])?$|^第\s*[0-9OolIiSs〇]+\s*页$|^page\s*[0-9OolIiSs〇]+$",
    re.IGNORECASE,
)
LIST_RE = re.compile(r"^(?:[-*•‣]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s+")
STRUCTURAL_RE = re.compile(
    r"^(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)|chapter\s+\d+|part\s+\d+)",
    re.IGNORECASE,
)
PUNCTUATION = set("，。！？；：、,.!?;:)]}》〉」』")
OPEN_PUNCTUATION = set("([{《〈「『")


@dataclass(frozen=True)
class PdfPostprocessResult:
    """The selected text view and compact audit information for a PDF import."""

    pages: tuple[PdfPage, ...]
    report: dict[str, Any]


@dataclass(frozen=True)
class _Line:
    source: PdfLine
    text: str
    index: int

    @property
    def x0(self) -> float:
        return self.source.x0

    @property
    def y0(self) -> float:
        return self.source.y0

    @property
    def x1(self) -> float:
        return self.source.x1

    @property
    def y1(self) -> float:
        return self.source.y1

    @property
    def size(self) -> float:
        return self.source.size


def _compact_space(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace(" ", " ")
    value = re.sub(r"[\t\r\f\v ]+", " ", value).strip()
    value = re.sub(r"(?<=[⺀-⿿　-〿㐀-䶿一-鿿豈-﫿])\s+(?=[⺀-⿿　-〿㐀-䶿一-鿿豈-﫿])", "", value)
    value = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", value)
    value = re.sub(r"([（(\[\{《〈「『])\s+", r"\1", value)
    return value


def _is_cjk(value: str) -> bool:
    return bool(value and CJK_RE.fullmatch(value))


def _line_signature(value: str) -> str:
    value = _compact_space(value).lower()
    return re.sub(r"[\s\-—–_·•.。,:：;；/\\|()[\]{}<>《》〈〉'\"`~!！?？]+", "", value)


def _is_page_label(value: str) -> bool:
    return bool(PAGE_LABEL_RE.fullmatch(_compact_space(value)))


def _is_latin_lower_start(value: str) -> bool:
    return bool(re.match(r"^[a-z]", value))


def _line_height(line: _Line) -> float:
    return max(0.1, line.y1 - line.y0, line.size or 0.1)


def _sample(target: list[dict[str, Any]], value: dict[str, Any], limit: int = 12) -> None:
    if len(target) < limit:
        target.append(value)


def _join_spans(line: PdfLine) -> tuple[str, int]:
    """Join spans without creating CJK spacing or losing ordinary Latin spacing."""
    spans = tuple(span for span in line.spans if _compact_space(span.text))
    if not spans:
        return _compact_space(line.text), 0
    result = ""
    changes = 0
    previous: PdfSpan | None = None
    for span in spans:
        text = _compact_space(span.text)
        if not result:
            result = text
            previous = span
            continue
        left = result[-1]
        right = text[0]
        separator = ""
        gap = max(0.0, span.x0 - (previous.x1 if previous else span.x0))
        threshold = max(0.8, (span.size or previous.size or 10.0) * 0.18) if previous else 0.8
        if left not in OPEN_PUNCTUATION and right not in PUNCTUATION:
            if not (_is_cjk(left) and _is_cjk(right)):
                if left.isascii() and right.isascii() and left.isalnum() and right.isalnum() and gap > threshold:
                    separator = " "
                elif (left.isascii() and left.isalnum() and _is_cjk(right)) or (_is_cjk(left) and right.isascii() and right.isalnum()):
                    separator = " " if gap > threshold else ""
        if separator:
            changes += 1
        result += separator + text
        previous = span
    return _compact_space(result), changes


def _prepared_lines(page: PdfPage, counters: Counter[str]) -> list[_Line]:
    records = page.lines
    if not records:
        records = tuple(
            line
            for block in page.blocks
            for line in block.lines
        )
    prepared: list[_Line] = []
    for index, line in enumerate(records):
        text, joined = _join_spans(line)
        counters["span_joins"] += joined
        if text:
            prepared.append(_Line(line, text, index))
    return prepared


def _edge_kind(page: PdfPage, line: _Line) -> str | None:
    if not page.height:
        return None
    if line.y1 <= page.height * 0.08:
        return "top"
    if line.y0 >= page.height * 0.92:
        return "bottom"
    return None


def _repeated_edge_lines(pages: list[PdfPage], prepared: dict[int, list[_Line]]) -> set[tuple[int, int]]:
    """Find only corroborated repeated headers/footers, never page-local guesses."""
    candidates: dict[tuple[str, str], list[tuple[PdfPage, _Line]]] = defaultdict(list)
    for page in pages:
        for line in prepared[page.number]:
            edge = _edge_kind(page, line)
            signature = _line_signature(line.text)
            if edge and signature and len(line.text) <= 140:
                candidates[(edge, signature)].append((page, line))

    threshold = max(3, math.ceil(len(pages) * 0.20))
    selected: set[tuple[int, int]] = set()
    for records in candidates.values():
        page_numbers = {page.number for page, _ in records}
        if len(page_numbers) < threshold:
            continue
        x_positions = [line.x0 / page.width for page, line in records if page.width]
        widths = [(line.x1 - line.x0) / page.width for page, line in records if page.width]
        sizes = [line.size for _, line in records if line.size]
        if x_positions and max(x_positions) - min(x_positions) > 0.14:
            continue
        if widths and max(widths) - min(widths) > 0.22:
            continue
        if sizes and max(sizes) - min(sizes) > max(2.0, statistics.median(sizes) * 0.35):
            continue
        selected.update((page.number, line.index) for page, line in records)
    return selected


def _remove_overlays(lines: Iterable[_Line], counters: Counter[str]) -> list[_Line]:
    result: list[_Line] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for line in lines:
        key = (
            _line_signature(line.text),
            round(line.x0 * 2),
            round(line.y0 * 2),
            round(line.x1 * 2),
            round(line.y1 * 2),
        )
        if key in seen:
            counters["overlay_duplicates"] += 1
            continue
        seen.add(key)
        result.append(line)
    return result


def _column_order(page: PdfPage, lines: list[_Line]) -> tuple[list[tuple[_Line, int]], bool, bool]:
    """Return ordered lines, two-column decision, and conservative ambiguity state."""
    if len(lines) < 4 or not page.width:
        return [(line, 0) for line in sorted(lines, key=lambda item: (item.y0, item.x0))], False, False

    wide = [line for line in lines if line.x1 - line.x0 >= page.width * 0.68]
    body = [line for line in lines if line not in wide]
    if len(body) < 4:
        return [(line, 0) for line in sorted(lines, key=lambda item: (item.y0, item.x0))], False, bool(wide)
    centers = sorted((line.x0 + line.x1) / 2 for line in body)
    gaps = [(right - left, index) for index, (left, right) in enumerate(zip(centers, centers[1:]))]
    gap, index = max(gaps, default=(0.0, 0))
    if gap < page.width * 0.18:
        return [(line, 0) for line in sorted(lines, key=lambda item: (item.y0, item.x0))], False, False
    split = (centers[index] + centers[index + 1]) / 2
    left = [line for line in body if (line.x0 + line.x1) / 2 < split]
    right = [line for line in body if (line.x0 + line.x1) / 2 >= split]
    if len(left) < 2 or len(right) < 2 or any(line.x0 < split < line.x1 for line in body):
        return [(line, 0) for line in sorted(lines, key=lambda item: (item.y0, item.x0))], False, True
    if wide:
        top = min(line.y0 for line in body)
        if any(line.y0 > top + _line_height(line) * 2 for line in wide):
            return [(line, 0) for line in sorted(lines, key=lambda item: (item.y0, item.x0))], False, True
    ordered: list[tuple[_Line, int]] = []
    ordered.extend((line, -1) for line in sorted(wide, key=lambda item: (item.y0, item.x0)))
    ordered.extend((line, 0) for line in sorted(left, key=lambda item: (item.y0, item.x0)))
    ordered.extend((line, 1) for line in sorted(right, key=lambda item: (item.y0, item.x0)))
    return ordered, True, False


def _looks_like_heading(line: _Line, page: PdfPage, median_size: float) -> bool:
    text = line.text
    centered = bool(page.width and abs((line.x0 + line.x1) / 2 - page.width / 2) <= page.width * 0.12)
    short = len(text) <= 100
    prominent = line.size >= max(13.0, median_size * 1.18)
    return short and (STRUCTURAL_RE.match(text) is not None or (centered and prominent))


def _join_lines(previous: str, current: str) -> tuple[str, bool]:
    if previous.endswith("-") and re.search(r"[A-Za-z]-$", previous) and _is_latin_lower_start(current):
        return previous[:-1] + current, True
    if not previous:
        return current, False
    left, right = previous[-1], current[0]
    if left == "-":
        return previous + current, False
    if _is_cjk(left) and _is_cjk(right):
        return previous + current, False
    if left in OPEN_PUNCTUATION or right in PUNCTUATION:
        return previous + current, False
    if (left.isascii() and left.isalnum()) and (right.isascii() and right.isalnum()):
        return previous + " " + current, False
    if (left.isascii() and left.isalnum() and _is_cjk(right)) or (_is_cjk(left) and right.isascii() and right.isalnum()):
        return previous + " " + current, False
    return previous + current, False


def _paragraph_blocks(page: PdfPage, ordered: list[tuple[_Line, int]], counters: Counter[str]) -> tuple[tuple[PageBlock, ...], str]:
    if not ordered:
        return (), ""
    by_column: dict[int, list[_Line]] = defaultdict(list)
    order: list[int] = []
    for line, column in ordered:
        if column not in by_column:
            order.append(column)
        by_column[column].append(line)

    blocks: list[PageBlock] = []
    for column in order:
        lines = by_column[column]
        sizes = [line.size for line in lines if line.size]
        median_size = statistics.median(sizes) if sizes else 10.0
        gaps = [max(0.0, current.y0 - previous.y1) for previous, current in zip(lines, lines[1:])]
        positive_gaps = [gap for gap in gaps if gap > 0]
        normal_gap = statistics.median(positive_gaps) if positive_gaps else median_size * 0.35
        current_lines: list[_Line] = []
        current_text = ""

        def flush() -> None:
            nonlocal current_lines, current_text
            if not current_lines or not current_text:
                current_lines, current_text = [], ""
                return
            first, last = current_lines[0], current_lines[-1]
            blocks.append(PageBlock(
                text=current_text,
                x0=min(line.x0 for line in current_lines),
                y0=first.y0,
                x1=max(line.x1 for line in current_lines),
                y1=last.y1,
                size=max((line.size for line in current_lines), default=0.0),
                lines=tuple(PdfLine(line.text, line.x0, line.y0, line.x1, line.y1, line.size, line.source.spans) for line in current_lines),
            ))
            current_lines, current_text = [], ""

        for line in lines:
            heading = _looks_like_heading(line, page, median_size)
            list_item = bool(LIST_RE.match(line.text))
            boundary = False
            if current_lines:
                previous = current_lines[-1]
                gap = max(0.0, line.y0 - previous.y1)
                indent_changed = abs(line.x0 - previous.x0) > max(page.width * 0.055, median_size * 1.8) if page.width else False
                style_changed = previous.size and line.size and abs(line.size - previous.size) > max(2.0, median_size * 0.32)
                boundary = gap > max(normal_gap * 1.8, normal_gap + median_size * 0.55) or indent_changed or style_changed or heading or list_item
            if boundary:
                flush()
                counters["paragraph_breaks"] += 1
            if heading or list_item:
                current_lines = [line]
                current_text = line.text
                flush()
                continue
            if not current_lines:
                current_lines = [line]
                current_text = line.text
            else:
                current_text, dehyphenated = _join_lines(current_text, line.text)
                current_lines.append(line)
                if dehyphenated:
                    counters["hyphen_wraps"] += 1
        flush()
    text = "\n\n".join(block.text for block in blocks if block.text)
    return tuple(blocks), text


def _processed_page(page: PdfPage, lines: list[_Line], counters: Counter[str]) -> tuple[PdfPage, bool, bool]:
    ordered, two_columns, ambiguous = _column_order(page, lines)
    blocks, text = _paragraph_blocks(page, ordered, counters)
    processed_lines = tuple(
        PdfLine(line.text, line.x0, line.y0, line.x1, line.y1, line.size, line.source.spans)
        for line, _ in ordered
    )
    result = PdfPage(page.number, text, blocks, page.height, page.width, processed_lines)
    return result, two_columns, ambiguous


def _base_report(mode: str, applied: bool, pages: int, **extra: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "version": "layout-v1",
        "mode": mode,
        "engine": "pymupdf-existing-text",
        "semantic_model": "none",
        "applied": applied,
        "structure_view": "native",
        "payload_view": "postprocessed" if applied else "native",
        "pages_processed": pages if applied else 0,
        "pages_changed": 0,
        "rules": {},
        "removed": {},
        "columns": {"two_column_pages": 0},
        "ambiguous_pages": [],
    }
    report.update(extra)
    return report


def postprocess_pdf_pages(pages: list[PdfPage], *, mode: str = "auto") -> PdfPostprocessResult:
    """Create a deterministic processed PDF text view while retaining native pages.

    ``auto`` and ``strict`` both use conservative rules.  In strict mode a page that
    would become empty despite containing source lines is retained unchanged.
    """
    if mode not in {"off", "auto", "strict"}:
        raise ValueError(f"unsupported PDF postprocess mode: {mode}")
    source_pages = tuple(pages)
    if mode == "off":
        return PdfPostprocessResult(source_pages, _base_report(mode, False, len(pages), reason="disabled"))
    has_layout = any(page.lines or any(block.lines for block in page.blocks) for page in pages)
    if not has_layout:
        return PdfPostprocessResult(source_pages, _base_report(mode, False, len(pages), reason="layout-unavailable"))

    counters: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    prepared = {page.number: _prepared_lines(page, counters) for page in pages}
    repeated = _repeated_edge_lines(pages, prepared)
    processed: list[PdfPage] = []
    two_columns = 0
    ambiguous_pages: list[int] = []
    changed_pages = 0

    for page in pages:
        if not prepared[page.number]:
            processed.append(page)
            continue
        retained: list[_Line] = []
        for line in prepared[page.number]:
            edge = _edge_kind(page, line)
            if (page.number, line.index) in repeated:
                counters["running_headers"] += 1
                _sample(samples, {"page": page.number, "rule": "running-header", "text": line.text})
                continue
            if edge and _is_page_label(line.text):
                counters["page_labels"] += 1
                _sample(samples, {"page": page.number, "rule": "page-label", "text": line.text})
                continue
            retained.append(line)
        retained = _remove_overlays(retained, counters)
        selected, page_two_columns, ambiguous = _processed_page(page, retained, counters)
        if mode == "strict" and page.text.strip() and not selected.text.strip():
            selected = page
            ambiguous = True
            counters["strict_reverted_pages"] += 1
        if selected.text != page.text:
            changed_pages += 1
        processed.append(selected)
        two_columns += int(page_two_columns)
        if ambiguous:
            ambiguous_pages.append(page.number)

    report = _base_report(mode, True, len(pages))
    report.update({
        "pages_changed": changed_pages,
        "rules": dict(sorted(counters.items())),
        "removed": {
            "running_headers": counters["running_headers"],
            "page_labels": counters["page_labels"],
            "overlay_duplicates": counters["overlay_duplicates"],
        },
        "columns": {"two_column_pages": two_columns},
        "ambiguous_pages": ambiguous_pages,
        "samples": samples,
    })
    return PdfPostprocessResult(tuple(processed), report)
