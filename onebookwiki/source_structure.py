"""Deterministic source-title and source-structure analysis for imported books.

The PDF path deliberately works from text and layout information that already exists in
PDF files.  It does not invoke OCR: a PDF without useful extractable text is reported as
unrecognised and falls back to explicitly-labelled page ranges.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import statistics
import unicodedata
from typing import Any, Iterable

from .chunking import count_tokens


DEFAULT_MAX_UNIT_TOKENS = 12000


@dataclass(frozen=True)
class PageBlock:
    text: str
    y0: float = 0.0
    y1: float = 0.0
    size: float = 0.0


@dataclass(frozen=True)
class PdfPage:
    number: int
    text: str
    blocks: tuple[PageBlock, ...] = ()
    height: float = 0.0


@dataclass(frozen=True)
class BookTitle:
    value: str
    method: str
    confidence: float
    candidates: tuple[dict[str, Any], ...] = ()


@dataclass
class SourceUnit:
    id: str
    title: str
    kind: str
    breadcrumb: tuple[str, ...]
    physical_page_start: int
    physical_page_end: int
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None


@dataclass
class SourceNode:
    id: str
    title: str
    kind: str
    breadcrumb: tuple[str, ...]
    confidence: float
    unit_ids: list[str] = field(default_factory=list)
    children: list["SourceNode"] = field(default_factory=list)


@dataclass
class SourceAnalysis:
    title: BookTitle
    method: str
    confidence: float
    units: list[SourceUnit]
    outline: list[SourceNode]
    report: dict[str, Any]


@dataclass(frozen=True)
class ReadingPart:
    source_unit_id: str
    title: str
    kind: str
    breadcrumb: tuple[str, ...]
    confidence: float
    part: int
    part_count: int
    physical_page_start: int
    physical_page_end: int
    page_fragments: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class BoundaryCandidate:
    """A possible reading-unit start observed at the top of a PDF page."""

    page: int
    title: str
    signature: str
    kind: str
    score: float
    source: str
    body_after: bool
    layout_backed: bool
    page_label_suffix: bool = False
    rejection: str | None = None


@dataclass(frozen=True)
class ContentBoundaryDetection:
    accepted: tuple[BoundaryCandidate, ...]
    rejected: tuple[BoundaryCandidate, ...]
    running_headers: dict[str, tuple[int, ...]]


TOC_MARKERS = ("目录", "目錄", "contents", "table of contents")
FRONT_MATTER_MARKERS = ("序", "前言", "推荐序", "推薦序", "译序", "譯序", "引言", "导言", "導言", "鸣谢", "鳴謝")
END_MARKERS = ("结语", "結語", "结论", "結論", "后记", "後記", "附录", "附錄", "参考文献", "參考文獻")
TITLE_NOISE = (
    "isbn", "版权所有", "版次", "印刷", "出版社", "责任编辑", "責任編輯", "译者", "譯者", "作者",
    "copyright", "all rights", "价格", "定价", "定價", "translated by", "translation by",
    "with analysis", "university press", "press", "foreword by",
)


def _clean_space(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).replace(" ", " ").split())


def normalise(value: str) -> str:
    value = _clean_space(value).lower()
    value = re.sub(r"[\s\-—–_·•.。,:：;；/\\|()[\]{}<>《》〈〉'\"`~!！?？]+", "", value)
    return value


def _clean_filename_title(path: Path) -> str:
    value = _clean_space(path.stem.replace("_", " "))
    # Common ebook filenames append author names after a dash. Keep Chinese dashes
    # intact because they can be part of a title, but remove an obvious ASCII suffix.
    value = re.sub(r"\s+-\s+[^-]{2,}$", "", value).strip()
    return value or "Untitled book"


def _looks_like_title(value: str) -> bool:
    value = _clean_space(value).strip("[]【】()（）")
    if not value or len(value) < 2 or len(value) > 80:
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in TITLE_NOISE):
        return False
    if re.search(r"^(page|第?\s*\d+\s*页|\d{1,4})$", value, flags=re.IGNORECASE):
        return False
    if value.count(" ") > 10 or re.search(r"https?://|www\.", value, flags=re.IGNORECASE):
        return False
    # Author credit lines are usually not book titles. This also excludes common
    # Western bylines even when a damaged extraction has separated the title.
    if re.search(r"(?:著|编|編|译|譯|主编|主編)\s*$", value):
        return False
    if re.match(r"^(?:by|edited by|translated by|introduction by|foreword by)\b", value, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(?:by|author|editor|translator)\b", value, flags=re.IGNORECASE) and len(value.split()) <= 10:
        return False
    return True


def _title_quality(value: str, filename_title: str) -> float:
    """Score title-shaped text conservatively before layout prominence.

    The title page of a PDF is often split into typographic fragments. A fragment
    such as ``OF SPIRIT`` can be large and early but should not outrank a clean
    filename-derived title. This term rewards coverage of the filename and
    penalises all-caps fragments, damaged glyphs, and generic paratext headings.
    """
    compact = normalise(value)
    filename = normalise(filename_title)
    if not compact:
        return 0.0
    similarity = _similarity(compact, filename)
    score = 0.20 + similarity * 0.65
    # ``_similarity`` deliberately grants a baseline to contained strings for
    # TOC matching. For titles that makes a short suffix ("OF SPIRIT") look
    # deceptively similar to a full filename title; it is still only a fragment.
    if compact in filename and len(compact) / max(1, len(filename)) < 0.72:
        score -= 0.35
    words = [word for word in re.findall(r"[A-Za-z]+", value) if word]
    if words and all(word.isupper() for word in words) and len(words) <= 3:
        score -= 0.20
    if re.search(r"[{}~\\]|['’]", value):
        score -= 0.18
    generic = {"contents", "foreword", "preface", "introduction", "acknowledgments", "acknowledgements"}
    if compact in generic:
        score -= 0.25
    return max(0.0, min(1.0, score))


def _page_lines(page: PdfPage) -> list[str]:
    return [_clean_space(line) for line in page.text.splitlines() if _clean_space(line)]


def _candidate_title_lines(pages: list[PdfPage]) -> list[tuple[str, int, float]]:
    candidates: list[tuple[str, int, float]] = []
    for page in pages[: min(8, len(pages))]:
        blocks = page.blocks or tuple(PageBlock(line) for line in _page_lines(page))
        for position, block in enumerate(blocks[:16]):
            text = _clean_space(block.text).strip("[]【】()（）")
            if not _looks_like_title(text):
                continue
            # Earlier, larger blocks are more likely title-page headings. We retain
            # every candidate and let repeated appearance make the choice auditable.
            prominence = max(0.0, 1.0 - position / 20)
            if block.size:
                prominence += min(block.size / 48, 0.35)
            candidates.append((text, page.number, prominence))
    return candidates


def recognise_book_title(pages: list[PdfPage], source: Path, explicit_title: str | None = None) -> BookTitle:
    """Select a title deterministically from an explicit value, book text, or filename."""
    if explicit_title and _clean_space(explicit_title):
        return BookTitle(_clean_space(explicit_title), "explicit", 1.0, ())

    filename_title = _clean_filename_title(source)
    lines = _candidate_title_lines(pages)
    occurrences = Counter(normalise(text) for text, _, _ in lines)
    records: list[dict[str, Any]] = [{
        "title": filename_title,
        "method": "filename",
        "score": 0.35,
        "pages": [],
    }]
    seen: set[str] = {normalise(filename_title)}
    for text, page, prominence in lines:
        key = normalise(text)
        if not key or key in seen:
            continue
        seen.add(key)
        similar_filename = 1.0 if key == normalise(filename_title) else _similarity(key, normalise(filename_title))
        repeated = occurrences[key]
        quality = _title_quality(text, filename_title)
        # Text quality dominates raw font prominence. A large but damaged title
        # fragment must not beat a clean filename candidate solely because it is
        # printed in display type on a title page.
        score = min(0.95, 0.16 + quality * 0.55 + prominence * 0.12 + min(repeated, 3) * 0.07 + similar_filename * 0.10)
        records.append({
            "title": text,
            "method": "book_text",
            "score": round(score, 4),
            "quality": round(quality, 4),
            "filename_similarity": round(similar_filename, 4),
            "pages": [candidate_page for candidate, candidate_page, _ in lines if normalise(candidate) == key],
        })
    records.sort(key=lambda item: (-float(item["score"]), -float(item.get("quality", 0.0)), len(str(item["title"])), str(item["title"])))
    selected = records[0]
    if selected["method"] == "book_text" and float(selected["score"]) >= 0.58 and float(selected.get("quality", 0.0)) >= 0.42:
        return BookTitle(str(selected["title"]), "book_text", float(selected["score"]), tuple(records))
    return BookTitle(filename_title, "filename", 0.35, tuple(records))


def _kind(title: str) -> str:
    compact = normalise(title)
    if any(normalise(marker) in compact for marker in END_MARKERS):
        return "back_matter"
    if any(normalise(marker) in compact for marker in FRONT_MATTER_MARKERS):
        return "front_matter"
    if re.search(r"(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部)|part\s+\d+)", title, flags=re.IGNORECASE):
        return "part"
    if re.search(r"(?:第?[一二三四五六七八九十百千万0-9]+(?:章|节|節)|chapter\s+\d+|\d+\s*[.、])", title, flags=re.IGNORECASE):
        return "chapter"
    return "section"


def _toc_pages(pages: list[PdfPage]) -> list[PdfPage]:
    return [page for page in pages[: min(len(pages), 20)] if any(marker in page.text.lower() for marker in TOC_MARKERS)]


def _page_label_value(value: str) -> int | None:
    """Return a short, OCR-tolerant printed page number and reject arbitrary text."""
    translated = _clean_space(value).translate(str.maketrans({
        "O": "0", "o": "0", "〇": "0", "I": "1", "i": "1", "l": "1", "S": "5",
    }))
    compact = re.sub(r"[\s\[\]{}()（）_.,:：;；\-—–]", "", translated)
    if not re.fullmatch(r"\d{1,4}", compact):
        return None
    return int(compact)


def _parse_page_number(value: str) -> int | None:
    return _page_label_value(value)


def _marker_kind(value: str) -> str | None:
    compact = normalise(value)
    if compact in {normalise(marker) for marker in FRONT_MATTER_MARKERS}:
        return "front_matter"
    if compact in {normalise(marker) for marker in END_MARKERS}:
        return "back_matter"
    return None


def _structural_heading(value: str) -> bool:
    """Recognise a structural label only when it starts the candidate line.

    Numbered phrases such as ``前三章`` and ``第三部书`` are common prose, so a
    substring search creates false units in otherwise ordinary paragraphs.
    """
    return bool(re.match(
        r"^\s*(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)|chapter\s+\d+|part\s+\d+)",
        value,
        re.IGNORECASE,
    ))


def _header_signature(value: str) -> tuple[str, bool]:
    """Normalise a title-shaped page header without altering persisted titles."""
    text = _clean_space(value)
    suffix = re.search(r"[\s_\[\]{}()（）0-9Oo〇IlIiSs.,:：;；\-—–]{2,6}$", text)
    if suffix:
        prefix = text[:suffix.start()].strip()
        if prefix and (_structural_heading(prefix) or _marker_kind(prefix)):
            return normalise(prefix), True
    return normalise(text), False


def parse_printed_toc(pages: Iterable[PdfPage]) -> list[dict[str, Any]]:
    """Parse same-line and alternating native-text contents rows deterministically."""
    entries: list[dict[str, Any]] = []
    pending_title: str | None = None
    pending_page: int | None = None
    pending_page_line: int | None = None
    marker_keys = {normalise(marker) for marker in TOC_MARKERS}
    page_token = r"[0-9OolIiS〇][0-9OolIiS〇\s\[\]{}()（）_.,:：;；\-—–]{0,7}"
    row = re.compile(rf"^(?P<title>.+?)(?:\s*(?:[.·…]{{2,}}|/)\s*|\s{{2,}})(?P<page>{page_token})\s*$")
    numbered_row = re.compile(
        rf"^(?P<title>(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)|chapter\s+\d+|part\s+\d+).+?)\s+(?P<page>{page_token})\s*$",
        re.IGNORECASE,
    )

    def append(title: str, printed: int | None, page: int, line: int, style: str) -> bool:
        cleaned = _clean_space(title).strip(".·… ")
        if not printed or not _looks_like_title(cleaned):
            return False
        if entries and printed <= int(entries[-1]["printed_page"]):
            return False
        key = normalise(cleaned)
        if not key or any(normalise(str(entry["title"])) == key for entry in entries):
            return False
        entries.append({
            "title": cleaned,
            "printed_page": printed,
            "toc_page": page,
            "line": line,
            "parse_style": style,
        })
        return True

    for page in pages:
        pending_title = None
        pending_page = None
        pending_page_line = None
        for line_number, raw in enumerate(_page_lines(page), 1):
            line = _clean_space(raw)
            if normalise(line) in marker_keys:
                pending_title = None
                pending_page = None
                pending_page_line = None
                continue
            match = row.match(line) or numbered_row.match(line)
            if match:
                append(
                    match.group("title"),
                    _parse_page_number(match.group("page")),
                    page.number,
                    line_number,
                    "inline",
                )
                pending_title = None
                pending_page = None
                pending_page_line = None
                continue
            printed = _parse_page_number(line)
            if printed is not None:
                if pending_title is not None:
                    append(pending_title, printed, page.number, line_number, "title_then_page")
                    pending_title = None
                    pending_page = None
                    pending_page_line = None
                else:
                    pending_page = printed
                    pending_page_line = line_number
                continue
            if not _looks_like_title(line) or len(line) > 96:
                continue
            if pending_page is not None:
                append(line, pending_page, page.number, pending_page_line or line_number, "page_then_title")
                pending_page = None
                pending_page_line = None
                pending_title = None
            elif pending_title is not None:
                candidate = f"{pending_title} {line}"
                if _looks_like_title(candidate) and len(candidate) <= 96:
                    pending_title = candidate
            else:
                pending_title = line
    return entries


def _body_follows(pages: list[PdfPage], page_index: int, line_index: int) -> bool:
    """Check that a candidate transitions into prose on this or the next page."""
    probes: list[str] = _page_lines(pages[page_index])[line_index + 1:]
    if not probes and page_index + 1 < len(pages):
        probes = _page_lines(pages[page_index + 1])[:4]
    for line in probes[:6]:
        if _page_label_value(line) is not None:
            continue
        if _structural_heading(line) or _marker_kind(line):
            continue
        if len(normalise(line)) >= 10:
            return True
    return False


def _layout_heading_evidence(page: PdfPage, title: str, signature: str) -> tuple[bool, bool]:
    if not page.blocks:
        return False, False
    matching = []
    for block in page.blocks[:8]:
        block_signature, _ = _header_signature(block.text)
        if normalise(title) in normalise(block.text) or signature == block_signature:
            matching.append(block)
    if not matching:
        return False, False
    block = matching[0]
    top = not page.height or block.y0 <= page.height * 0.32
    prominent = block.size >= 16.0
    return top, prominent


def _content_boundary_candidates(pages: list[PdfPage], toc_titles: Iterable[str] = ()) -> ContentBoundaryDetection:
    """Find conservative page-start units and distinguish running headers."""
    targets = {normalise(title) for title in toc_titles if normalise(title)}
    raw: list[BoundaryCandidate] = []
    marker_keys = {normalise(marker) for marker in TOC_MARKERS}
    for page_index, page in enumerate(pages):
        page_line_keys = {normalise(line) for line in _page_lines(page)}
        if page_line_keys & marker_keys:
            continue
        lines = _page_lines(page)
        for line_index, line in enumerate(lines[:4]):
            title = _clean_space(line)
            if not _looks_like_title(title):
                continue
            if len(normalise(title)) < 2:
                continue
            signature, page_label_suffix = _header_signature(title)
            marker_kind = _marker_kind(signature)
            structural = _structural_heading(title) or _structural_heading(signature)
            matches_toc = bool(signature and signature in targets)
            if not (structural or marker_kind or matches_toc):
                continue
            kind = marker_kind or (_kind(title) if structural else "section")
            body_after = _body_follows(pages, page_index, line_index)
            if not page.blocks and not body_after and not (structural and matches_toc):
                continue
            top_layout, prominent = _layout_heading_evidence(page, title, signature)
            score = 0.0
            if structural:
                score += 0.60
            if marker_kind:
                score += 0.55
            if matches_toc:
                score += 0.50
            if line_index == 0:
                score += 0.10
            if body_after:
                score += 0.10
            if top_layout:
                score += 0.06
            if prominent:
                score += 0.06
            raw.append(BoundaryCandidate(
                page.number,
                title,
                signature,
                kind,
                min(score, 1.0),
                "layout_top" if top_layout else "text_top",
                body_after,
                top_layout,
                page_label_suffix,
            ))
            break

    suffix_pages: dict[str, list[int]] = {}
    for candidate in raw:
        if candidate.page_label_suffix and candidate.signature:
            suffix_pages.setdefault(candidate.signature, []).append(candidate.page)
    # A page-number-shaped suffix is enough to identify a running-header variant.
    # Repetition strengthens the diagnosis but a single observed instance must also
    # be surfaced so that reports explain why it could not create a reading unit.
    running_headers = {
        signature: tuple(page_numbers)
        for signature, page_numbers in suffix_pages.items()
    }
    first_page: dict[str, int] = {}
    accepted: list[BoundaryCandidate] = []
    rejected: list[BoundaryCandidate] = []
    for candidate in raw:
        if candidate.page_label_suffix:
            reason = "running_header" if candidate.signature in running_headers else "page_label_suffix"
            rejected.append(BoundaryCandidate(**{**candidate.__dict__, "rejection": reason}))
            continue
        previous = first_page.setdefault(candidate.signature, candidate.page)
        if candidate.page != previous:
            rejected.append(BoundaryCandidate(**{**candidate.__dict__, "rejection": "repeated_page_start_heading"}))
        elif candidate.score < 0.70:
            rejected.append(BoundaryCandidate(**{**candidate.__dict__, "rejection": "insufficient_boundary_evidence"}))
        else:
            accepted.append(candidate)
    return ContentBoundaryDetection(tuple(accepted), tuple(rejected), running_headers)


def _heading_candidates(pages: list[PdfPage], toc_titles: Iterable[str] = ()) -> dict[int, list[str]]:
    """Compatibility view of accepted content boundaries for existing callers."""
    detected = _content_boundary_candidates(pages, toc_titles)
    result: dict[int, list[str]] = {page.number: [] for page in pages}
    for candidate in detected.accepted:
        result[candidate.page].append(candidate.title)
    return result


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right)) * 0.35 + 0.65
    left_grams = {left[index : index + 2] for index in range(max(1, len(left) - 1))}
    right_grams = {right[index : index + 2] for index in range(max(1, len(right) - 1))}
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _page_number_anchors(pages: list[PdfPage]) -> list[tuple[int, int]]:
    """Collect only isolated edge labels; prose, ISBNs, and TOC rows never qualify."""
    anchors: list[tuple[int, int]] = []
    for page in pages:
        lines = _page_lines(page)
        for line in lines[:2] + lines[-2:]:
            printed = _page_label_value(line)
            if printed is not None and printed > 0:
                anchors.append((printed, page.number))
    return anchors


def _predicted_offset(pages: list[PdfPage]) -> tuple[int | None, dict[str, Any]]:
    """Return a validated, optional page-label offset used only as a soft bonus."""
    offsets = [physical - printed for printed, physical in _page_number_anchors(pages)]
    if len(offsets) < 3:
        return None, {"accepted": False, "reason": "fewer_than_three_page_labels", "anchors": len(offsets)}
    median = int(round(statistics.median(offsets)))
    agreeing = [offset for offset in offsets if abs(offset - median) <= 1]
    if len(agreeing) < 3 or len(agreeing) / len(offsets) < 0.60:
        return None, {
            "accepted": False,
            "reason": "page_label_offsets_do_not_converge",
            "anchors": len(offsets),
            "median": median,
        }
    return median, {"accepted": True, "offset": median, "anchors": len(offsets), "agreeing": len(agreeing)}


def _toc_candidates(
    entry: dict[str, Any],
    pages: list[PdfPage],
    boundaries: ContentBoundaryDetection,
    offset: int | None,
    toc_page_numbers: set[int],
) -> list[tuple[int, float, str]]:
    """Align a TOC title to independently plausible content boundaries."""
    target = normalise(str(entry["title"]))
    candidates: list[tuple[int, float, str]] = []
    by_page = {candidate.page: candidate for candidate in boundaries.accepted}
    for page in pages:
        if page.number in toc_page_numbers:
            continue
        boundary = by_page.get(page.number)
        if boundary is not None:
            score = _similarity(target, boundary.signature)
            if score >= 0.45:
                if offset is not None:
                    predicted = int(entry["printed_page"]) + offset
                    score += max(0.0, 0.08 - abs(page.number - predicted) * 0.003)
                candidates.append((page.number, min(score, 1.0), "content_boundary"))
                continue
        # Ordinary body-text agreement is deliberately weak: it can disambiguate
        # an endpoint but cannot outrank a page-start title candidate.
        score = _similarity(target, normalise(page.text))
        if score >= 0.78:
            candidates.append((page.number, min(score * 0.72, 0.72), "page_text"))
    return sorted(candidates, key=lambda item: (-item[1], item[0]))[:12]


def _monotonic_alignment(entries: list[dict[str, Any]], candidates: list[list[tuple[int, float, str]]]) -> list[tuple[int, float, str] | None]:
    """Choose a maximum-score strictly monotonic sequence of candidate pages."""
    states: dict[int, tuple[float, list[tuple[int, float, str] | None]]] = {0: (0.0, [])}
    for entry_candidates in candidates:
        next_states: dict[int, tuple[float, list[tuple[int, float, str] | None]]] = {}
        for last_page, (score, selected) in states.items():
            skipped = (score - 0.15, [*selected, None])
            current = next_states.get(last_page)
            if current is None or skipped[0] > current[0]:
                next_states[last_page] = skipped
            for candidate in entry_candidates:
                page, candidate_score, _ = candidate
                if page <= last_page:
                    continue
                proposed = (score + candidate_score, [*selected, candidate])
                current = next_states.get(page)
                if current is None or proposed[0] > current[0]:
                    next_states[page] = proposed
        states = next_states or states
    return max(states.values(), key=lambda item: item[0])[1] if states else [None] * len(entries)


def _infer_first_front_matter_start(
    entries: list[dict[str, Any]],
    matches: list[tuple[int, float, str] | None],
    pages: list[PdfPage],
    toc_pages: list[PdfPage],
) -> tuple[int, float, str] | None:
    """Map an unlabelled first introduction to the first prose page after a TOC.

    This is deliberately limited to the first TOC item and established front-matter
    markers. It never fabricates a numbered chapter from page numbers alone.
    """
    if not entries or not matches or matches[0] is not None:
        return None
    if _marker_kind(str(entries[0]["title"])) != "front_matter":
        return None
    next_verified = next((match[0] for match in matches[1:] if match is not None), None)
    if next_verified is None:
        return None
    first_page = max((page.number for page in toc_pages), default=0) + 1
    for page in pages:
        if not first_page <= page.number < next_verified:
            continue
        lines = _page_lines(page)
        if any(len(normalise(line)) >= 16 for line in lines[:6]):
            return page.number, 0.74, "inferred_first_front_matter"
    return None


def _outline_from_units(units: list[SourceUnit]) -> list[SourceNode]:
    roots: list[SourceNode] = []
    current_part: SourceNode | None = None
    for unit in units:
        node = SourceNode(unit.id, unit.title, unit.kind, unit.breadcrumb, unit.confidence, [unit.id])
        if unit.kind == "part":
            roots.append(node)
            current_part = node
            continue
        if current_part is not None and unit.kind in {"chapter", "section"}:
            unit.parent_id = current_part.id
            node.breadcrumb = (*current_part.breadcrumb, unit.title)
            unit.breadcrumb = node.breadcrumb
            current_part.children.append(node)
        else:
            roots.append(node)
    return roots


def _units_from_mapped_toc(entries: list[dict[str, Any]], selected: list[tuple[int, float, str] | None], page_count: int) -> list[SourceUnit]:
    mapped = [(entry, candidate) for entry, candidate in zip(entries, selected) if candidate is not None]
    units: list[SourceUnit] = []
    if not mapped:
        return units
    first_start = mapped[0][1][0]
    if first_start > 1:
        units.append(SourceUnit("unit-01", "前置材料", "front_matter", ("前置材料",), 1, first_start - 1, 0.75, {"derived": "before_first_recognised_unit"}))
    number = len(units) + 1
    for index, (entry, candidate) in enumerate(mapped):
        start, score, match_source = candidate
        end = mapped[index + 1][1][0] - 1 if index + 1 < len(mapped) else page_count
        if end < start:
            continue
        title = str(entry["title"])
        units.append(SourceUnit(
            f"unit-{number:02d}", title, _kind(title), (title,), start, end, round(score, 4),
            {"toc_page": entry["toc_page"], "printed_page": entry["printed_page"], "matched_page": start, "match_source": match_source},
        ))
        number += 1
    return units


def _valid_bookmarks(bookmarks: list[dict[str, Any]], page_count: int) -> list[dict[str, Any]]:
    """Retain usable bookmarks without erasing their source hierarchy/order."""
    result: list[dict[str, Any]] = []
    previous_page = 0
    for raw in bookmarks:
        page = raw.get("page")
        title = _clean_space(str(raw.get("title", "")))
        level = raw.get("level", 1)
        if not isinstance(page, int) or not 1 <= page <= page_count or not _looks_like_title(title):
            continue
        if not isinstance(level, int) or level < 1 or page < previous_page:
            return []
        previous_page = page
        result.append({"title": title, "page": page, "level": level})
    return result


def _bookmark_leaf_indexes(bookmarks: list[dict[str, Any]]) -> list[int]:
    """A bookmark with descendants is a structural node, not duplicate prose."""
    leaves: list[int] = []
    for index, bookmark in enumerate(bookmarks):
        next_level = bookmarks[index + 1]["level"] if index + 1 < len(bookmarks) else 0
        if next_level <= bookmark["level"]:
            leaves.append(index)
    return leaves


def _units_from_bookmarks(bookmarks: list[dict[str, Any]], page_count: int) -> list[SourceUnit]:
    valid = _valid_bookmarks(bookmarks, page_count)
    leaf_indexes = _bookmark_leaf_indexes(valid)
    if len(leaf_indexes) < 2:
        return []
    units: list[SourceUnit] = []
    first_start = int(valid[leaf_indexes[0]]["page"])
    if first_start > 1:
        units.append(SourceUnit(
            "unit-01", "前置材料", "front_matter", ("前置材料",), 1, first_start - 1, 0.75,
            {"derived": "before_first_bookmark_leaf"},
        ))
    for source_index in leaf_indexes:
        bookmark = valid[source_index]
        start = int(bookmark["page"])
        next_start = next((int(next_bookmark["page"]) for next_bookmark in valid[source_index + 1:] if int(next_bookmark["page"]) > start), page_count + 1)
        end = next_start - 1
        if end < start:
            continue
        title = str(bookmark["title"])
        units.append(SourceUnit(
            f"unit-{len(units) + 1:02d}", title, _kind(title), (title,), start, end, 0.92,
            {"bookmark_level": int(bookmark["level"]), "bookmark_page": start},
        ))
    return units if len(units) >= 2 else []


def _outline_from_bookmarks(bookmarks: list[dict[str, Any]], units: list[SourceUnit], page_count: int) -> list[SourceNode]:
    """Build the nested native bookmark tree and attach only leaf reading units."""
    valid = _valid_bookmarks(bookmarks, page_count)
    leaf_indexes = set(_bookmark_leaf_indexes(valid))
    unit_by_bookmark_page_and_title = {
        (unit.physical_page_start, normalise(unit.title)): unit
        for unit in units
        if unit.evidence.get("bookmark_page")
    }
    roots: list[SourceNode] = []
    stack: list[tuple[int, SourceNode]] = []
    for index, bookmark in enumerate(valid):
        level = int(bookmark["level"])
        while stack and stack[-1][0] >= level:
            stack.pop()
        parents = [node for _, node in stack]
        title = str(bookmark["title"])
        node = SourceNode(
            id=f"bookmark-{index + 1:03d}",
            title=title,
            kind=_kind(title),
            breadcrumb=tuple([*(parent.title for parent in parents), title]),
            confidence=0.92,
        )
        if index in leaf_indexes:
            unit = unit_by_bookmark_page_and_title.get((int(bookmark["page"]), normalise(title)))
            if unit is not None:
                unit.parent_id = stack[-1][1].id if stack else None
                unit.breadcrumb = node.breadcrumb
                node.unit_ids.append(unit.id)
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((level, node))
    front = next((unit for unit in units if unit.kind == "front_matter" and unit.evidence.get("derived") == "before_first_bookmark_leaf"), None)
    if front is not None:
        roots.insert(0, SourceNode(front.id, front.title, front.kind, front.breadcrumb, front.confidence, [front.id]))
    return roots


def _units_from_headings(pages: list[PdfPage], detected: ContentBoundaryDetection) -> list[SourceUnit]:
    starts = list(detected.accepted)
    numbered = [candidate for candidate in starts if _structural_heading(candidate.title)]
    endpoints = [candidate for candidate in starts if candidate.kind in {"front_matter", "back_matter"}]
    # A content-only outline needs a numbered sequence, or a numbered spine with a
    # semantic endpoint. This prevents ordinary short prose lines becoming units.
    if len(numbered) < 2:
        return []
    units: list[SourceUnit] = []
    first_start = starts[0].page
    if first_start > 1:
        units.append(SourceUnit(
            "unit-01", "前置材料", "front_matter", ("前置材料",), 1, first_start - 1, 0.75,
            {"derived": "before_first_content_boundary"},
        ))
    for index, candidate in enumerate(starts):
        end = starts[index + 1].page - 1 if index + 1 < len(starts) else len(pages)
        units.append(SourceUnit(
            f"unit-{len(units) + 1:02d}",
            candidate.title,
            candidate.kind,
            (candidate.title,),
            candidate.page,
            end,
            round(candidate.score, 4),
            {
                "heading_page": candidate.page,
                "source": candidate.source,
                "signature": candidate.signature,
                "body_after": candidate.body_after,
                "layout_backed": candidate.layout_backed,
            },
        ))
    return units


def fallback_units(page_count: int, pages_per_chapter: int | None = None, chapter_count: int | None = None) -> list[SourceUnit]:
    if pages_per_chapter:
        size = pages_per_chapter
    elif chapter_count:
        size = max(1, (page_count + chapter_count - 1) // chapter_count)
    else:
        size = page_count
    result = []
    for index, start in enumerate(range(1, page_count + 1, size), 1):
        end = min(page_count, start + size - 1)
        title = f"未识别 PDF 页段：pp. {start}-{end}"
        result.append(SourceUnit(f"unit-{index:02d}", title, "range_fallback", (title,), start, end, 0.0, {"fallback": True}))
    return result


def analyse_pdf(
    pages: list[PdfPage],
    source: Path,
    *,
    explicit_title: str | None = None,
    bookmarks: list[dict[str, Any]] | None = None,
    mode: str = "auto",
    min_confidence: float = 0.72,
    pages_per_chapter: int | None = None,
    chapter_count: int | None = None,
) -> SourceAnalysis:
    """Derive a source outline from text/layout, never OCRing or guessing silently."""
    if not pages:
        raise ValueError("PDF has no pages")
    title = recognise_book_title(pages, source, explicit_title)
    report: dict[str, Any] = {
        "schema_version": 2,
        "detector": "content-boundary-v1",
        "title": {"value": title.value, "method": title.method, "confidence": title.confidence, "candidates": list(title.candidates)},
        "mode": mode,
        "ocr": "disabled",
        "methods": [],
    }
    selected: list[SourceUnit] = []
    selected_method = "ranges"
    selected_confidence = 0.0
    bookmark_outline: list[SourceNode] | None = None

    if mode in {"auto", "bookmarks"} and bookmarks:
        valid_bookmarks = _valid_bookmarks(bookmarks, len(pages))
        bookmarked = _units_from_bookmarks(valid_bookmarks, len(pages))
        confidence = sum(unit.confidence for unit in bookmarked) / len(bookmarked) if bookmarked else 0.0
        accepted = bool(bookmarked and confidence >= max(min_confidence, 0.85))
        report["methods"].append({
            "method": "bookmarks",
            "bookmarks": len(valid_bookmarks),
            "units": len(bookmarked),
            "confidence": confidence,
            "accepted": accepted,
        })
        if accepted:
            selected, selected_method, selected_confidence = bookmarked, "bookmarks", confidence
            bookmark_outline = _outline_from_bookmarks(valid_bookmarks, bookmarked, len(pages))

    toc_pages = _toc_pages(pages)
    entries = parse_printed_toc(toc_pages) if mode in {"auto", "toc"} else []
    detected = _content_boundary_candidates(pages, [str(entry["title"]) for entry in entries])
    report["content_boundaries"] = {
        "accepted": [asdict(candidate) for candidate in detected.accepted],
        "rejected": [asdict(candidate) for candidate in detected.rejected[:80]],
        "rejected_count": len(detected.rejected),
        "running_headers": {signature: list(page_numbers) for signature, page_numbers in detected.running_headers.items()},
    }

    if not selected and mode in {"auto", "toc"}:
        offset, offset_report = _predicted_offset(pages)
        candidates = [
            _toc_candidates(entry, pages, detected, offset, {page.number for page in toc_pages})
            for entry in entries
        ]
        matches = _monotonic_alignment(entries, candidates) if entries else []
        inferred_front = _infer_first_front_matter_start(entries, matches, pages, toc_pages)
        if inferred_front is not None:
            matches[0] = inferred_front
        mapped = [match for match in matches if match is not None]
        confidence = sum(match[1] for match in mapped) / len(mapped) if mapped else 0.0
        accepted = len(entries) >= 2 and len(mapped) / len(entries) >= 0.7 and confidence >= max(min_confidence, 0.78)
        report["methods"].append({
            "method": "toc",
            "toc_pages": [page.number for page in toc_pages],
            "entries": entries,
            "page_offset": offset,
            "page_offset_evidence": offset_report,
            "candidates": [
                [{"physical_page": page, "confidence": round(score, 4), "source": source} for page, score, source in entry_candidates]
                for entry_candidates in candidates
            ],
            "matches": [None if match is None else {"physical_page": match[0], "confidence": round(match[1], 4), "source": match[2]} for match in matches],
            "confidence": round(confidence, 4),
            "accepted": accepted,
        })
        if accepted:
            selected, selected_method, selected_confidence = _units_from_mapped_toc(entries, matches, len(pages)), "toc", confidence

    if not selected and mode in {"auto", "headings"}:
        heading_units = _units_from_headings(pages, detected)
        confidence = sum(unit.confidence for unit in heading_units) / len(heading_units) if heading_units else 0.0
        accepted = len(heading_units) >= 2 and confidence >= max(min_confidence, 0.75)
        report["methods"].append({
            "method": "headings",
            "units": len(heading_units),
            "confidence": round(confidence, 4),
            "accepted": accepted,
        })
        if accepted:
            selected, selected_method, selected_confidence = heading_units, "headings", confidence

    if not selected:
        selected = fallback_units(len(pages), pages_per_chapter, chapter_count)
        selected_method = "ranges"
        report["methods"].append({"method": "ranges", "units": len(selected), "confidence": 0.0, "accepted": True})

    outline = bookmark_outline if selected_method == "bookmarks" and bookmark_outline is not None else _outline_from_units(selected)
    report.update({
        "selected_method": selected_method,
        "confidence": round(selected_confidence, 4),
        "units": [source_unit_dict(unit) for unit in selected],
        "outline": [source_node_dict(node) for node in outline],
    })
    return SourceAnalysis(title, selected_method, selected_confidence, selected, outline, report)


def _split_text(text: str, budget: int) -> list[str]:
    """Split without text loss, preferring paragraph then sentence boundaries."""
    if count_tokens(text) <= budget:
        return [text]
    pieces = [piece.strip() for piece in re.split(r"\n\s*\n", text) if piece.strip()]
    if len(pieces) <= 1:
        pieces = [piece.strip() for piece in re.split(r"(?<=[。！？.!?])\s*", text) if piece.strip()]
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
            # A single extracted line can exceed the hard ceiling. Use a binary
            # search so the fallback remains bounded for CJK tokenizers.
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


def split_unit_into_parts(unit: SourceUnit, pages: list[PdfPage], max_unit_tokens: int = DEFAULT_MAX_UNIT_TOKENS) -> list[ReadingPart]:
    """Create bounded reading parts while preserving every source character once."""
    if max_unit_tokens < 256:
        raise ValueError("max_unit_tokens must be at least 256")
    relevant = [page for page in pages if unit.physical_page_start <= page.number <= unit.physical_page_end]
    # Indexed chunks retain a small overlap and include import metadata. Reserve
    # enough room that all chunks for this part still fit the downstream input
    # budget; generation must never discard a trailing chunk.
    budget = max(128, int(max_unit_tokens * 0.70))
    fragments: list[tuple[int, str]] = []
    for page in relevant:
        # A recognized subheading is the safest natural split point within an
        # overlong original unit. Keep using page.text for the actual payload so
        # layout analysis cannot replace or omit native extracted characters.
        blocks = page.blocks or tuple(PageBlock(line) for line in _page_lines(page))
        heading_keys = {
            normalise(block.text)
            for block in blocks
            if _looks_like_title(block.text)
            and (_kind(block.text) in {"part", "chapter", "section"} or block.size >= 16)
        }
        segments: list[str] = []
        current: list[str] = []
        for line in page.text.splitlines():
            if current and normalise(line) in heading_keys:
                segments.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            segments.append("\n".join(current))
        for segment in segments or [page.text]:
            for fragment in _split_text(segment, budget):
                if fragment:
                    fragments.append((page.number, fragment))
    groups: list[list[tuple[int, str]]] = []
    pending: list[tuple[int, str]] = []
    used = 0
    for page_number, fragment in fragments:
        amount = count_tokens(fragment)
        if pending and used + amount > budget:
            groups.append(pending)
            pending, used = [], 0
        pending.append((page_number, fragment))
        used += amount
    if pending:
        groups.append(pending)
    if not groups:
        groups = [[(unit.physical_page_start, "")]]
    count = len(groups)
    result: list[ReadingPart] = []
    for index, group in enumerate(groups, 1):
        title = unit.title if count == 1 else f"{unit.title}（第 {index} 部分）"
        result.append(ReadingPart(
            unit.id, title, unit.kind, unit.breadcrumb, unit.confidence, index, count,
            group[0][0], group[-1][0], tuple(group),
        ))
    return result


def source_unit_dict(unit: SourceUnit) -> dict[str, Any]:
    value = asdict(unit)
    value["breadcrumb"] = list(unit.breadcrumb)
    return value


def source_node_dict(node: SourceNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "title": node.title,
        "kind": node.kind,
        "breadcrumb": list(node.breadcrumb),
        "confidence": node.confidence,
        "unitIds": list(node.unit_ids),
        "children": [source_node_dict(child) for child in node.children],
    }


def reading_part_dict(part: ReadingPart, chapter: int, raw_path: str) -> dict[str, Any]:
    return {
        "chapter": chapter,
        "source_unit_id": part.source_unit_id,
        "title": part.title,
        "source_title": part.title if part.part_count == 1 else re.sub(r"（第 \d+ 部分）$", "", part.title),
        "kind": part.kind,
        "breadcrumb": list(part.breadcrumb),
        "confidence": part.confidence,
        "part": part.part,
        "part_count": part.part_count,
        "physical_page_start": part.physical_page_start,
        "physical_page_end": part.physical_page_end,
        "raw_path": raw_path,
    }
