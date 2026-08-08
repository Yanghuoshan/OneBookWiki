#!/usr/bin/env python3
"""Build a complete evidence-grounded book wiki from one PDF or EPUB."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from onebookwiki.generation import GenerationError, GenerationOptions, generate_wiki, resume_generation
from onebookwiki.importers import import_epub, import_pdf
from onebookwiki.providers import ProviderUnavailable
from onebookwiki.rendering import render_artifacts
from onebookwiki.checkpoints import CheckpointStore

# Reuse the established index implementations rather than shelling out to scripts.
from ingest_book import index_cloud, index_project
from check_book import main as check_book


def build_progress(message: str) -> None:
    print(f"[generate] {message}", file=sys.stderr, flush=True)


def generation_options(args: argparse.Namespace) -> GenerationOptions:
    return GenerationOptions(
        provider=args.provider,
        model=args.model,
        language=args.language,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        rollup_size=args.rollup_size,
        retries=args.retries,
        run_id=args.run_id,
        input_rate=args.input_rate,
        output_rate=args.output_rate,
        dry_run=args.dry_run,
        progress=build_progress,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="PDF or EPUB source file")
    parser.add_argument("project_root")
    parser.add_argument("--backend", choices=("lexical", "bge-m3", "modelscope"), default="bge-m3")
    parser.add_argument("--book-title")
    parser.add_argument("--pages-per-chapter", type=int)
    parser.add_argument("--chapter-count", type=int)
    parser.add_argument("--structure", choices=("auto", "bookmarks", "toc", "headings", "ranges"), default="auto")
    parser.add_argument("--structure-min-confidence", type=float, default=0.72)
    parser.add_argument("--structure-report")
    parser.add_argument("--max-unit-tokens", type=int, default=12000)
    parser.add_argument("--keep-front-matter", action="store_true")
    parser.add_argument("--force", action="store_true", help="replace existing imported raw chapters")
    parser.add_argument("--resume", action="store_true", help="resume the latest generation run")
    parser.add_argument("--run-id")
    parser.add_argument("--provider", default="none")
    parser.add_argument("--model")
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--max-input-tokens", type=int, default=12000)
    parser.add_argument("--max-output-tokens", type=int, default=1800)
    parser.add_argument("--rollup-size", type=int, default=4)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--input-rate", type=float)
    parser.add_argument("--output-rate", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-check", action="store_true")
    args = parser.parse_args(argv[1:])
    source = Path(args.source).resolve()
    root = Path(args.project_root).resolve()
    if not source.is_file():
        print(f"source not found: {source}", file=sys.stderr)
        return 1
    suffix = source.suffix.lower()
    if suffix not in {".pdf", ".epub"}:
        print("source must be a .pdf or .epub file", file=sys.stderr)
        return 1
    try:
        if args.resume and not args.force:
            print("[1/4] resume: preserving existing imported raw chapters")
        elif suffix == ".pdf":
            print("[1/4] importing PDF")
            report = Path(args.structure_report).resolve() if args.structure_report else None
            imported = import_pdf(
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
            )
            print(f"imported {len(imported)} raw chapter file(s)")
        else:
            print("[1/4] importing EPUB")
            imported = import_epub(source, root, args.book_title, args.force, args.keep_front_matter)
            print(f"imported {len(imported)} raw chapter file(s)")
        print(f"[2/4] indexing with {args.backend}")
        if args.backend in {"bge-m3", "modelscope"}:
            changed, added, reused = index_cloud(root, args.backend)
        else:
            changed, added, reused = index_project(root, args.backend)
        print(f"indexed: {changed} changed chapter(s), {added} new chunk(s), {reused} reused chapter(s)")
        print("[3/4] generating structured wiki artifacts")
        options = generation_options(args)
        build_progress(f"provider={options.provider}, model={options.model or 'default'}, retries={options.retries}")
        store = resume_generation(root, options, args.run_id) if args.resume else generate_wiki(root, options)
        print(f"run: {store.run_id}")
        if args.dry_run:
            planned = len(store.data.get("nodes", {}))
            print(f"dry run complete; {planned} generation node(s) planned and no wiki pages rendered")
            return 0
        print("[4/4] rendering Wikipedia-style Markdown")
        pages = render_artifacts(root)
        print(f"rendered {len(pages)} page(s) under {root / 'wiki'}")
        if not args.skip_check:
            build_progress("running final consistency check")
            status = check_book(["check_book.py", str(root), "--json"])
            build_progress(f"final consistency check completed with status {status}")
            if status:
                return status
        return 0
    except (GenerationError, ProviderUnavailable, ValueError, OSError) as error:
        print(f"build failed: {error}", file=sys.stderr)
        if isinstance(error, GenerationError):
            print("generation checkpoint was saved; correct the provider output or configuration, then rerun with --resume", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
