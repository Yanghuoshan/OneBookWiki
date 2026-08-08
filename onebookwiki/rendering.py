"""Deterministic Wikipedia-like Markdown rendering for validated artifacts."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

from .chunking import locator_for_range
from .citations import display_label
from .models import BookSynthesis, ChapterInterpretation, Claim, EvidenceRef, book_from_dict, chapter_from_dict, to_dict


CITATION_ID_RE = re.compile(r"\[?(C\d+E\d+)(?!\d)\]?")


def _citation(ref: EvidenceRef) -> str:
    return f"[{display_label(ref)}](onebookwiki://evidence/{ref.evidence_id})"


def _citation_list(ids: tuple[str, ...], evidence_by_id: dict[str, EvidenceRef]) -> str:
    citations = [_citation(evidence_by_id[item]) for item in ids if item in evidence_by_id]
    return f" ({'; '.join(citations)})" if citations else ""


def _replace_inline_citations(text: str, evidence_by_id: dict[str, EvidenceRef]) -> str:
    return CITATION_ID_RE.sub(
        lambda match: _citation(evidence_by_id[match.group(1)]) if match.group(1) in evidence_by_id else "[来源未解析]",
        text,
    )


def _replace_inline_citation_labels(text: str, evidence_by_id: dict[str, EvidenceRef]) -> str:
    return CITATION_ID_RE.sub(
        lambda match: f" [{display_label(evidence_by_id[match.group(1)])}]" if match.group(1) in evidence_by_id else " [来源未解析]",
        text,
    )


def _inline_evidence_ids(text: str) -> set[str]:
    return {match.group(1) for match in CITATION_ID_RE.finditer(text)}


def _hydrate_legacy_locators(chapters: list[ChapterInterpretation], root: Path) -> list[ChapterInterpretation]:
    """Derive display locators for old artifacts without rewriting them."""
    source_texts: dict[str, str] = {}
    hydrated: list[ChapterInterpretation] = []
    for chapter in chapters:
        evidence: list[EvidenceRef] = []
        for ref in chapter.evidence:
            if ref.locator:
                evidence.append(ref)
                continue
            raw_path = root / ref.source_path
            if raw_path.is_file():
                source_text = source_texts.setdefault(ref.source_path, raw_path.read_text(encoding="utf-8"))
                locator = locator_for_range(source_text, ref.start_line, ref.end_line, ref.chapter)
            else:
                locator = {}
            evidence.append(replace(ref, locator=locator) if locator else ref)
        hydrated.append(replace(chapter, evidence=evidence))
    return hydrated


def _manifest_evidence(root: Path) -> dict[str, EvidenceRef]:
    """Recreate refs omitted by legacy artifacts from manifest chunk order."""
    manifest_path = root / ".onebookwiki" / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    chunks = manifest.get("chunks", {})
    raw_texts: dict[str, str] = {}
    result: dict[str, EvidenceRef] = {}
    for chapter_data in (manifest.get("chapters", {}) or {}).values():
        chapter = int(chapter_data.get("chapter", 0))
        for index, chunk_id in enumerate(chapter_data.get("chunk_ids", []), 1):
            chunk = chunks.get(chunk_id, {})
            if not isinstance(chunk, dict):
                continue
            source_path = str(chunk.get("source_path", ""))
            start_line = int(chunk.get("start_line", 0))
            end_line = int(chunk.get("end_line", 0))
            locator = dict(chunk.get("locator") or {})
            raw_path = root / source_path
            if not locator and raw_path.is_file():
                source_text = raw_texts.setdefault(source_path, raw_path.read_text(encoding="utf-8"))
                locator = locator_for_range(source_text, start_line, end_line, chapter)
            result[f"C{chapter}E{index}"] = EvidenceRef(
                evidence_id=f"C{chapter}E{index}",
                chunk_id=str(chunk.get("chunk_id", chunk_id)),
                source_path=source_path,
                chapter=int(chunk.get("chapter", chapter)),
                start_line=start_line,
                end_line=end_line,
                locator=locator,
            )
    return result


def _evidence_index(root: Path, chapters: list[ChapterInterpretation]) -> dict[str, EvidenceRef]:
    result = _manifest_evidence(root)
    result.update({ref.evidence_id: ref for chapter in chapters for ref in chapter.evidence})
    return result


def _source_metadata(root: Path) -> dict:
    path = root / ".onebookwiki" / "source.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _chapter_label(chapter: ChapterInterpretation) -> str:
    return chapter.title or chapter.source_title or f"Reading unit {chapter.chapter}"


def _chapter_link(chapter: ChapterInterpretation, prefix: str = "") -> str:
    return f"[{_chapter_label(chapter)}]({prefix}chapters/{chapter.chapter:02d}-{slugify(_chapter_label(chapter))}.md)"


def _write_evidence_index(root: Path, chapters: list[ChapterInterpretation], book_title: str = "") -> Path:
    source_hash = ""
    source_path = root / ".onebookwiki" / "source.json"
    if source_path.is_file():
        try:
            source_hash = str(json.loads(source_path.read_text(encoding="utf-8")).get("source_hash", ""))
        except (OSError, ValueError):
            pass
    chapter_by_number = {chapter.chapter: chapter for chapter in chapters}
    records = {}
    for evidence_id, ref in _evidence_index(root, chapters).items():
        excerpt = ""
        raw_path = root / ref.source_path
        if raw_path.is_file() and ref.start_line > 0 and ref.end_line >= ref.start_line:
            excerpt = "\n".join(raw_path.read_text(encoding="utf-8").splitlines()[ref.start_line - 1 : ref.end_line]).strip()[:800]
        chapter = chapter_by_number.get(ref.chapter)
        semantic = {
            "book_title": book_title,
            "source_title": _chapter_label(chapter) if chapter else "",
            "breadcrumb": list(chapter.breadcrumb) if chapter else [],
            "source_type": chapter.source_type if chapter else "",
            "physical_page_start": chapter.physical_page_start if chapter else None,
            "physical_page_end": chapter.physical_page_end if chapter else None,
            "spine": chapter.spine or None if chapter else None,
            "spine_index": chapter.spine_index if chapter else None,
            "href": chapter.href or None if chapter else None,
            "fragment": chapter.fragment or None if chapter else None,
        }
        records[evidence_id] = {**to_dict(ref), **semantic, "display_label": display_label(ref), "excerpt": excerpt, "source_hash": source_hash}
    payload = {"schema_version": 1, "evidence": records}
    targets = [root / ".onebookwiki" / "artifacts" / "evidence.json", root / "wiki" / "evidence.json"]
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return targets[-1]


def slugify(value: str) -> str:
    """Return stable readable ASCII slugs without collapsing non-Latin titles."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if normalized:
        return normalized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"unit-{digest}"


def _claims(
    values: list[Claim],
    kind: str | None = None,
    slugs: dict[int, str] | None = None,
    evidence_by_id: dict[str, EvidenceRef] | None = None,
) -> str:
    if not values:
        return "（暂无经过验证的内容。）"
    evidence_by_id = evidence_by_id or {}
    lines = []
    for index, item in enumerate(values):
        text = _replace_inline_citations(item.text or "（未命名条目）", evidence_by_id)
        if kind and slugs is not None:
            text = f"[{_replace_inline_citation_labels(item.text or '（未命名条目）', evidence_by_id)}]({kind}/{slugs[index]}.md)"
        inline_ids = _inline_evidence_ids(item.text)
        remaining_ids = tuple(item_id for item_id in item.evidence_ids if item_id not in inline_ids)
        lines.append(f"- {text}{_citation_list(remaining_ids, evidence_by_id)}")
    return "\n".join(lines)


def _entity_records(values: list[Claim]) -> list[tuple[int, Claim, str]]:
    """Assign deterministic, collision-free slugs to entity claims."""
    records: list[tuple[int, Claim, str]] = []
    used: set[str] = set()
    for index, claim in enumerate(values):
        base = slugify(claim.text or f"item-{index + 1}")
        slug = base
        suffix = 2
        while slug in used:
            slug = f"{base}-{suffix}"
            suffix += 1
        used.add(slug)
        records.append((index, claim, slug))
    return records


def _raw_source(chapter: ChapterInterpretation, root: Path) -> str | None:
    for ref in chapter.evidence:
        candidate = root / ref.source_path
        if candidate.is_file():
            return ref.source_path
    raw_dir = root / "raw" / "chapters"
    for candidate in sorted(raw_dir.glob("*.md")) if raw_dir.is_dir() else []:
        if re.match(rf"^{chapter.chapter:02d}[-_]", candidate.name):
            return candidate.relative_to(root).as_posix()
    return None


def _raw_metadata(prefix: str, raw_path: str | None) -> str:
    if not raw_path:
        return ""
    label = Path(raw_path).stem.split("-", 1)[-1].replace("-", " ")
    return f"> Raw: [{label}]({prefix}{raw_path})\n"


def render_chapter(
    chapter: ChapterInterpretation,
    root: Path,
    evidence_by_id: dict[str, EvidenceRef] | None = None,
) -> Path:
    target = root / "wiki" / "chapters" / f"{chapter.chapter:02d}-{slugify(chapter.title)}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    raw_path = _raw_source(chapter, root)
    evidence_by_id = evidence_by_id or {ref.evidence_id: ref for ref in chapter.evidence}
    evidence = "\n".join(
        f"- {_citation(ref)} · [{ref.source_path}:{ref.start_line}-{ref.end_line}]"
        f"(../../{ref.source_path}#L{ref.start_line}-L{ref.end_line})"
        for ref in chapter.evidence
    ) or "- （暂无可验证证据。）"
    raw_line = _raw_metadata("../../", raw_path)
    pages = ""
    if chapter.physical_page_start is not None:
        end = chapter.physical_page_end or chapter.physical_page_start
        pages = f"\n> Pages: {chapter.physical_page_start}-{end}"
    epub_location = ""
    if chapter.href or chapter.spine or chapter.spine_index is not None:
        href = f"{chapter.href}{f'#{chapter.fragment}' if chapter.fragment else ''}" if chapter.href else ""
        spine = f"Spine {chapter.spine_index}" if chapter.spine_index is not None else chapter.spine
        epub_location = f"\n> EPUB Location: {' · '.join(item for item in (spine, href) if item)}"
    breadcrumb = " › ".join(chapter.breadcrumb)
    part = f"\n> Part: {chapter.part}/{chapter.part_count}" if chapter.part_count > 1 else ""
    content = f"""# {_chapter_label(chapter)}

> Sources: {raw_path or _chapter_label(chapter)}
{raw_line}> Book: [Book](../book.md)
> Chapter: {chapter.chapter}
> Source Unit: {chapter.source_unit_id}
> Breadcrumb: {breadcrumb}
> Type: {chapter.source_type or 'reading_unit'}{pages}{epub_location}{part}
> Updated: {date.today().isoformat()}

## Chapter Purpose

{_replace_inline_citations(chapter.purpose or '证据不足以判断。', evidence_by_id)}

## Executive Summary

{_replace_inline_citations(chapter.executive_summary or '证据不足以判断。', evidence_by_id)}

## Core Thesis

{_replace_inline_citations(chapter.core_thesis or '证据不足以判断。', evidence_by_id)}

## Argument Map

{_replace_inline_citations(chapter.argument_map or '证据不足以判断。', evidence_by_id)}

## Key Concepts

{chr(10).join(f'- {_replace_inline_citations(item, evidence_by_id)}' for item in chapter.key_concepts) or '- （暂无。）'}

## Evidence and Examples

{_claims(chapter.evidence_examples, evidence_by_id=evidence_by_id)}

## Important Quotations

{_claims(chapter.important_quotations, evidence_by_id=evidence_by_id)}

## Relation to Previous Chapters

{_replace_inline_citations(chapter.relation_to_previous or '证据不足以判断。', evidence_by_id)}

## Relation to Following Chapters

{_replace_inline_citations(chapter.relation_to_following or '证据不足以判断。', evidence_by_id)}

## Cross-Chapter Connections

{_claims(chapter.cross_chapter_connections, evidence_by_id=evidence_by_id)}

## Open Questions

{chr(10).join(f'- {_replace_inline_citations(item, evidence_by_id)}' for item in chapter.open_questions) or '- （暂无。）'}

## Review Questions

{chr(10).join(f'- {_replace_inline_citations(item, evidence_by_id)}' for item in chapter.review_questions) or '- （暂无。）'}

## Evidence Register

{evidence}
"""
    target.write_text(content, encoding="utf-8")
    return target


def _entity_page(
    root: Path,
    book: BookSynthesis,
    claim: Claim,
    kind: str,
    slug: str,
    chapters: list[ChapterInterpretation],
) -> Path:
    target = root / "wiki" / kind / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    evidence_by_id = _evidence_index(root, chapters)
    chapter_numbers = sorted({evidence_by_id[item].chapter for item in claim.evidence_ids if item in evidence_by_id})
    chapter_links = []
    for number in chapter_numbers:
        chapter = next((item for item in chapters if item.chapter == number), None)
        if chapter:
            chapter_links.append(f"- {_chapter_link(chapter, '../')}")
    section_title = {"themes": "Theme", "concepts": "Concept", "arguments": "Claim"}.get(kind, "Entry")
    target.write_text(
        f"""# {_replace_inline_citation_labels(claim.text or slug, evidence_by_id)}

> Book: [{book.title}](../book.md)
> Updated: {date.today().isoformat()}
> Type: {section_title}

## {section_title}

{_replace_inline_citations(claim.text or '证据不足以判断。', evidence_by_id)}{_citation_list(tuple(item for item in claim.evidence_ids if item not in _inline_evidence_ids(claim.text)), evidence_by_id)}

## Qualification

{claim.qualification or 'No additional qualification was recorded.'}

## Evidence Chapters

{chr(10).join(chapter_links) or '- （暂无可链接的章节证据。）'}

## Sources

{'; '.join(_citation(evidence_by_id[item]) for item in claim.evidence_ids if item in evidence_by_id) or '（暂无。）'}
""",
        encoding="utf-8",
    )
    return target


def _linked_claims(
    values: list[Claim],
    kind: str,
    evidence_by_id: dict[str, EvidenceRef],
) -> tuple[str, list[tuple[int, Claim, str]]]:
    records = _entity_records(values)
    slugs = {index: slug for index, _, slug in records}
    return _claims(values, kind, slugs, evidence_by_id), records


def render_book(book: BookSynthesis, chapters: list[ChapterInterpretation], root: Path) -> list[Path]:
    chapters = _hydrate_legacy_locators(chapters, root)
    wiki = root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    first_raw = next((_raw_source(chapter, root) for chapter in chapters if _raw_source(chapter, root)), None)
    evidence_by_id = _evidence_index(root, chapters)

    for chapter in chapters:
        generated.append(render_chapter(chapter, root, evidence_by_id))

    entity_records: dict[str, list[tuple[int, Claim, str]]] = {}
    linked_sections: dict[str, str] = {}
    for kind, values in (("themes", book.themes), ("concepts", book.concepts), ("arguments", book.arguments)):
        linked, records = _linked_claims(values, kind, evidence_by_id)
        linked_sections[kind] = linked
        entity_records[kind] = records
        for _, claim, slug in records:
            generated.append(_entity_page(root, book, claim, kind, slug, chapters))

    rows = "\n".join(
        f"| {' › '.join(item.breadcrumb) or item.source_type or '阅读单元'} | {_chapter_link(item)} | "
        f"{_replace_inline_citations(item.executive_summary or '暂无摘要', evidence_by_id)} | generated | {date.today().isoformat()} |"
        for item in chapters
    )
    index = wiki / "index.md"
    index_raw = _raw_metadata("../", first_raw)
    index.write_text(
        f"""# OneBookWiki: {book.title}

> Sources: Generated from chapter interpretations
{index_raw}> Updated: {date.today().isoformat()}

## Book Metadata

- [Book metadata](book.md)
- Reading Status: complete
- Last Updated: {date.today().isoformat()}

## Reading Map

| Original structure | Reading unit | Summary | Status | Updated |
|---|---|---|---|---|
{rows or '| - | - | 暂无阅读单元 | - | - |'}

## Book-Level Thesis

{_replace_inline_citations(book.core_thesis or '证据不足以判断。', evidence_by_id)}

## Cross-Chapter Themes

{linked_sections['themes']}

## Concept Index

{linked_sections['concepts']}

## Argument Index

{linked_sections['arguments']}

## Review Hub

- [Review Questions](review/questions.md)
- [Flashcards](review/flashcards.md)
""",
        encoding="utf-8",
    )
    generated.append(index)

    book_page = wiki / "book.md"
    book_raw = _raw_metadata("../", first_raw)
    book_page.write_text(
        f"""# {book.title}

> Sources: Generated from the book's indexed chapters
{book_raw}> Updated: {date.today().isoformat()}
> Format: structured book interpretation
> Chapters: {len(chapters)}

## Overview

{_replace_inline_citations(book.overview or '证据不足以判断。', evidence_by_id)}

## Core Thesis

{_replace_inline_citations(book.core_thesis or '证据不足以判断。', evidence_by_id)}

## Argument Chain

{_replace_inline_citations(book.argument_chain or '证据不足以判断。', evidence_by_id)}

## Themes

{linked_sections['themes']}

## Concepts

{linked_sections['concepts']}

## Arguments

{linked_sections['arguments']}

## Terminology

{chr(10).join(f'- {_replace_inline_citations(item, evidence_by_id)}' for item in book.terminology) or '- （暂无。）'}

## Unresolved Questions

{chr(10).join(f'- {_replace_inline_citations(item, evidence_by_id)}' for item in book.unresolved_questions) or '- （暂无。）'}

[Open the reading map](index.md).
""",
        encoding="utf-8",
    )
    generated.append(book_page)

    review = wiki / "review"
    review.mkdir(parents=True, exist_ok=True)
    questions = [(chapter, question) for chapter in chapters for question in chapter.review_questions]
    questions_text = "\n".join(
        f"- {_replace_inline_citations(question, evidence_by_id)} "
        f"({_chapter_link(chapter, '../')})"
        for chapter, question in questions
    )
    (review / "questions.md").write_text(
        f"# Review Questions\n\n> Sources: Chapter interpretations\n> Updated: {date.today().isoformat()}\n\n{questions_text or '- （暂无。）'}\n",
        encoding="utf-8",
    )
    generated.append(review / "questions.md")
    flashcards = "\n".join(
        f"| {_replace_inline_citations(question, evidence_by_id)} | "
        f"{_replace_inline_citations(chapter.executive_summary or '参见章节解读。', evidence_by_id)} | "
        f"{_chapter_link(chapter, '../')} |"
        for chapter, question in questions
    )
    (review / "flashcards.md").write_text(
        f"# Flashcards: {book.title}\n\n| Front | Back | Source |\n|---|---|---|\n{flashcards or '| - | 暂无 | - |'}\n",
        encoding="utf-8",
    )
    generated.append(review / "flashcards.md")

    chapter_ids = [f"chapter-{item.chapter:02d}" for item in chapters]
    source = _source_metadata(root)
    source_structure = dict(source.get("source_structure") or {})
    source_outline = list(source_structure.get("outline") or [])
    rendered_page_ids = set(chapter_ids)

    def resolve_outline(nodes: list[object]) -> list[dict]:
        """Keep the source tree, but only publish reading pages that were rendered."""
        resolved: list[dict] = []
        for raw_node in nodes:
            if not isinstance(raw_node, dict):
                continue
            node = dict(raw_node)
            node["pageIds"] = [
                page_id for page_id in node.get("pageIds", [])
                if isinstance(page_id, str) and page_id in rendered_page_ids
            ]
            node["children"] = resolve_outline(list(node.get("children") or []))
            resolved.append(node)
        return resolved

    source_outline = resolve_outline(source_outline)
    entity_pages = []
    pages = [
        {"id": "book", "title": book.title, "path": "book.md", "kind": "book", "relatedPages": ["index", *chapter_ids]},
        {"id": "index", "title": "Reading Map", "path": "index.md", "kind": "index", "relatedPages": ["book", *chapter_ids]},
    ]
    for chapter in chapters:
        page_id = f"chapter-{chapter.chapter:02d}"
        entity_ids = []
        for kind, records in entity_records.items():
            for _, claim, slug in records:
                if any(item in claim.evidence_ids for item in {ref.evidence_id for ref in chapter.evidence}):
                    entity_ids.append(f"{kind[:-1]}-{slug}")
        pages.append({
            "id": page_id,
            "title": _chapter_label(chapter),
            "path": f"chapters/{chapter.chapter:02d}-{slugify(_chapter_label(chapter))}.md",
            "kind": "reading_unit",
            "chapter": chapter.chapter,
            "bookTitle": book.title,
            "sourceUnitId": chapter.source_unit_id,
            "sourceTitle": chapter.source_title or _chapter_label(chapter),
            "sourceKind": chapter.source_type,
            "breadcrumb": chapter.breadcrumb,
            "physicalPageStart": chapter.physical_page_start,
            "physicalPageEnd": chapter.physical_page_end,
            "spine": chapter.spine or None,
            "spineIndex": chapter.spine_index,
            "href": chapter.href or None,
            "fragment": chapter.fragment or None,
            "part": chapter.part,
            "partCount": chapter.part_count,
            "rawSources": sorted({ref.source_path for ref in chapter.evidence}),
            "relatedPages": ["book", "index", *entity_ids],
        })
    for kind, records in entity_records.items():
        singular = kind[:-1]
        for _, claim, slug in records:
            page_id = f"{singular}-{slug}"
            entity_pages.append(page_id)
            pages.append({"id": page_id, "title": _replace_inline_citation_labels(claim.text, evidence_by_id), "path": f"{kind}/{slug}.md", "kind": singular, "relatedPages": ["book", "index"]})
    pages.extend([
        {"id": "review-questions", "title": "Review Questions", "path": "review/questions.md", "kind": "review", "relatedPages": ["book", "index"]},
        {"id": "review-flashcards", "title": "Flashcards", "path": "review/flashcards.md", "kind": "review", "relatedPages": ["book", "index"]},
    ])
    sections = [{"id": "overview", "title": "Overview", "pages": ["book", "index"]}]
    for kind, records in entity_records.items():
        if records:
            sections.append({"id": kind, "title": kind.title(), "pages": [f"{kind[:-1]}-{slug}" for _, _, slug in records]})
    sections.append({"id": "review", "title": "Review", "pages": ["review-questions", "review-flashcards"]})
    structure = {
        "id": "book-wiki",
        "title": book.title,
        "description": _replace_inline_citation_labels(book.overview, evidence_by_id),
        "sourceOutline": source_outline,
        "pages": pages,
        "sections": sections,
    }
    structure_path = wiki / "structure.json"
    structure_path.write_text(json.dumps(structure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    generated.append(structure_path)
    generated.append(_write_evidence_index(root, chapters, book.title))
    log = wiki / "log.md"
    log.write_text(
        f"# Wiki Log\n\n## [{date.today().isoformat()}] build | {book.title}\n"
        f"- Disposition: Generated\n- Raw: raw/chapters/\n"
        f"- Updated: {len(chapters)} chapter page(s), {len(entity_pages)} entity page(s), book.md, index.md, review/, structure.json\n",
        encoding="utf-8",
    )
    generated.append(log)
    return generated


def render_artifacts(root: Path) -> list[Path]:
    book_path = root / ".onebookwiki" / "artifacts" / "book.json"
    chapter_dir = root / ".onebookwiki" / "artifacts" / "chapters"
    if not book_path.is_file():
        raise ValueError("book artifact is missing")
    book = book_from_dict(json.loads(book_path.read_text(encoding="utf-8")))
    chapters = [chapter_from_dict(json.loads(path.read_text(encoding="utf-8"))) for path in sorted(chapter_dir.glob("*.json"))]
    return render_book(book, _hydrate_legacy_locators(chapters, root), root)
