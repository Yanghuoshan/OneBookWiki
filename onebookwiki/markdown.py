"""Markdown parsing and evidence extraction used by the book checker."""

from __future__ import annotations

import hashlib
import re
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
class Candidate:
    kind: str
    value: str


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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
        return candidate.value in haystack
    suffix_guard = r"(?!-\d{2})" if candidate.kind == "date" and len(candidate.value) == 7 else ""
    pattern = r"(?<![\d.,])" + re.escape(candidate.value) + suffix_guard + r"(?![A-Za-z0-9]|[.,]\d|%)"
    return re.search(pattern, haystack) is not None


def source_content(path: Path) -> str:
    return normalize("\n".join(parse_document(path.read_text(encoding="utf-8")).body))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
