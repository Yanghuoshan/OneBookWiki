"""Deterministic, language-aware, line-tracked chunking for long chapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re

from .markdown import sha256_text

CJK_RE = re.compile(r"[⺀-⿿　-〿㐀-䶿一-鿿豈-﫿]")
SENTENCE_RE = re.compile(r"(?<=[。！？；!?;])\s*|\n+")
SOFT_BREAK_RE = re.compile(r"(?<=[，、：,:])\s*|\s+")


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


def _units(text: str, cjk: bool) -> list[str]:
    pattern = SENTENCE_RE if cjk else re.compile(r"\n+")
    units = [part.strip() for part in pattern.split(text) if part.strip()]
    if not cjk:
        return units
    refined: list[str] = []
    for unit in units:
        if count_tokens(unit) <= 800:
            refined.append(unit)
            continue
        refined.extend(part.strip() for part in SOFT_BREAK_RE.split(unit) if part.strip())
    return refined


def _split_block(start: int, end: int, section: str, text: str, cjk: bool, max_tokens: int) -> list[tuple[int, int, str, str]]:
    units = _units(text, cjk)
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


def _make_chunk(blocks, source_path: str, chapter: int, ordinal: int) -> Chunk:
    start, _, section, _ = blocks[0]
    _, end, _, _ = blocks[-1]
    body = "\n\n".join(item[3] for item in blocks)
    digest = sha256_text(body)
    return Chunk(f"{chapter:04d}-{ordinal:04d}-{digest[:12]}", source_path, chapter, section, body, start, end, count_tokens(body), digest)


def _with_overlap(chunks: list[Chunk], source_path: str, chapter: int, overlap_tokens: int, max_tokens: int) -> list[Chunk]:
    if overlap_tokens <= 0 or len(chunks) < 2:
        return chunks
    result: list[Chunk] = []
    for index, chunk in enumerate(chunks):
        if index:
            previous = chunks[index - 1]
            tail = previous.text
            pieces = _units(tail, contains_cjk(tail))
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
                chunk = Chunk(f"{chapter:04d}-{index:04d}-{digest[:12]}", source_path, chapter, chunk.section, body, chunk.start_line, chunk.end_line, count_tokens(body), digest)
        result.append(chunk)
    return result


def chunk_text(
    text: str,
    source_path: str,
    chapter: int,
    target_words: int = 263,
    overlap_words: int = 38,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    max_tokens: int | None = None,
) -> list[Chunk]:
    """Split prose by language-aware boundaries with a hard token ceiling.

    ``target_words`` and ``overlap_words`` remain for compatibility. Explicit
    token arguments take precedence; CJK defaults are 225/30/300 and Latin
    defaults are 263/38/338.
    """
    cjk = contains_cjk(text)
    target = target_tokens if target_tokens is not None else (225 if cjk else target_words)
    overlap = overlap_tokens if overlap_tokens is not None else (30 if cjk else overlap_words)
    maximum = max_tokens if max_tokens is not None else (300 if cjk else max(338, target + overlap))
    target = max(1, min(target, maximum))
    overlap = max(0, min(overlap, target // 2))

    units: list[tuple[int, int, str, str]] = []
    for start, end, section, body in _section_blocks(text):
        units.extend(_split_block(start, end, section, body, cjk, maximum))

    grouped: list[Chunk] = []
    pending: list[tuple[int, int, str, str]] = []
    pending_tokens = 0
    for block in units:
        block_tokens = count_tokens(block[3])
        if pending and pending_tokens + block_tokens > target:
            grouped.append(_make_chunk(pending, source_path, chapter, len(grouped)))
            # Apply overlap only after chunks have been formed. Carrying whole
            # blocks here can immediately exceed the target when a CJK
            # sentence is larger than the requested overlap budget.
            pending, pending_tokens = [], 0
        pending.append(block)
        pending_tokens += block_tokens
    if pending:
        grouped.append(_make_chunk(pending, source_path, chapter, len(grouped)))
    return _with_overlap(grouped, source_path, chapter, overlap, maximum)


def chunks_for_file(path: Path, chapter: int) -> list[Chunk]:
    return chunk_text(path.read_text(encoding="utf-8"), path.as_posix(), chapter)


def chunk_dict(chunk: Chunk) -> dict:
    return asdict(chunk)
