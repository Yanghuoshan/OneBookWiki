#!/usr/bin/env python3
"""Extract a PDF into immutable chapter Markdown files."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from onebookwiki.importers import extract_pdf_pages, import_pdf, pdf_chapter_ranges, slugify

# Kept as public aliases for scripts and existing callers.
extract = extract_pdf_pages
chapter_ranges = pdf_chapter_ranges


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("project_root")
    parser.add_argument("--pages-per-chapter", type=int)
    parser.add_argument("--chapter-count", type=int)
    parser.add_argument("--book-title")
    parser.add_argument("--structure", choices=("auto", "bookmarks", "toc", "headings", "ranges"), default="auto")
    parser.add_argument("--structure-min-confidence", type=float, default=0.72)
    parser.add_argument("--structure-report")
    parser.add_argument("--structure-manifest", "--manifest", dest="structure_manifest")
    parser.add_argument("--max-unit-tokens", type=int, default=12000)
    parser.add_argument("--pdf-postprocess", choices=("off", "auto", "strict"), default="auto")
    parser.add_argument("--pdf-ocr", choices=("off", "assist"), default="off")
    parser.add_argument("--pdf-ocr-dpi", type=int)
    parser.add_argument("--pdf-ocr-confidence", type=float)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv[1:])
    source = Path(args.pdf).resolve()
    root = Path(args.project_root).resolve()
    if not source.is_file():
        print(f"PDF not found: {source}", file=sys.stderr)
        return 1
    try:
        report = Path(args.structure_report).resolve() if args.structure_report else None
        files = import_pdf(
            source,
            root,
            args.book_title,
            args.pages_per_chapter,
            args.chapter_count,
            args.force,
            structure=args.structure,
            structure_min_confidence=args.structure_min_confidence,
            structure_report=report,
            max_unit_tokens=args.max_unit_tokens,
            postprocess=args.pdf_postprocess,
            structure_manifest=Path(args.structure_manifest).resolve() if args.structure_manifest else None,
            pdf_ocr=args.pdf_ocr,
            pdf_ocr_dpi=args.pdf_ocr_dpi,
            pdf_ocr_confidence=args.pdf_ocr_confidence,
        )
    except (OSError, ValueError) as error:
        print(f"PDF import failed: {error}", file=sys.stderr)
        return 1
    for path in files:
        print(f"wrote {path.relative_to(root)}")
    print(f"imported {len(files)} PDF chapter file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
