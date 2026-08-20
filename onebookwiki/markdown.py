"""Markdown parsing and evidence extraction used by the book checker."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

NUMBER_RE = re.compile(r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)*(?:\s*[KMB%](?![A-Za-z]))?|\d+(?:\.\d+)*(?:\s*[KMB%](?![A-Za-z]))?)(?![A-Za-z])")
DATE_RE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")
QUOTE_RES = (re.compile(r'"([^"\n]*)"'), re.compile(r"“([^”\n]*)”"))
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
METADATA_RE = re.compile(r"^>\s*([A-Za-z][A-Za-z ]*):")
STATUS_RE = re.compile(r"^>\s*\*\*Status:")
RAW_LINK_RE = re.compile(r"\(([^)]+\.md)(?:#[^)]*)?\)")

@dataclass(frozen=True)
class Document:
    title: str | None
    header: tuple[str, ...]
    body: tuple[str, ...]


@dataclass(frozen=True)
class BodyExtraction:
    """Body-only Markdown content with a map back to physical source lines."""

    title: str | None
    header: tuple[str, ...]
    body: str
    source_line_map: tuple[int, ...]

    @property
    def body_start_line(self) -> int | None:
        return self.source_line_map[0] if self.source_line_map else None

    @property
    def body_end_line(self) -> int | None:
        return self.source_line_map[-1] if self.source_line_map else None


@dataclass(frozen=True)
class Candidate:
    kind: str
    value: str


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _canonical_lines(text: str) -> list[str]:
    """Split text with stable line endings and NFC-normalized characters."""
    return [unicodedata.normalize("NFC", line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def extract_body(text: str) -> BodyExtraction:
    """Return Markdown body content while preserving physical source lines.

    Imported chapters put a title and a variable-length blockquote metadata
    header before the evidence body. Those lines are excluded, but body line
    numbers remain coordinates in the materialized raw Markdown file.
    """
    lines = _canonical_lines(text)
    title: str | None = None
    header: list[str] = []
    body: list[tuple[int, str]] = []
    state = "before"
    active: tuple[str, int] | None = None

    for line_number, line in enumerate(lines, 1):
        if active:
            body.append((line_number, line))
            if is_fence_closer(line, *active):
                active = None
            continue
        opener = fence_opener(line)
        if opener:
            if state == "after-title":
                state = "body"
            if state == "body":
                body.append((line_number, line))
                active = opener
                continue
            # A fence before the title is not metadata and remains body text.
            body.append((line_number, line))
            active = opener
            continue
        if state == "before":
            if line.startswith("# ") and title is None:
                title, state = line, "after-title"
            else:
                body.append((line_number, line))
        elif state == "after-title":
            if not line.strip():
                continue
            if METADATA_RE.match(line.strip()):
                header.append(line)
                state = "header"
            else:
                body.append((line_number, line))
                state = "body"
        elif state == "header":
            if METADATA_RE.match(line.strip()):
                header.append(line)
            else:
                body.append((line_number, line))
                state = "body"
        else:
            body.append((line_number, line))

    while body and not body[0][1].strip():
        body.pop(0)
    while body and not body[-1][1].strip():
        body.pop()
    return BodyExtraction(
        title=title,
        header=tuple(header),
        body="\n".join(line for _, line in body),
        source_line_map=tuple(line_number for line_number, _ in body),
    )


def fence_opener(line: str) -> tuple[str, int] | None:
    match = FENCE_OPEN_RE.match(line)
    if not match:
        return None
    marker, info = match.groups()
    if marker[0] == "`" and "`" in info:
        return None
    return marker[0], len(marker)


def is_fence_closer(line: str, char: str, length: int) -> bool:
    match = FENCE_CLOSE_RE.match(line)
    return bool(match and match.group(1)[0] == char and len(match.group(1)) >= length)


def strip_fences(text: str) -> str:
    output: list[str] = []
    active: tuple[str, int] | None = None
    for line in text.splitlines():
        if active:
            if is_fence_closer(line, *active):
                active = None
            continue
        opener = fence_opener(line)
        if opener:
            active = opener
        else:
            output.append(line)
    return "\n".join(output)


def parse_document(text: str) -> Document:
    title = None
    header: list[str] = []
    preamble: list[str] = []
    body: list[str] = []
    state = "before"
    active: tuple[str, int] | None = None
    for line in text.splitlines():
        if active:
            if is_fence_closer(line, *active):
                active = None
            continue
        opener = fence_opener(line)
        if opener:
            active = opener
            if state == "after-title":
                state = "body"
            continue
        if state == "before":
            if line.startswith("# "):
                title, state = line, "after-title"
            else:
                preamble.append(line)
        elif state == "after-title":
            if not line.strip():
                continue
            if line.strip().startswith(">"):
                header.append(line); state = "header"
            else:
                body.append(line); state = "body"
        elif state == "header":
            if line.strip().startswith(">"):
                header.append(line)
            else:
                body.append(line); state = "body"
        else:
            body.append(line)
    return Document(title, tuple(header), tuple(preamble + body))


def metadata(document: Document) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in document.header:
        match = METADATA_RE.match(line.strip())
        if match:
            key = match.group(1).strip().lower().replace(" ", "_")
            result[key] = line.split(":", 1)[1].strip()
    return result


def raw_links_of(text: str) -> list[str]:
    return [link for line in parse_document(text).header if line.strip().lower().startswith("> raw:") for link in RAW_LINK_RE.findall(line)]


def strip_noise(text: str) -> str:
    text = INLINE_CODE_RE.sub(" ", text)
    return LINK_RE.sub(r"\1", text)


def _keep_number(value: str) -> bool:
    value = value.strip()
    return bool(re.search(r"[KMB%]$", value) or "," in value or "." in value or len(value) >= 4)


def extract_candidates(text: str) -> list[Candidate]:
    document = parse_document(text)
    lines = ([document.title] if document.title else []) + [line for line in document.header if not METADATA_RE.match(line.strip())] + list(document.body)
    candidates: list[Candidate] = []
    paragraph: list[str] = []
    quote_lines: list[str] = []
    skipping_status = False

    def flush_paragraph() -> None:
        if paragraph:
            joined = normalize(" ".join(paragraph))
            for regex in QUOTE_RES:
                candidates.extend(Candidate("quote", m.group(1).strip()) for m in regex.finditer(joined) if len(m.group(1).strip()) >= 15)
            paragraph.clear()

    def flush_quote() -> None:
        if quote_lines:
            joined = normalize(" ".join(quote_lines))
            if len(joined) >= 15:
                candidates.append(Candidate("quote", joined))
            quote_lines.clear()

    for line in lines:
        stripped = line.strip()
        if STATUS_RE.match(stripped):
            flush_paragraph(); flush_quote(); skipping_status = True; continue
        if skipping_status:
            if stripped.startswith(">"):
                continue
            skipping_status = False
        if stripped.startswith(">"):
            flush_paragraph()
            content = strip_noise(stripped.lstrip("> "))
            quote_lines.append(content)
            candidates.extend(_numbers(content))
            continue
        flush_quote()
        if not stripped:
            flush_paragraph(); continue
        clean = strip_noise(line)
        candidates.extend(_numbers(clean))
        paragraph.append(clean)
    flush_quote(); flush_paragraph()
    unique: list[Candidate] = []
    seen: set[Candidate] = set()
    for candidate in candidates:
        value = candidate.value.strip().strip(".,;:()[]")
        normalized = Candidate(candidate.kind, value)
        if value and normalized not in seen:
            seen.add(normalized); unique.append(normalized)
    return unique


def _numbers(line: str) -> list[Candidate]:
    clean = strip_noise(line)
    dates = list(DATE_RE.finditer(clean))
    masked = list(clean)
    results = [Candidate("date", match.group()) for match in dates]
    for match in dates:
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    results.extend(Candidate("number", match.group()) for match in NUMBER_RE.finditer("".join(masked)) if _keep_number(match.group()))
    return results


def contains(haystack: str, candidate: Candidate) -> bool:
    if candidate.kind == "quote":
        if candidate.value in haystack:
            return True
        # OCR often inserts spaces between CJK characters. Treat that as
        # formatting noise while retaining exact character and punctuation order.
        compact = lambda value: re.sub(r"\s+", "", value)
        return compact(candidate.value) in compact(haystack)
    suffix_guard = r"(?!-\d{2})" if candidate.kind == "date" and len(candidate.value) == 7 else ""
    pattern = r"(?<![\d.,])" + re.escape(candidate.value) + suffix_guard + r"(?![A-Za-z0-9]|[.,]\d|%)"
    return re.search(pattern, haystack) is not None


def source_content(path: Path) -> str:
    """Return normalized source prose without title or metadata headers."""
    return normalize(extract_body(path.read_text(encoding="utf-8")).body)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
