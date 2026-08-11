"""Deterministic, language-aware, line-tracked chunking for long chapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re

from .markdown import sha256_text

CJK_RE = re.compile(r"[⺀-⿿　-〿㐀-䶿一-鿿豈-﫿]")
SENTENCE_RE = re.compile(r"(?<=[。！？；!?;])\s*|\n+")
SOFT_BREAK_RE = re.compile(r"(?<=[，、：,:])\s*|\s+")
CHUNK_PROFILE_REVISION = "smaller-v1"
_CJK_DEFAULTS = (400, 60, 520)
_LATIN_DEFAULTS = (500, 75, 650)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_path: str
    chapter: int
    section: str
    text: str
    start_line: int
    end_line: int
    token_count: int
    content_hash: str
    locator: dict[str, object] = field(default_factory=dict)


_METADATA_RE = re.compile(r"^>\s*([^:]+):\s*(.*?)\s*$", re.MULTILINE)
_PAGE_HEADING_RE = re.compile(r"^##\s+Page\s+(\d+)\s*$", re.IGNORECASE)


def _metadata_locator(values: dict[str, str]) -> dict[str, object] | None:
    raw = values.get("locator", "")
    if not raw:
        return None
    try:
        locator = __import__("json").loads(raw)
    except (TypeError, ValueError):
        return None
    return dict(locator) if isinstance(locator, dict) else None


def _document_metadata(text: str) -> dict[str, str]:
    return {key.strip().lower(): value.strip() for key, value in _METADATA_RE.findall(text)}


def _page_numbers_for_lines(text: str, start_line: int, end_line: int) -> list[int]:
    """Return PDF physical pages whose Markdown ranges overlap a chunk."""
    lines = text.splitlines()
    headings = [
        (line_number, int(match.group(1)))
        for line_number, line in enumerate(lines, 1)
        if (match := _PAGE_HEADING_RE.match(line))
    ]
    pages: list[int] = []
    for index, (heading_line, page) in enumerate(headings):
        next_heading = headings[index + 1][0] if index + 1 < len(headings) else len(lines) + 1
        if heading_line <= end_line and next_heading - 1 >= start_line:
            pages.append(page)
    return pages


def locator_for_range(text: str, start_line: int, end_line: int, chapter: int) -> dict[str, object]:
    """Build a precise evidence locator from normalized imported Markdown."""
    values = _document_metadata(text)
    native = _metadata_locator(values)
    source_format = str((native or {}).get("format") or values.get("format", "")).upper()
    if source_format == "PDF":
        pages = _page_numbers_for_lines(text, start_line, end_line)
        if pages:
            return {"format": "PDF", "physical_page_start": min(pages), "physical_page_end": max(pages)}
        page_range = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", values.get("pages", ""))
        if page_range:
            return {"format": "PDF", "physical_page_start": int(page_range.group(1)), "physical_page_end": int(page_range.group(2)), "precision": "chapter-range"}
        return {"format": "PDF", "precision": "unknown"}
    if native:
        locator: dict[str, object] = dict(native)
        locator.setdefault("kind", f"{source_format.lower()}_range" if source_format else "source_range")
        locator["chapter"] = chapter
        locator["source_line_start"] = start_line
        locator["source_line_end"] = end_line
        return locator
    if source_format == "EPUB":
        locator = {"format": "EPUB", "kind": "epub_spine", "chapter": chapter}
        if values.get("spine"):
            locator["spine_id"] = values["spine"]
        if values.get("spine index"):
            locator["spine_index"] = int(values["spine index"])
        if values.get("href"):
            locator["href"] = values["href"]
        if values.get("fragment"):
            locator["fragment"] = values["fragment"]
        return locator
    return {"format": source_format or "UNKNOWN", "kind": "source_range", "chapter": chapter, "source_line_start": start_line, "source_line_end": end_line}


def count_tokens(text: str) -> int:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        # Conservative fallback: CJK characters generally consume about one
        # token each, while Latin text averages roughly four characters/token.
        cjk = len(CJK_RE.findall(text))
        non_cjk = max(0, len(text) - cjk)
        return max(1, cjk + (non_cjk + 3) // 4)


def contains_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def _paragraphs(lines: list[str]) -> list[tuple[int, int, str]]:
    blocks = []
    start = None
    for index, line in enumerate(lines, 1):
        if line.strip() and start is None:
            start = index
        if start is not None and (not line.strip() or index == len(lines)):
            end = index if line.strip() and index == len(lines) else index - 1
            text = "\n".join(lines[start - 1:end]).strip()
            if text:
                blocks.append((start, end, text))
            start = None
    return blocks


def _section_blocks(text: str) -> list[tuple[int, int, str, str]]:
    lines = text.splitlines()
    current_section = "Front Matter"
    blocks: list[tuple[int, int, str, str]] = []
    section_start = 0
    for index, line in enumerate(lines, 1):
        heading = re.match(r"^#{2,6}\s+(.+?)\s*$", line)
        if heading:
            current_section = heading.group(1)
        if line.strip() and section_start == 0:
            section_start = index
        if section_start and (not line.strip() or index == len(lines)):
            end = index if line.strip() and index == len(lines) else index - 1
            body = "\n".join(lines[section_start - 1:end]).strip()
            if body:
                blocks.append((section_start, end, current_section, body))
            section_start = 0
    if not blocks:
        blocks = [(start, end, current_section, body) for start, end, body in _paragraphs(lines)]
    return blocks


def _units(text: str, cjk: bool, max_tokens: int) -> list[str]:
    pattern = SENTENCE_RE if cjk else re.compile(r"\n+")
    units = [part.strip() for part in pattern.split(text) if part.strip()]
    if not cjk:
        return units
    refined: list[str] = []
    for unit in units:
        if count_tokens(unit) <= max_tokens:
            refined.append(unit)
            continue
        refined.extend(part.strip() for part in SOFT_BREAK_RE.split(unit) if part.strip())
    return refined


def _split_block(start: int, end: int, section: str, text: str, cjk: bool, max_tokens: int) -> list[tuple[int, int, str, str]]:
    units = _units(text, cjk, max_tokens)
    if not units:
        return []
    result: list[tuple[int, int, str, str]] = []
    for unit in units:
        if count_tokens(unit) <= max_tokens:
            # Preserve CJK sentences as separate composition units. The outer
            # accumulator applies the target budget and can therefore build
            # useful, smaller chunks rather than treating a whole paragraph as
            # indivisible.
            result.append((start, end, section, unit))
            continue
        # Find the largest prefix strictly within the limit. Tokenizers can
        # encode one character as multiple tokens, so fixed character slices
        # are insufficient for a hard API boundary.
        remaining = unit
        while remaining:
            low, high, best = 1, len(remaining), 1
            while low <= high:
                middle = (low + high) // 2
                if count_tokens(remaining[:middle]) <= max_tokens:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            result.append((start, end, section, remaining[:best]))
            remaining = remaining[best:]
    return result


def _make_chunk(blocks, source_path: str, chapter: int, ordinal: int, source_text: str) -> Chunk:
    start, _, section, _ = blocks[0]
    _, end, _, _ = blocks[-1]
    body = "\n\n".join(item[3] for item in blocks)
    digest = sha256_text(body)
    return Chunk(
        f"{chapter:04d}-{ordinal:04d}-{digest[:12]}",
        source_path,
        chapter,
        section,
        body,
        start,
        end,
        count_tokens(body),
        digest,
        locator_for_range(source_text, start, end, chapter),
    )


def _with_overlap(chunks: list[Chunk], source_path: str, chapter: int, overlap_tokens: int, max_tokens: int) -> list[Chunk]:
    if overlap_tokens <= 0 or len(chunks) < 2:
        return chunks
    result: list[Chunk] = []
    for index, chunk in enumerate(chunks):
        if index:
            previous = chunks[index - 1]
            tail = previous.text
            pieces = _units(tail, contains_cjk(tail), max_tokens)
            overlap: list[str] = []
            total = 0
            for piece in reversed(pieces):
                size = count_tokens(piece)
                if total + size > overlap_tokens:
                    if not overlap and not result[index - 1].text:
                        continue
                    break
                overlap.insert(0, piece)
                total += size
            if overlap:
                body = " ".join(overlap) + "\n\n" + chunk.text
                if count_tokens(body) > max_tokens:
                    body = chunk.text
                digest = sha256_text(body)
                chunk = Chunk(
                    f"{chapter:04d}-{index:04d}-{digest[:12]}",
                    source_path,
                    chapter,
                    chunk.section,
                    body,
                    chunk.start_line,
                    chunk.end_line,
                    count_tokens(body),
                    digest,
                    chunk.locator,
                )
        result.append(chunk)
    return result


def chunking_profile(
    text: str,
    target_words: int | None = None,
    overlap_words: int | None = None,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    max_tokens: int | None = None,
) -> dict[str, int | str]:
    """Return the normalized chunking budget applied to a document."""
    cjk = contains_cjk(text)
    default_target, default_overlap, default_maximum = _CJK_DEFAULTS if cjk else _LATIN_DEFAULTS
    target = target_tokens if target_tokens is not None else (default_target if cjk or target_words is None else target_words)
    overlap = overlap_tokens if overlap_tokens is not None else (default_overlap if cjk or overlap_words is None else overlap_words)
    maximum = max_tokens if max_tokens is not None else max(default_maximum, target + overlap)
    target = max(1, min(target, maximum))
    overlap = max(0, min(overlap, target // 2))
    return {
        "revision": CHUNK_PROFILE_REVISION,
        "language": "cjk" if cjk else "latin",
        "target_tokens": target,
        "overlap_tokens": overlap,
        "max_tokens": maximum,
    }


def chunk_text(
    text: str,
    source_path: str,
    chapter: int,
    target_words: int | None = None,
    overlap_words: int | None = None,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    max_tokens: int | None = None,
) -> list[Chunk]:
    """Split prose by language-aware boundaries with a hard token ceiling.

    Explicit token arguments take precedence. The defaults are 400/60/520 for
    CJK documents and 500/75/650 for Latin documents.
    """
    profile = chunking_profile(
        text,
        target_words,
        overlap_words,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        max_tokens=max_tokens,
    )
    cjk = profile["language"] == "cjk"
    target = int(profile["target_tokens"])
    overlap = int(profile["overlap_tokens"])
    maximum = int(profile["max_tokens"])

    units: list[tuple[int, int, str, str]] = []
    for start, end, section, body in _section_blocks(text):
        units.extend(_split_block(start, end, section, body, cjk, maximum))

    grouped: list[Chunk] = []
    pending: list[tuple[int, int, str, str]] = []
    pending_tokens = 0
    for block in units:
        block_tokens = count_tokens(block[3])
        if pending and pending_tokens + block_tokens > target:
            grouped.append(_make_chunk(pending, source_path, chapter, len(grouped), text))
            # Apply overlap only after chunks have been formed. Carrying whole
            # blocks here can immediately exceed the target when a CJK
            # sentence is larger than the requested overlap budget.
            pending, pending_tokens = [], 0
        pending.append(block)
        pending_tokens += block_tokens
    if pending:
        grouped.append(_make_chunk(pending, source_path, chapter, len(grouped), text))
    return _with_overlap(grouped, source_path, chapter, overlap, maximum)


def chunks_for_file(path: Path, chapter: int) -> list[Chunk]:
    return chunk_text(path.read_text(encoding="utf-8"), path.as_posix(), chapter)


def chunk_dict(chunk: Chunk) -> dict:
    return asdict(chunk)
