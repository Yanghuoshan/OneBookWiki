"""Deterministic Wikipedia-like Markdown rendering for validated artifacts."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .models import BookSynthesis, ChapterInterpretation, Claim, book_from_dict, chapter_from_dict


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "item"


def _claims(values: list[Claim], kind: str | None = None, slugs: dict[int, str] | None = None) -> str:
    if not values:
        return "（暂无经过验证的内容。）"
    lines = []
    for index, item in enumerate(values):
        text = item.text or "（未命名条目）"
        if kind and slugs is not None:
            text = f"[{text}]({kind}/{slugs[index]}.md)"
        evidence = f" (`{', '.join(item.evidence_ids)}`)" if item.evidence_ids else ""
        lines.append(f"- {text}{evidence}")
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


def render_chapter(chapter: ChapterInterpretation, root: Path) -> Path:
    target = root / "wiki" / "chapters" / f"{chapter.chapter:02d}-{slugify(chapter.title)}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    raw_path = _raw_source(chapter, root)
    evidence = "\n".join(
        f"- `{ref.evidence_id}`: [{ref.source_path}:{ref.start_line}-{ref.end_line}]"
        f"(../../{ref.source_path}#L{ref.start_line}-L{ref.end_line})"
        for ref in chapter.evidence
    ) or "- （暂无可验证证据。）"
    raw_line = _raw_metadata("../../", raw_path)
    content = f"""# Chapter {chapter.chapter}: {chapter.title}

> Sources: {raw_path or chapter.title}
{raw_line}> Book: [Book](../book.md)
> Chapter: {chapter.chapter}
> Updated: {date.today().isoformat()}

## Chapter Purpose

{chapter.purpose or '证据不足以判断。'}

## Executive Summary

{chapter.executive_summary or '证据不足以判断。'}

## Core Thesis

{chapter.core_thesis or '证据不足以判断。'}

## Argument Map

{chapter.argument_map or '证据不足以判断。'}

## Key Concepts

{chr(10).join(f'- {item}' for item in chapter.key_concepts) or '- （暂无。）'}

## Evidence and Examples

{_claims(chapter.evidence_examples)}

## Important Quotations

{_claims(chapter.important_quotations)}

## Relation to Previous Chapters

{chapter.relation_to_previous or '证据不足以判断。'}

## Relation to Following Chapters

{chapter.relation_to_following or '证据不足以判断。'}

## Cross-Chapter Connections

{_claims(chapter.cross_chapter_connections)}

## Open Questions

{chr(10).join(f'- {item}' for item in chapter.open_questions) or '- （暂无。）'}

## Review Questions

{chr(10).join(f'- {item}' for item in chapter.review_questions) or '- （暂无。）'}

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
    evidence_by_id = {ref.evidence_id: ref for chapter in chapters for ref in chapter.evidence}
    chapter_numbers = sorted({evidence_by_id[item].chapter for item in claim.evidence_ids if item in evidence_by_id})
    chapter_links = []
    for number in chapter_numbers:
        chapter = next((item for item in chapters if item.chapter == number), None)
        if chapter:
            chapter_links.append(f"- [Chapter {number}](../chapters/{number:02d}-{slugify(chapter.title)}.md)")
    section_title = {"themes": "Theme", "concepts": "Concept", "arguments": "Claim"}.get(kind, "Entry")
    target.write_text(
        f"""# {claim.text or slug}

> Book: [{book.title}](../book.md)
> Updated: {date.today().isoformat()}
> Type: {section_title}

## {section_title}

{claim.text or '证据不足以判断。'}

## Qualification

{claim.qualification or 'No additional qualification was recorded.'}

## Evidence Chapters

{chr(10).join(chapter_links) or '- （暂无可链接的章节证据。）'}

## Evidence IDs

{', '.join(f'`{item}`' for item in claim.evidence_ids) or '（暂无。）'}
""",
        encoding="utf-8",
    )
    return target


def _linked_claims(values: list[Claim], kind: str) -> tuple[str, list[tuple[int, Claim, str]]]:
    records = _entity_records(values)
    slugs = {index: slug for index, _, slug in records}
    return _claims(values, kind, slugs), records


def render_book(book: BookSynthesis, chapters: list[ChapterInterpretation], root: Path) -> list[Path]:
    wiki = root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    first_raw = next((_raw_source(chapter, root) for chapter in chapters if _raw_source(chapter, root)), None)

    for chapter in chapters:
        generated.append(render_chapter(chapter, root))

    entity_records: dict[str, list[tuple[int, Claim, str]]] = {}
    linked_sections: dict[str, str] = {}
    for kind, values in (("themes", book.themes), ("concepts", book.concepts), ("arguments", book.arguments)):
        linked, records = _linked_claims(values, kind)
        linked_sections[kind] = linked
        entity_records[kind] = records
        for _, claim, slug in records:
            generated.append(_entity_page(root, book, claim, kind, slug, chapters))

    rows = "\n".join(
        f"| {item.chapter} | [Chapter {item.chapter}](chapters/{item.chapter:02d}-{slugify(item.title)}.md) | "
        f"{item.executive_summary or '暂无摘要'} | generated | {date.today().isoformat()} |"
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

| # | Chapter | Summary | Status | Updated |
|---|---|---|---|---|
{rows or '| - | - | 暂无章节 | - | - |'}

## Book-Level Thesis

{book.core_thesis or '证据不足以判断。'}

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

{book.overview or '证据不足以判断。'}

## Core Thesis

{book.core_thesis or '证据不足以判断。'}

## Argument Chain

{book.argument_chain or '证据不足以判断。'}

## Themes

{linked_sections['themes']}

## Concepts

{linked_sections['concepts']}

## Arguments

{linked_sections['arguments']}

## Terminology

{chr(10).join(f'- {item}' for item in book.terminology) or '- （暂无。）'}

## Unresolved Questions

{chr(10).join(f'- {item}' for item in book.unresolved_questions) or '- （暂无。）'}

[Open the reading map](index.md).
""",
        encoding="utf-8",
    )
    generated.append(book_page)

    review = wiki / "review"
    review.mkdir(parents=True, exist_ok=True)
    questions = [(chapter, question) for chapter in chapters for question in chapter.review_questions]
    questions_text = "\n".join(f"- {question} ([Chapter {chapter.chapter}](../chapters/{chapter.chapter:02d}-{slugify(chapter.title)}.md))" for chapter, question in questions)
    (review / "questions.md").write_text(
        f"# Review Questions\n\n> Sources: Chapter interpretations\n> Updated: {date.today().isoformat()}\n\n{questions_text or '- （暂无。）'}\n",
        encoding="utf-8",
    )
    generated.append(review / "questions.md")
    flashcards = "\n".join(
        f"| {question} | {chapter.executive_summary or '参见章节解读。'} | [Chapter {chapter.chapter}](../chapters/{chapter.chapter:02d}-{slugify(chapter.title)}.md) |"
        for chapter, question in questions
    )
    (review / "flashcards.md").write_text(
        f"# Flashcards: {book.title}\n\n| Front | Back | Source |\n|---|---|---|\n{flashcards or '| - | 暂无 | - |'}\n",
        encoding="utf-8",
    )
    generated.append(review / "flashcards.md")

    chapter_ids = [f"chapter-{item.chapter:02d}" for item in chapters]
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
        pages.append({"id": page_id, "title": chapter.title, "path": f"chapters/{chapter.chapter:02d}-{slugify(chapter.title)}.md", "kind": "chapter", "chapter": chapter.chapter, "rawSources": sorted({ref.source_path for ref in chapter.evidence}), "relatedPages": ["book", "index", *entity_ids]})
    for kind, records in entity_records.items():
        singular = kind[:-1]
        for _, claim, slug in records:
            page_id = f"{singular}-{slug}"
            entity_pages.append(page_id)
            pages.append({"id": page_id, "title": claim.text, "path": f"{kind}/{slug}.md", "kind": singular, "relatedPages": ["book", "index"]})
    pages.extend([
        {"id": "review-questions", "title": "Review Questions", "path": "review/questions.md", "kind": "review", "relatedPages": ["book", "index"]},
        {"id": "review-flashcards", "title": "Flashcards", "path": "review/flashcards.md", "kind": "review", "relatedPages": ["book", "index"]},
    ])
    sections = [{"id": "overview", "title": "Overview", "pages": ["book", "index"]}]
    if chapter_ids:
        sections.append({"id": "chapters", "title": "Chapters", "pages": chapter_ids})
    for kind, records in entity_records.items():
        if records:
            sections.append({"id": kind, "title": kind.title(), "pages": [f"{kind[:-1]}-{slug}" for _, _, slug in records]})
    sections.append({"id": "review", "title": "Review", "pages": ["review-questions", "review-flashcards"]})
    structure = {"id": "book-wiki", "title": book.title, "description": book.overview, "pages": pages, "sections": sections}
    structure_path = wiki / "structure.json"
    structure_path.write_text(json.dumps(structure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    generated.append(structure_path)
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
    return render_book(book, chapters, root)
