"""Deterministic source-title and source-structure analysis for imported books.

The PDF path deliberately works from text and layout information that already exists in
PDF files.  It does not invoke OCR: a PDF without useful extractable text is reported as
unrecognised and falls back to explicitly-labelled page ranges.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
import re
import statistics
import unicodedata
from typing import Any, Iterable

from .chunking import count_tokens
from .pdf_manifest import manifest_digest, validate_manifest


DEFAULT_MAX_UNIT_TOKENS = 12000


@dataclass(frozen=True)
class PdfSpan:
    """A native PDF text span with the geometry needed for deterministic cleanup."""

    text: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    size: float = 0.0
    flags: int = 0
    font: str = ""


@dataclass(frozen=True)
class PdfLine:
    """A native PDF text line. ``text`` is always derived from ``spans`` when present."""

    text: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    size: float = 0.0
    spans: tuple[PdfSpan, ...] = ()


@dataclass(frozen=True)
class PageBlock:
    text: str
    y0: float = 0.0
    y1: float = 0.0
    size: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    lines: tuple[PdfLine, ...] = ()


@dataclass(frozen=True)
class PdfPage:
    number: int
    text: str
    blocks: tuple[PageBlock, ...] = ()
    height: float = 0.0
    width: float = 0.0
    lines: tuple[PdfLine, ...] = ()


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
    level: int = 1


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
    header_clusters: dict[str, tuple[int, ...]]


TOC_MARKERS = ("目录", "目錄", "contents", "table of contents")
FRONT_MATTER_MARKERS = ("序", "前言", "推荐序", "推薦序", "译序", "譯序", "引言", "导言", "導言", "鸣谢", "鳴謝")
END_MARKERS = ("结语", "結語", "结论", "結論", "后记", "後記", "译后记", "譯後記", "附录", "附錄", "参考文献", "參考文獻", "索引")
TITLE_NOISE = (
    "isbn", "版权所有", "版次", "印刷", "出版社", "责任编辑", "責任編輯", "译者", "譯者", "作者",
    "copyright", "all rights", "价格", "定价", "定價", "translated by", "translation by",
    "with analysis", "university press", "press", "foreword by",
)


def _clean_space(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).replace(" ", " ").split())


def _normalised_marker(value: str) -> str:
    """Normalize TOC markers while retaining ordinary text boundaries."""
    return "".join(unicodedata.normalize("NFKC", value).lower().split())


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


def _looks_like_structural_title(value: str) -> bool:
    value = _clean_space(value)
    if re.search(
        r"^(?:第?\s*[一二三四五六七八九十百千万0-9]+\s*(?:部分|部|章|节|節)|"
        r"[一二三四五六七八九十百千万0-9]+\s*[、.]|"
        r"chapter\s+\d+|section\s+\d+(?:\.\d+)*|part\s+\d+)",
        value,
        flags=re.IGNORECASE,
    ):
        return True
    compact = normalise(value)
    return compact in {normalise(marker) for marker in (*FRONT_MATTER_MARKERS, *END_MARKERS)}


def _looks_like_title_noise(value: str) -> bool:
    value = _clean_space(value)
    compact = normalise(value)
    if re.fullmatch(r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日?", compact):
        return True
    if re.fullmatch(r"(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2}", value):
        return True
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}", value):
        return True
    if re.search(r"(?:仅供|只供).{0,12}(?:参考|学习|传阅|交流|使用)", value):
        return True
    if re.search(r"内部.{0,8}(?:参考|学习|资料|传阅|交流|使用|流通)", value):
        return True
    if re.search(r"(?:禁止|不得).{0,8}(?:传播|销售|商用|传阅|流通)", value):
        return True
    return False


def _looks_like_prose_fragment(value: str) -> bool:
    value = _clean_space(value)
    if len(value) < 18 or _looks_like_structural_title(value):
        return False
    clause_marks = len(re.findall(r"[，,。；;？！!?]", value))
    if clause_marks >= 2:
        return True
    if clause_marks >= 1 and len(value) >= 24:
        return True
    if re.search(r"《[^》]{2,}》", value):
        outside = re.sub(r"《[^》]{2,}》", "", value)
        if len(normalise(outside)) >= 10 and re.search(r"(?:但是|但其|却是|因此|所以|然而|因为|由于|如果|那么|同时|并且|如今|最早出版于|系统)", outside):
            return True
    if len(value) >= 26 and re.search(r"(?:但是|但其|却是|因此|所以|然而|因为|由于|如果|那么|同时|并且|如今|最早出版于|系统)", value):
        return True
    return False


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
    if _looks_like_title_noise(value):
        return False
    # Author credit lines are usually not book titles. This also excludes common
    # Western bylines even when a damaged extraction has separated the title.
    if re.search(r"(?:著|编|編|译|譯|主编|主編)\s*$", value):
        return False
    if re.match(r"^(?:by|edited by|translated by|introduction by|foreword by)\b", value, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(?:by|author|editor|translator)\b", value, flags=re.IGNORECASE) and len(value.split()) <= 10:
        return False
    if _looks_like_prose_fragment(value):
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
    if filename in compact and compact != filename:
        filename_coverage = len(filename) / max(1, len(compact))
        if _looks_like_prose_fragment(value):
            score -= 0.45
        elif len(compact) >= len(filename) + 12 and filename_coverage < 0.55:
            score -= 0.20
    if re.search(r"[一-鿿]", value) and 5 <= len(compact) <= 16 and not _looks_like_structural_title(value) and not re.search(r"[，,。；;？！!?:：]", value):
        score += 0.25
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
    if re.search(r"(?:第?[一二三四五六七八九十百千万0-9]+(?:章|节|節)|[一二三四五六七八九十百千万]+\s*[、.]|\d+(?:\.\d+)*\s*[.、]|chapter\s+\d+|section\s+\d+(?:\.\d+)?)", title, flags=re.IGNORECASE):
        return "chapter"
    return "section"


_TOC_PAGE_TOKEN = r"[0-9OolIiS〇<>()[\]{}（）_.,:：;；\-—–]"
_PAGE_LABEL_CHARS = r"[0-9OolIiS〇<>()[\]{}（）_.,:：;；\-—–\s]"


def _toc_page_evidence(page: PdfPage) -> bool:
    """Return whether a page plausibly continues a nearby printed TOC."""
    lines = _page_lines(page)
    if not lines:
        return False
    page_labels = sum(_page_label_value(line) is not None for line in lines)
    slash_rows = sum(
        bool(re.search(rf"/\s*{_PAGE_LABEL_CHARS}{{1,12}}(?=\s|/|$)", line))
        for line in lines
    )
    title_lines = sum(_looks_like_title(line) for line in lines[:12])
    # A continuation page normally has either isolated page labels beside short
    # titles, or at least one slash-delimited row. Body prose alone is not enough.
    return slash_rows >= 1 or (page_labels >= 1 and title_lines >= 1)


def _toc_pages(pages: list[PdfPage], markers: Iterable[str] | None = None) -> list[PdfPage]:
    """Find the first TOC and its contiguous continuation pages."""
    window = pages[: max(20, min(120, len(pages) // 6))]
    marker_keys = {_normalised_marker(marker) for marker in (markers or TOC_MARKERS)}
    start = next((index for index, page in enumerate(window) if any(_normalised_marker(line) in marker_keys for line in _page_lines(page))), None)
    if start is None:
        return []
    selected = [window[start]]
    index = start + 1
    while index < len(window) and _toc_page_evidence(window[index]):
        selected.append(window[index])
        index += 1
    return selected


def _page_label_value(value: str) -> int | None:
    """Return a short, OCR-tolerant printed page number and reject arbitrary text."""
    translated = _clean_space(value).translate(str.maketrans({
        "O": "0", "o": "0", "〇": "0", "I": "1", "i": "1", "l": "1", "S": "5",
    }))
    # Some embedded fonts turn the zero glyph in a page label into ``(>`` or a
    # similar paired bracket. Only repair that narrow shape in an otherwise label.
    translated = re.sub(r"(?:\(\s*>|<\s*\)|\[\s*>|>\s*\])", "0", translated)
    compact = re.sub(r"[\s\[\]{}()（）<>_.,:：;；\-—–]", "", translated)
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
        r"^\s*(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)|[一二三四五六七八九十百千万]+[、.]|\d+(?:\.\d+)*[.、]|chapter\s+\d+|part\s+\d+|section\s+\d+(?:\.\d+)*)",
        value,
        re.IGNORECASE,
    ))


def _structural_level(value: str) -> int:
    """Return an explicit, conservative outline level for a recognised title."""
    text = _clean_space(value)
    if re.match(r"^\s*(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部)|part\s+\d+)", text, re.IGNORECASE):
        return 1
    if re.match(r"^\s*(?:第?[一二三四五六七八九十百千万0-9]+章|chapter\s+\d+|[一二三四五六七八九十百千万]+[、.])", text, re.IGNORECASE):
        return 2
    if re.match(r"^\s*(?:第?[一二三四五六七八九十百千万0-9]+(?:节|節)|section\s+\d+)", text, re.IGNORECASE):
        return 3
    match = re.match(r"^\s*\d+(?:\.(\d+))+(?:[.、]|\s)", text)
    if match:
        return text.split()[0].rstrip(".、").count(".") + 1
    if re.match(r"^\s*\d+[.、]", text):
        return 1
    return 1


def _header_signature(value: str) -> tuple[str, bool]:
    """Normalise a title-shaped page header without altering persisted titles."""
    text = _clean_space(value)
    suffix = re.search(r"[\s_\[\]{}()（）0-9Oo〇IlIiSs.,:：;；\-—–]{2,6}$", text)
    if suffix:
        prefix = text[:suffix.start()].strip()
        if prefix and (_structural_heading(prefix) or _marker_kind(prefix)):
            return normalise(prefix), True
    return normalise(text), False


def _title_alignment_signature(value: str) -> str:
    """Normalise title presentation noise only for TOC/body alignment."""
    text = _clean_space(value)
    text = re.sub(
        r"^\s*(?:"
        r"第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)"
        r"|[一二三四五六七八九十百千万]+[、.]"
        r"|\d+(?:\.\d+)*[.、]?"
        r")\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.translate(str.maketrans({
        "“": "", "”": "", "‘": "", "’": "", "\"": "", "'": "",
        "—": "", "–": "", "-": "", "_": "", "·": "",
    }))
    return normalise(text)


def _leading_structural_ordinal(value: str) -> str | None:
    """Return a chapter ordinal used to keep header clusters distinct."""
    match = re.match(
        r"^\s*("
        r"第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)"
        r"|[一二三四五六七八九十百千万]+[、.]"
        r"|\d+(?:\.\d+)*[.、]?"
        r")",
        _clean_space(value),
        re.IGNORECASE,
    )
    return normalise(match.group(1)) if match else None


def _toc_title_score(target: str, candidate: str) -> float:
    """Score a TOC/body title pair without broadening global text normalisation."""
    exact_target = normalise(target)
    exact_candidate = normalise(candidate)
    if exact_target and exact_target == exact_candidate:
        return 1.0
    aligned_target = _title_alignment_signature(target)
    aligned_candidate = _title_alignment_signature(candidate)
    if not aligned_target or not aligned_candidate:
        return 0.0
    if aligned_target == aligned_candidate:
        return 0.96
    if min(len(aligned_target), len(aligned_candidate)) < 6:
        return 0.0
    return SequenceMatcher(None, aligned_target, aligned_candidate).ratio()


def _same_header_cluster(left: BoundaryCandidate, right: BoundaryCandidate) -> bool:
    """Cluster only later OCR variants of the same explicitly numbered heading."""
    left_ordinal = _leading_structural_ordinal(left.title)
    right_ordinal = _leading_structural_ordinal(right.title)
    if not left_ordinal or left_ordinal != right_ordinal:
        return False
    left_title = _title_alignment_signature(left.title)
    right_title = _title_alignment_signature(right.title)
    return bool(left_title and right_title and SequenceMatcher(None, left_title, right_title).ratio() >= 0.82)


def parse_printed_toc(pages: Iterable[PdfPage], markers: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Parse same-line and alternating native-text contents rows deterministically."""
    entries: list[dict[str, Any]] = []
    pending_title: str | None = None
    pending_page: int | None = None
    pending_page_line: int | None = None
    marker_keys = {_normalised_marker(marker) for marker in (markers or TOC_MARKERS)}
    page_token = rf"(?:{_PAGE_LABEL_CHARS}{{1,12}})"
    row = re.compile(rf"^(?P<title>.+?)(?:\s*(?:[.·…]{{2,}}|/)\s*|\s{{2,}})(?P<page>{page_token})\s*$")
    numbered_row = re.compile(
        rf"^(?P<title>(?:第?[一二三四五六七八九十百千万0-9]+(?:部分|部|章|节|節)|chapter\s+\d+|part\s+\d+).+?)\s+(?P<page>{page_token})\s*$",
        re.IGNORECASE,
    )

    def compact_rows(line: str) -> list[tuple[str, int, str]]:
        """Parse dense rows whose extraction lost column spacing."""
        rows: list[tuple[str, int, str]] = []
        dotted = re.compile(r"(?P<title>.{2,80}?)[.·…]{2,}\s*[（(]?\s*(?P<page>[0-9OolIiS〇<>]{1,4})\s*[）)]?")
        for match in dotted.finditer(line):
            printed = _parse_page_number(match.group("page"))
            title = match.group("title").strip(" .·…\\n")
            if printed is not None and _looks_like_title(title):
                rows.append((title, printed, "dense_dotted_parenthesized"))
        if rows:
            return rows
        page_first = re.compile(r"(?P<page>[0-9OolIiS〇<>]{1,4})\s*/\s*(?P<title>[^/]{2,80}?)(?=\s+[0-9OolIiS〇<>]{1,4}\s*/|$)")
        page_first_matches = list(page_first.finditer(line))
        if page_first_matches and page_first_matches[0].start() == 0:
            for match in page_first_matches:
                printed = _parse_page_number(match.group("page"))
                title = re.sub(r"^[1-9]\d?\s+(?=[^0-9])", "", match.group("title").strip())
                if printed is not None and _looks_like_title(title):
                    rows.append((title, printed, "page_title_slash_stream"))
            if rows:
                return rows

        title_page = re.compile(
            r"(?P<title>(?:第?[一二三四五六七八九十百千万0-9]+\s*)?[一-鿿][^/\n]{1,80}?)"
            r"\s*/\s*(?P<page>[0-9OolIiS〇<>]{1,4})"
            r"(?=\s+(?:(?:第?[一二三四五六七八九十百千万0-9]+\s*)?[一-鿿])|$)"
        )
        for match in title_page.finditer(line):
            title = match.group("title").strip()
            printed = _parse_page_number(match.group("page"))
            if printed is not None and _looks_like_title(title):
                rows.append((title, printed, "title_page_slash_stream"))
        return rows

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
        for line_number, raw in enumerate(_page_lines(page), 1):
            line = _clean_space(raw)
            if _normalised_marker(line) in marker_keys:
                pending_title = None
                pending_page = None
                pending_page_line = None
                continue
            line = re.sub(r"^目\s*录\s*", "", line)
            compact = compact_rows(line)
            if compact:
                emitted = False
                for title, printed, style in compact:
                    emitted = append(title, printed, page.number, line_number, style) or emitted
                if emitted:
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
    """Check that a candidate transitions into prose on this or a nearby page."""
    probes: list[str] = _page_lines(pages[page_index])[line_index + 1:]
    if not probes:
        for next_index in range(page_index + 1, min(len(pages), page_index + 3)):
            probes.extend(_page_lines(pages[next_index])[:4])
    for line in probes[:10]:
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
    toc_targets = [str(title) for title in toc_titles if normalise(title)]
    targets = {normalise(title) for title in toc_targets}
    raw: list[BoundaryCandidate] = []
    marker_keys = {normalise(marker) for marker in TOC_MARKERS}
    for page_index, page in enumerate(pages):
        page_line_keys = {normalise(line) for line in _page_lines(page)}
        if page_line_keys & marker_keys:
            continue
        lines = _page_lines(page)
        line_options: list[tuple[int, str, int]] = []
        for line_index, line in enumerate(lines[:4]):
            line_options.append((line_index, _clean_space(line), line_index))
            if line_index + 1 >= min(len(lines), 4):
                continue
            first, second = lines[line_index], lines[line_index + 1]
            if len(normalise(first)) > 24 or len(normalise(second)) > 24:
                continue
            joined = _clean_space(f"{first} {second}")
            joined_signature = normalise(joined)
            if joined_signature in targets or _marker_kind(joined_signature):
                line_options.append((line_index, joined, line_index + 1))
        for line_index, title, consumed_index in line_options:
            if not _looks_like_title(title):
                continue
            if len(normalise(title)) < 2:
                continue
            signature, page_label_suffix = _header_signature(title)
            marker_kind = _marker_kind(signature)
            structural = _structural_heading(title) or _structural_heading(signature)
            toc_match_score = max((_toc_title_score(target, title) for target in toc_targets), default=0.0)
            matches_toc = bool(signature and signature in targets) or toc_match_score >= 0.88
            if not (structural or marker_kind or matches_toc):
                continue
            kind = marker_kind or (_kind(title) if structural else "section")
            body_after = _body_follows(pages, page_index, consumed_index)
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
    clustered_pages: dict[str, list[int]] = {}
    cluster_representatives: list[BoundaryCandidate] = []
    for candidate in raw:
        if candidate.page_label_suffix:
            reason = "running_header" if candidate.signature in running_headers else "page_label_suffix"
            rejected.append(BoundaryCandidate(**{**candidate.__dict__, "rejection": reason}))
            continue
        previous = first_page.setdefault(candidate.signature, candidate.page)
        if candidate.page != previous:
            rejected.append(BoundaryCandidate(**{**candidate.__dict__, "rejection": "repeated_page_start_heading"}))
            continue
        matching_cluster = next((representative for representative in cluster_representatives if _same_header_cluster(representative, candidate)), None)
        if matching_cluster is not None:
            cluster_key = f"{_leading_structural_ordinal(matching_cluster.title)}:{_title_alignment_signature(matching_cluster.title)}"
            clustered_pages.setdefault(cluster_key, [matching_cluster.page]).append(candidate.page)
            rejected.append(BoundaryCandidate(**{**candidate.__dict__, "rejection": "running_header"}))
        elif candidate.score < 0.70:
            rejected.append(BoundaryCandidate(**{**candidate.__dict__, "rejection": "insufficient_boundary_evidence"}))
        else:
            accepted.append(candidate)
            if _leading_structural_ordinal(candidate.title):
                cluster_representatives.append(candidate)
    header_clusters = {
        key: tuple(page_numbers)
        for key, page_numbers in clustered_pages.items()
        if len(page_numbers) > 1
    }
    return ContentBoundaryDetection(tuple(accepted), tuple(rejected), running_headers, header_clusters)


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


def _offset_from_matches(entries: list[dict[str, Any]], matches: list[tuple[int, float, str] | None]) -> tuple[int | None, dict[str, Any]]:
    """Infer a printed-to-physical offset from strong direct TOC matches."""
    anchors = [
        match[0] - int(entry["printed_page"])
        for entry, match in zip(entries, matches)
        if match is not None and match[2] in {"content_boundary", "page_text"} and match[1] >= 0.60
    ]
    if len(anchors) < 3:
        return None, {"accepted": False, "reason": "fewer_than_three_direct_matches", "anchors": len(anchors)}
    median = int(round(statistics.median(anchors)))
    agreeing = [value for value in anchors if abs(value - median) <= 1]
    accepted = len(agreeing) >= 3 and len(agreeing) / len(anchors) >= 0.60
    report = {"accepted": accepted, "offset": median, "anchors": len(anchors), "agreeing": len(agreeing)}
    if not accepted:
        report["reason"] = "direct_match_offsets_do_not_converge"
    return (median if accepted else None), report


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
            score = _toc_title_score(str(entry["title"]), boundary.title)
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
    """Build a bounded tree from explicit structural levels only."""
    roots: list[SourceNode] = []
    stack: list[tuple[int, SourceNode]] = []
    for unit in units:
        level = max(1, unit.level)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else None
        if parent is not None and parent.kind in {"front_matter", "back_matter", "range_fallback"}:
            parent = None
        breadcrumb = (*parent.breadcrumb, unit.title) if parent else (unit.title,)
        unit.parent_id = parent.id if parent else None
        unit.breadcrumb = breadcrumb
        node = SourceNode(unit.id, unit.title, unit.kind, breadcrumb, unit.confidence, [unit.id])
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
        stack.append((level, node))
    return roots


def _complete_toc_matches(
    entries: list[dict[str, Any]],
    matches: list[tuple[int, float, str] | None],
    boundaries: ContentBoundaryDetection,
) -> tuple[list[tuple[int, float, str] | None], list[dict[str, Any]]]:
    """Fill exact TOC title matches and semantic endpoints from detected boundaries."""
    completed = list(matches)
    actions: list[dict[str, Any]] = []
    by_signature = {candidate.signature: candidate for candidate in boundaries.accepted}
    for index, (entry, match) in enumerate(zip(entries, completed)):
        if match is not None:
            continue
        candidate = by_signature.get(normalise(str(entry["title"])))
        if candidate is None:
            continue
        completed[index] = (candidate.page, candidate.score, "content_boundary_completion")
        actions.append({"title": entry["title"], "physical_page": candidate.page, "source": "exact_content_boundary"})

    # A known concluding marker must never remain swallowed by the final TOC unit.
    # It may be absent from a damaged TOC, so append it only when it is a high-confidence
    # monotonic endpoint that is not already represented by a matched title.
    mapped_signatures = {normalise(str(entry["title"])) for entry, match in zip(entries, completed) if match is not None}
    for candidate in boundaries.accepted:
        if candidate.kind != "back_matter" or candidate.score < 0.70 or candidate.signature in mapped_signatures:
            continue
        previous = max((match[0] for match in completed if match is not None), default=0)
        if candidate.page > previous:
            entries.append({
                "title": candidate.title.replace(" ", "") if _marker_kind(candidate.signature) else candidate.title,
                "printed_page": None,
                "toc_page": None,
                "line": None,
                "parse_style": "content_boundary_completion",
            })
            completed.append((candidate.page, candidate.score, "content_boundary_completion"))
            actions.append({"title": candidate.title, "physical_page": candidate.page, "source": "semantic_endpoint"})
    return completed, actions


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
        evidence = {"matched_page": start, "match_source": match_source}
        if entry.get("toc_page") is not None:
            evidence["toc_page"] = entry["toc_page"]
        if entry.get("printed_page") is not None:
            evidence["printed_page"] = entry["printed_page"]
        units.append(SourceUnit(
            f"unit-{number:02d}",
            title,
            str(entry.get("kind") or _kind(title)),
            (title,),
            start,
            end,
            round(score, 4),
            evidence,
            level=int(entry.get("level") or _structural_level(title)),
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


def _bookmark_coverage(units: list[SourceUnit], page_count: int) -> dict[str, Any]:
    """Report whether leaf-derived bookmark units cover every physical PDF page.

    A parent bookmark can occupy a long page range while its only child points to
    an appendix near the end of a book.  Since only leaves become reading units,
    accepting that tree would silently omit the parent's intervening body pages.
    """
    expected = 1
    gaps: list[dict[str, int]] = []
    for unit in sorted(units, key=lambda item: (item.physical_page_start, item.physical_page_end)):
        if unit.physical_page_start > expected:
            gaps.append({"physical_page_start": expected, "physical_page_end": unit.physical_page_start - 1})
        expected = max(expected, unit.physical_page_end + 1)
    if expected <= page_count:
        gaps.append({"physical_page_start": expected, "physical_page_end": page_count})
    return {
        "complete": not gaps and bool(units),
        "gaps": gaps,
        "covered_units": len(units),
        "page_count": page_count,
    }


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
            level=_structural_level(candidate.title),
        ))
    return units


def _units_from_manifest(manifest: dict[str, Any]) -> list[SourceUnit]:
    """Build fully verified reading units from explicit physical manifest ranges."""
    units: list[SourceUnit] = []
    for index, entry in enumerate(manifest.get("units") or [], 1):
        title = str(entry["title"])
        units.append(SourceUnit(
            f"unit-{index:02d}",
            title,
            str(entry.get("kind", "chapter")),
            (title,),
            int(entry["start"]),
            int(entry["end"]),
            1.0,
            {"manifest_unit": index, "source": "manual"},
            level=int(entry.get("level", 1)),
        ))
    return units


def _apply_manifest_toc_hints(entries: list[dict[str, Any]], manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Add verified TOC anchors without treating them as physical-page boundaries."""
    if not manifest:
        return entries
    expected = list((manifest.get("toc") or {}).get("entries") or [])
    if not expected:
        return entries
    result = [dict(entry) for entry in entries]
    for hint in expected:
        titles = {normalise(str(hint["title"]))}
        titles.update(normalise(str(alias)) for alias in hint.get("aliases", []))
        existing = next((entry for entry in result if normalise(str(entry["title"])) in titles), None)
        if existing is not None:
            existing["title"] = str(hint["title"])
            existing["level"] = int(hint.get("level", 1))
            if hint.get("kind"):
                existing["kind"] = hint["kind"]
            existing["manifest_hint"] = True
            continue
        result.append({
            "title": str(hint["title"]),
            "printed_page": hint.get("printed_page"),
            "toc_page": None,
            "line": None,
            "parse_style": "manifest_anchor",
            "level": int(hint.get("level", 1)),
            "kind": hint.get("kind"),
            "manifest_hint": True,
        })
    result.sort(key=lambda entry: (int(entry.get("printed_page") or 0), str(entry["title"])))
    return result


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
    structure_manifest: dict[str, Any] | None = None,
) -> SourceAnalysis:
    """Derive a source outline from text/layout, never OCRing or guessing silently."""
    if not pages:
        raise ValueError("PDF has no pages")
    manifest = validate_manifest(structure_manifest, source=source, page_count=len(pages)) if structure_manifest else None
    manifest_info = None
    if manifest is not None:
        manifest_info = {
            "id": manifest["id"],
            "status": manifest.get("status", "ready"),
            "hash": manifest_digest(manifest),
            "source_match": {"filename": True, "page_count": True, "sha256": "not_checked"},
            "application_mode": "explicit_units" if manifest.get("units") else "rules_only",
        }
    title_override = manifest.get("title_override") if manifest else None
    title = recognise_book_title(pages, source, title_override or explicit_title)
    report: dict[str, Any] = {
        "schema_version": 2,
        "detector": "content-boundary-v1",
        "title": {"value": title.value, "method": title.method, "confidence": title.confidence, "candidates": list(title.candidates)},
        "mode": mode,
        "ocr": "disabled",
        "methods": [],
    }
    if manifest_info is not None:
        report["structure_manifest"] = manifest_info
    selected: list[SourceUnit] = []
    selected_method = "ranges"
    selected_confidence = 0.0
    bookmark_outline: list[SourceNode] | None = None
    bookmark_rejected_for_coverage = False

    if manifest is not None and manifest.get("units"):
        selected = _units_from_manifest(manifest)
        selected_method = "manifest"
        selected_confidence = 1.0
        report["methods"].append({"method": "manifest", "units": len(selected), "confidence": 1.0, "accepted": True})

    if not selected and mode in {"auto", "bookmarks"} and bookmarks:
        valid_bookmarks = _valid_bookmarks(bookmarks, len(pages))
        bookmarked = _units_from_bookmarks(valid_bookmarks, len(pages))
        coverage = _bookmark_coverage(bookmarked, len(pages))
        confidence = sum(unit.confidence for unit in bookmarked) / len(bookmarked) if bookmarked else 0.0
        accepted = bool(bookmarked and coverage["complete"] and confidence >= max(min_confidence, 0.85))
        report["methods"].append({
            "method": "bookmarks",
            "bookmarks": len(valid_bookmarks),
            "units": len(bookmarked),
            "confidence": confidence,
            "coverage": coverage,
            "accepted": accepted,
            "rejection": None if accepted else "incomplete_physical_page_coverage" if bookmarked and not coverage["complete"] else "insufficient_confidence",
        })
        if accepted:
            selected, selected_method, selected_confidence = bookmarked, "bookmarks", confidence
            bookmark_outline = _outline_from_bookmarks(valid_bookmarks, bookmarked, len(pages))
        bookmark_rejected_for_coverage = bool(bookmarked and not coverage["complete"])

    toc_markers = list((manifest.get("toc") or {}).get("markers") or []) if manifest else []
    toc_pages = _toc_pages(pages, [*TOC_MARKERS, *toc_markers])
    entries = parse_printed_toc(toc_pages, [*TOC_MARKERS, *toc_markers]) if mode in {"auto", "toc"} else []
    entries = _apply_manifest_toc_hints(entries, manifest)
    printed_toc_entries = sum(entry.get("toc_page") is not None for entry in entries)
    genuine_toc_evidence = bool(toc_pages) and printed_toc_entries >= 2
    detected = _content_boundary_candidates(pages, [str(entry["title"]) for entry in entries])
    report["content_boundaries"] = {
        "accepted": [asdict(candidate) for candidate in detected.accepted],
        "rejected": [asdict(candidate) for candidate in detected.rejected[:80]],
        "rejected_count": len(detected.rejected),
        "running_headers": {signature: list(page_numbers) for signature, page_numbers in detected.running_headers.items()},
        "header_clusters": {signature: list(page_numbers) for signature, page_numbers in detected.header_clusters.items()},
    }

    # An incomplete bookmark tree must fall through to automatic sources even when
    # the caller requested bookmarks explicitly; returning it would omit pages.
    allow_toc = mode in {"auto", "toc"} or bookmark_rejected_for_coverage
    if not selected and allow_toc:
        offset, offset_report = _predicted_offset(pages)
        toc_page_numbers = {page.number for page in toc_pages}
        candidates = [
            _toc_candidates(entry, pages, detected, offset, toc_page_numbers)
            for entry in entries
        ]
        matches = _monotonic_alignment(entries, candidates) if entries else []
        inferred_front = _infer_first_front_matter_start(entries, matches, pages, toc_pages)
        if inferred_front is not None:
            matches[0] = inferred_front
        matched_offset, matched_offset_report = _offset_from_matches(entries, matches)
        if matched_offset is not None:
            for index, (entry, match) in enumerate(zip(entries, matches)):
                if match is not None:
                    continue
                predicted = int(entry["printed_page"]) + matched_offset
                if 1 <= predicted <= len(pages) and predicted not in toc_page_numbers:
                    candidates[index].append((predicted, 0.64, "validated_page_offset"))
                    # The printed TOC page can be a physical-page estimate rather than
                    # a true folio. When the exact page has no standalone heading,
                    # tolerate the first nearby prose page within the validated delta.
                    for nearby in range(predicted + 1, min(len(pages), predicted + 4) + 1):
                        if nearby not in toc_page_numbers:
                            candidates[index].append((nearby, 0.63, "validated_page_offset_nearby"))
                    candidates[index].sort(key=lambda item: (-item[1], item[0]))
            matches = _monotonic_alignment(entries, candidates)
            inferred_front = _infer_first_front_matter_start(entries, matches, pages, toc_pages)
            if inferred_front is not None:
                matches[0] = inferred_front
        matches, completion_actions = _complete_toc_matches(entries, matches, detected)
        mapped = [match for match in matches if match is not None]
        ordered = [match[0] for match in mapped]
        safe_alignment = ordered == sorted(ordered) and len(ordered) == len(set(ordered))
        confidence = sum(match[1] for match in mapped) / len(mapped) if mapped else 0.0
        accepted = genuine_toc_evidence and len(entries) >= 2 and safe_alignment and len(mapped) / len(entries) >= 0.7 and confidence >= max(min_confidence, 0.78)
        rejection = (
            None if accepted
            else "missing_printed_toc_evidence" if not genuine_toc_evidence
            else "unsafe_toc_alignment" if not safe_alignment
            else "insufficient_toc_coverage_or_confidence"
        )
        report["methods"].append({
            "method": "toc",
            "toc_pages": [page.number for page in toc_pages],
            "printed_toc_entries": printed_toc_entries,
            "genuine_toc_evidence": genuine_toc_evidence,
            "entries": entries,
            "page_offset": offset,
            "page_offset_evidence": offset_report,
            "direct_match_offset": matched_offset,
            "direct_match_offset_evidence": matched_offset_report,
            "completion_actions": completion_actions,
            "safe_alignment": safe_alignment,
            "rejection": rejection,
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

    allow_headings = mode in {"auto", "headings"} or bookmark_rejected_for_coverage
    if not selected and allow_headings:
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
        # overlong unit. ``blocks`` and ``text`` always come from the same page
        # view, whether native structure text or the selected processed payload.
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
