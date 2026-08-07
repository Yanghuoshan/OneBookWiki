"""Validate and render evidence references from generated artifacts."""
from __future__ import annotations

import re
from pathlib import Path

from .manifest import Manifest
from .models import EvidenceRef


def evidence_map(root: Path) -> dict[str, EvidenceRef]:
    manifest = Manifest.load(root)
    result: dict[str, EvidenceRef] = {}
    for chunk_id, chunk in manifest.chunks.items():
        source = str(chunk.get("source_path", ""))
        chapter = int(chunk.get("chapter", 0))
        evidence_id = f"{chapter}:{chunk_id}"
        result[evidence_id] = EvidenceRef(evidence_id, chunk_id, source, chapter, int(chunk.get("start_line", 0)), int(chunk.get("end_line", 0)), str(chunk.get("text", "")))
    return result


def validate_evidence(root: Path, refs: list[EvidenceRef]) -> list[str]:
    manifest = Manifest.load(root)
    problems: list[str] = []
    raw_root = (root / "raw" / "chapters").resolve()
    for ref in refs:
        chunk = manifest.chunks.get(ref.chunk_id)
        if chunk is None:
            problems.append(f"unknown chunk: {ref.chunk_id}")
            continue
        source = (root / ref.source_path).resolve()
        if not source.is_relative_to(raw_root) or not source.is_file():
            problems.append(f"invalid evidence source: {ref.source_path}")
            continue
        lines = source.read_text(encoding="utf-8").splitlines()
        if ref.start_line < 1 or ref.end_line < ref.start_line or ref.end_line > len(lines):
            problems.append(f"invalid line range: {ref.evidence_id}")
            continue
        excerpt = "\n".join(lines[ref.start_line - 1 : ref.end_line])
        if ref.quote and ref.quote not in excerpt and " ".join(ref.quote.split()) not in " ".join(excerpt.split()):
            problems.append(f"quote not found: {ref.evidence_id}")
        if int(chunk.get("chapter", 0)) != ref.chapter:
            problems.append(f"chapter mismatch: {ref.evidence_id}")
    return problems


def citation_links(root: Path, refs: list[EvidenceRef]) -> list[str]:
    links: list[str] = []
    for ref in refs:
        anchor = f"L{ref.start_line}-L{ref.end_line}"
        links.append(f"[{ref.source_path}:{ref.start_line}-{ref.end_line}](../{ref.source_path}#{anchor})")
    return links


def evidence_ids_in_claims(value: object) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                ids.update(str(entry) for entry in item)
            else:
                ids.update(evidence_ids_in_claims(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(evidence_ids_in_claims(item))
    return ids
