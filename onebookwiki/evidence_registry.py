"""Immutable body-only evidence and book revisions for Grounded v2."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .chunking import Chunk, chunk_text, chunking_profile, locator_for_range
from .markdown import BodyExtraction, extract_body, metadata, parse_document

CONTRACT_VERSION = "grounded-v2"
REGISTRY_VERSION = 1


class EvidenceRegistryError(RuntimeError):
    """Raised when an immutable evidence or book revision is inconsistent."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_immutable(path: Path, data: bytes) -> None:
    if path.is_file():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise EvidenceRegistryError(f"cannot read immutable revision {path}") from exc
        if existing != data:
            raise EvidenceRegistryError(f"immutable revision content mismatch: {path}")
        return
    _atomic_write(path, data)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceRegistryError(f"invalid revision JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EvidenceRegistryError(f"revision JSON must be an object: {path}")
    return value


def _chapter_number(path: Path, text: str) -> int | None:
    values = metadata(parse_document(text))
    chapter = values.get("chapter", "").strip()
    if chapter.isdigit():
        return int(chapter)
    match = re.match(r"^(\d+)[-_]", path.name)
    return int(match.group(1)) if match else None


def _relative_source_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise EvidenceRegistryError(f"source path is outside book root: {path}") from exc


def _safe_revision_path(root: Path, relative: str, expected: str) -> Path:
    if relative != expected:
        raise EvidenceRegistryError("revision body path mismatch")
    resolved = (root / relative).resolve()
    knowledge_root = (root / ".onebookwiki" / "knowledge").resolve()
    if not resolved.is_relative_to(knowledge_root):
        raise EvidenceRegistryError("revision body path escapes the knowledge store")
    return resolved


def _body_segments(extraction: BodyExtraction) -> list[tuple[int, int, str, list[int]]]:
    """Return exact nonblank body spans with physical Markdown coordinates."""
    if not extraction.body:
        return []
    lines = extraction.body.split("\n")
    if len(lines) != len(extraction.source_line_map):
        raise EvidenceRegistryError("body line map length mismatch")
    segments: list[tuple[int, int, str, list[int]]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() and start is None:
            start = index
        at_end = index == len(lines) - 1
        if start is not None and (not line.strip() or at_end):
            end = index if line.strip() and at_end else index - 1
            source_lines = list(extraction.source_line_map[start : end + 1])
            segments.append((source_lines[0], source_lines[-1], "\n".join(lines[start : end + 1]), source_lines))
            start = None
    return segments


@dataclass(frozen=True)
class EvidenceRevision:
    evidence_revision_id: str
    revision_hash: str
    source_path: str
    chapter: int
    segment_ordinal: int
    source_file_hash: str
    source_body_hash: str
    body_hash: str
    source_line_start: int
    source_line_end: int
    source_line_map: tuple[int, ...]
    locator: dict[str, object]
    quote: str
    body_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "registry_version": REGISTRY_VERSION,
            "kind": "evidence_revision",
            "evidence_revision_id": self.evidence_revision_id,
            "revision_hash": self.revision_hash,
            "source_path": self.source_path,
            "chapter": self.chapter,
            "segment_ordinal": self.segment_ordinal,
            "source_body_hash": self.source_body_hash,
            "body_hash": self.body_hash,
            "source_line_start": self.source_line_start,
            "source_line_end": self.source_line_end,
            "source_line_map": list(self.source_line_map),
            "locator": dict(self.locator),
            "quote": self.quote,
            "body_path": self.body_path,
        }


@dataclass(frozen=True)
class RegisteredChapter:
    source_path: str
    chapter: int
    source_file_hash: str
    body_hash: str
    chunking: dict[str, int | str]
    chunks: tuple[Chunk, ...]
    evidence: tuple[EvidenceRevision, ...]

    @property
    def evidence_revision_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_revision_id for item in self.evidence)


@dataclass(frozen=True)
class RegistrySnapshot:
    book_revision_id: str
    book_revision_hash: str
    book_revision: dict[str, Any]
    chapters: dict[str, RegisteredChapter]
    evidence: dict[str, EvidenceRevision]


def _revision_payload(
    source_path: str,
    chapter: int,
    segment_ordinal: int,
    source_body_hash: str,
    body_hash: str,
    source_line_start: int,
    source_line_end: int,
    source_line_map: list[int],
    locator: dict[str, object],
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "registry_version": REGISTRY_VERSION,
        "kind": "evidence_revision",
        "source_path": source_path,
        "chapter": chapter,
        "segment_ordinal": segment_ordinal,
        "source_body_hash": source_body_hash,
        "body_hash": body_hash,
        "source_line_start": source_line_start,
        "source_line_end": source_line_end,
        "source_line_map": source_line_map,
        "locator": locator,
    }


def _register_evidence(
    root: Path,
    source_path: str,
    chapter: int,
    segment_ordinal: int,
    source_file_hash: str,
    source_body_hash: str,
    source_line_start: int,
    source_line_end: int,
    source_line_map: list[int],
    locator: dict[str, object],
    quote: str,
) -> EvidenceRevision:
    body_hash = _sha256_text(quote)
    payload = _revision_payload(
        source_path,
        chapter,
        segment_ordinal,
        source_body_hash,
        body_hash,
        source_line_start,
        source_line_end,
        source_line_map,
        locator,
    )
    revision_hash = _sha256_bytes(_canonical_json(payload))
    evidence_revision_id = f"evr-{revision_hash[:32]}"
    body_path = f".onebookwiki/knowledge/evidence-bodies/{evidence_revision_id}.txt"
    revision = EvidenceRevision(
        evidence_revision_id=evidence_revision_id,
        revision_hash=revision_hash,
        source_path=source_path,
        chapter=chapter,
        segment_ordinal=segment_ordinal,
        source_file_hash=source_file_hash,
        source_body_hash=source_body_hash,
        body_hash=body_hash,
        source_line_start=source_line_start,
        source_line_end=source_line_end,
        source_line_map=tuple(source_line_map),
        locator=dict(locator),
        quote=quote,
        body_path=body_path,
    )
    revision_path = root / ".onebookwiki" / "knowledge" / "revisions" / "evidence" / f"{evidence_revision_id}.json"
    revision_bytes = json.dumps(revision.to_dict(), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _write_immutable(root / body_path, quote.encode("utf-8"))
    _write_immutable(revision_path, revision_bytes)
    return revision


def register_chapter(root: Path, path: Path, chapter: int) -> RegisteredChapter:
    """Register exact body spans from one raw Markdown chapter."""
    root = root.resolve()
    path = path.resolve()
    source_path = _relative_source_path(root, path)
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceRegistryError(f"cannot read UTF-8 source chapter: {path}") from exc
    extraction = extract_body(text)
    if not extraction.body:
        raise EvidenceRegistryError(f"source chapter has no evidence body: {source_path}")
    source_file_hash = _sha256_bytes(raw_bytes)
    source_body_hash = _sha256_text(extraction.body)
    evidence = tuple(
        _register_evidence(
            root,
            source_path,
            chapter,
            ordinal,
            source_file_hash,
            source_body_hash,
            start,
            end,
            source_lines,
            locator_for_range(text, start, end, chapter),
            quote,
        )
        for ordinal, (start, end, quote, source_lines) in enumerate(_body_segments(extraction), 1)
    )
    if not evidence:
        raise EvidenceRegistryError(f"source chapter has no registerable evidence: {source_path}")
    return RegisteredChapter(
        source_path=source_path,
        chapter=chapter,
        source_file_hash=source_file_hash,
        body_hash=source_body_hash,
        chunking=chunking_profile(extraction.body),
        chunks=tuple(chunk_text(text, source_path, chapter)),
        evidence=evidence,
    )


def _book_revision_payload(chapters: list[RegisteredChapter], evidence: dict[str, EvidenceRevision]) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "registry_version": REGISTRY_VERSION,
        "kind": "book_revision",
        "chapters": [
            {
                "source_path": chapter.source_path,
                "chapter": chapter.chapter,
                "body_hash": chapter.body_hash,
                "evidence_revision_ids": list(chapter.evidence_revision_ids),
            }
            for chapter in sorted(chapters, key=lambda item: (item.chapter, item.source_path))
        ],
        "evidence_revision_ids": sorted(evidence),
    }


def register_project(root: Path) -> RegistrySnapshot:
    """Register all raw chapters and publish a deterministic book revision pointer."""
    root = root.resolve()
    raw_dir = root / "raw" / "chapters"
    if not raw_dir.is_dir():
        raise EvidenceRegistryError(f"no raw/chapters directory under {root}")
    chapters: dict[str, RegisteredChapter] = {}
    evidence: dict[str, EvidenceRevision] = {}
    for path in sorted(raw_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise EvidenceRegistryError(f"cannot read source chapter: {path}") from exc
        chapter = _chapter_number(path, text)
        if chapter is None:
            raise EvidenceRegistryError(f"cannot determine chapter number: {path.name}")
        registered = register_chapter(root, path, chapter)
        chapters[registered.source_path] = registered
        for revision in registered.evidence:
            previous = evidence.setdefault(revision.evidence_revision_id, revision)
            if previous != revision:
                raise EvidenceRegistryError(f"evidence revision collision: {revision.evidence_revision_id}")
    if not chapters or not evidence:
        raise EvidenceRegistryError("book has no registerable evidence")
    payload = _book_revision_payload(list(chapters.values()), evidence)
    revision_hash = _sha256_bytes(_canonical_json(payload))
    book_revision_id = f"book-{revision_hash[:32]}"
    book_revision = {
        **payload,
        "book_revision_id": book_revision_id,
        "revision_hash": revision_hash,
    }
    book_path = root / ".onebookwiki" / "knowledge" / "revisions" / "book" / f"{book_revision_id}.json"
    _write_immutable(
        book_path,
        json.dumps(book_revision, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    pointer = {
        "contract_version": CONTRACT_VERSION,
        "registry_version": REGISTRY_VERSION,
        "book_revision_id": book_revision_id,
        "revision_hash": revision_hash,
    }
    _atomic_write(
        root / ".onebookwiki" / "knowledge" / "current.json",
        json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    return RegistrySnapshot(book_revision_id, revision_hash, book_revision, chapters, evidence)


def load_evidence_revision(
    root: Path,
    evidence_revision_id: str,
    *,
    expected_source_file_hash: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Load and verify one fixed revision against its body and current source."""
    if not re.fullmatch(r"evr-[0-9a-f]{32}", evidence_revision_id):
        raise EvidenceRegistryError("invalid evidence revision id")
    revision_path = root / ".onebookwiki" / "knowledge" / "revisions" / "evidence" / f"{evidence_revision_id}.json"
    revision = _read_json(revision_path)
    if revision.get("evidence_revision_id") != evidence_revision_id:
        raise EvidenceRegistryError("evidence revision identity mismatch")
    payload = {key: revision[key] for key in (
        "contract_version", "registry_version", "kind", "source_path", "chapter", "segment_ordinal",
        "source_body_hash", "body_hash", "source_line_start", "source_line_end", "source_line_map", "locator",
    )}
    revision_hash = _sha256_bytes(_canonical_json(payload))
    if revision_hash != revision.get("revision_hash") or evidence_revision_id != f"evr-{revision_hash[:32]}":
        raise EvidenceRegistryError("evidence revision payload hash mismatch")
    expected_body_path = f".onebookwiki/knowledge/evidence-bodies/{evidence_revision_id}.txt"
    body_path = _safe_revision_path(root, str(revision.get("body_path", "")), expected_body_path)
    try:
        body = body_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceRegistryError("evidence body is missing or unreadable") from exc
    if _sha256_text(body) != revision.get("body_hash") or body != revision.get("quote"):
        raise EvidenceRegistryError("evidence body hash or quote mismatch")
    source_path = (root / str(revision.get("source_path", ""))).resolve()
    raw_root = (root / "raw" / "chapters").resolve()
    if not source_path.is_relative_to(raw_root):
        raise EvidenceRegistryError("source chapter path escapes raw/chapters")
    try:
        raw_bytes = source_path.read_bytes()
        extraction = extract_body(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceRegistryError("source chapter is missing or unreadable") from exc
    if expected_source_file_hash is not None and _sha256_bytes(raw_bytes) != expected_source_file_hash:
        raise EvidenceRegistryError("source chapter hash mismatch")
    if _sha256_text(extraction.body) != revision.get("source_body_hash"):
        raise EvidenceRegistryError("source body hash mismatch")
    start = int(revision.get("source_line_start", 0))
    end = int(revision.get("source_line_end", 0))
    selected = [
        (line_number, line)
        for line_number, line in zip(extraction.source_line_map, extraction.body.split("\n"))
        if start <= line_number <= end
    ]
    if [line for line, _ in selected] != revision.get("source_line_map"):
        raise EvidenceRegistryError("evidence source line map mismatch")
    if "\n".join(line for _, line in selected) != body:
        raise EvidenceRegistryError("evidence body is not an exact source span")
    return revision, body
