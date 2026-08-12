"""Build a complete evidence-grounded book wiki from one supported source format."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from onebookwiki.generation import GenerationError, GenerationOptions, generate_wiki as do_generate_wiki, resume_generation
from onebookwiki.importers import ImportOptions, detect_source_type, import_document
from onebookwiki.providers import ProviderUnavailable
from onebookwiki.rendering import render_artifacts
from onebookwiki.checkpoints import CheckpointStore

# Use the established index implementations from the CLI package.
from onebookwiki.cli.ingest_book import index_cloud, index_project
from onebookwiki.cli.check_book import main as check_book


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


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="source file: PDF, EPUB, MOBI, AZW, AZW3, TXT, DOC, DOCX, HTML, or HTM")
    parser.add_argument("project_root")
    parser.add_argument("--backend", choices=("lexical", "bge-m3", "modelscope"), default="bge-m3")
    parser.add_argument("--book-title")
    parser.add_argument("--source-format", choices=("PDF", "EPUB", "MOBI", "AZW", "AZW3", "TXT", "DOC", "DOCX", "HTML"), help="override source format detection")
    parser.add_argument("--pages-per-chapter", type=int)
    parser.add_argument("--chapter-count", type=int)
    parser.add_argument("--structure", choices=("auto", "bookmarks", "toc", "headings", "ranges"), default="auto")
    parser.add_argument("--structure-min-confidence", type=float, default=0.72)
    parser.add_argument("--structure-report")
    parser.add_argument("--structure-manifest", "--manifest", dest="structure_manifest")
    parser.add_argument("--max-unit-tokens", type=int, default=12000)
    parser.add_argument("--pdf-postprocess", choices=("off", "auto", "strict"), default="auto")
    parser.add_argument("--pdf-ocr", choices=("off", "assist"), default="off")
    parser.add_argument("--pdf-ocr-dpi", type=int)
    parser.add_argument("--pdf-ocr-confidence", type=float)
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
    parser.add_argument("--retries", type=int, default=4)
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
    try:
        source_format = detect_source_type(source, args.source_format)
    except ValueError as error:
        print(f"source type error: {error}", file=sys.stderr)
        return 1
    pdf_only_requested = (
        args.pages_per_chapter is not None
        or args.chapter_count is not None
        or args.structure != "auto"
        or args.structure_min_confidence != 0.72
        or args.structure_report is not None
        or args.structure_manifest is not None
        or args.pdf_postprocess != "auto"
        or args.pdf_ocr != "off"
        or args.pdf_ocr_dpi is not None
        or args.pdf_ocr_confidence is not None
    )
    if source_format != "PDF" and pdf_only_requested:
        print("PDF structure, OCR, and post-processing options may only be used with PDF input", file=sys.stderr)
        return 1
    if args.keep_front_matter and source_format not in {"EPUB", "MOBI", "AZW", "AZW3"}:
        print("--keep-front-matter may only be used with EPUB or DRM-free Kindle input", file=sys.stderr)
        return 1
    try:
        if args.resume and not args.force:
            print("[1/4] resume: preserving existing imported raw chapters")
        else:
            print(f"[1/4] importing {source_format}")
            imported = import_document(
                source,
                root,
                ImportOptions(
                    title=args.book_title,
                    force=args.force,
                    keep_front_matter=args.keep_front_matter,
                    pages_per_chapter=args.pages_per_chapter,
                    chapter_count=args.chapter_count,
                    structure=args.structure,
                    structure_min_confidence=args.structure_min_confidence,
                    structure_report=Path(args.structure_report).resolve() if args.structure_report else None,
                    max_unit_tokens=args.max_unit_tokens,
                    postprocess=args.pdf_postprocess,
                    structure_manifest=Path(args.structure_manifest).resolve() if args.structure_manifest else None,
                    pdf_ocr=args.pdf_ocr,
                    pdf_ocr_dpi=args.pdf_ocr_dpi,
                    pdf_ocr_confidence=args.pdf_ocr_confidence,
                    source_format=args.source_format,
                ),
            )
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
        store = resume_generation(root, options, args.run_id) if args.resume else do_generate_wiki(root, options)
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
