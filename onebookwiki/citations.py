"""Validate and render evidence references from generated artifacts."""
from __future__ import annotations

import re
from pathlib import Path

from .manifest import Manifest
from .models import EvidenceRef


def locator_for_ref(ref: EvidenceRef) -> dict[str, object]:
    """Normalize new and legacy evidence provenance into one locator."""
    if ref.locator:
        return dict(ref.locator)
    if ref.page is not None:
        return {"format": "PDF", "physical_page_start": ref.page, "physical_page_end": ref.page}
    if ref.spine:
        return {"format": "EPUB", "chapter": ref.chapter, "spine_id": ref.spine}
    return {"format": "UNKNOWN", "chapter": ref.chapter}


def display_label(ref: EvidenceRef) -> str:
    locator = locator_for_ref(ref)
    source_format = str(locator.get("format", "")).upper()
    if source_format == "PDF":
        start = locator.get("physical_page_start")
        end = locator.get("physical_page_end", start)
        if isinstance(start, int) and isinstance(end, int):
            return f"PDF p. {start}" if start == end else f"PDF pp. {start}-{end}"
        return f"PDF Chapter {ref.chapter}"
    if source_format == "EPUB":
        label = f"EPUB Ch. {locator.get('chapter', ref.chapter)}"
        if locator.get("spine_index") is not None:
            label += f" · Spine {locator['spine_index']}"
        elif locator.get("spine_id"):
            label += f" · Spine {locator['spine_id']}"
        href = str(locator.get("href", ""))
        fragment = str(locator.get("fragment", ""))
        if href:
            label += f" · {href}{('#' + fragment) if fragment else ''}"
        return label
    return f"Chapter {ref.chapter} · lines {ref.start_line}-{ref.end_line}"


def evidence_map(root: Path) -> dict[str, EvidenceRef]:
    manifest = Manifest.load(root)
    result: dict[str, EvidenceRef] = {}
    for chunk_id, chunk in manifest.chunks.items():
        source = str(chunk.get("source_path", ""))
        chapter = int(chunk.get("chapter", 0))
        evidence_id = f"{chapter}:{chunk_id}"
        result[evidence_id] = EvidenceRef(
            evidence_id,
            chunk_id,
            source,
            chapter,
            int(chunk.get("start_line", 0)),
            int(chunk.get("end_line", 0)),
            str(chunk.get("text", "")),
            locator=dict(chunk.get("locator") or {}),
        )
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
