#!/usr/bin/env python3
"""Generate resumable chapter interpretations and book-level wiki pages."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from onebookwiki.generation import GenerationError, GenerationOptions, generate_chapters, generate_wiki, resume_generation, synthesize_book
from onebookwiki.rendering import render_artifacts


def options(args: argparse.Namespace) -> GenerationOptions:
    return GenerationOptions(provider=args.provider, model=args.model, language=args.language, max_input_tokens=args.max_input_tokens, max_output_tokens=args.max_output_tokens, rollup_size=args.rollup_size, retries=args.retries, run_id=getattr(args, "run_id", None), input_rate=args.input_rate, output_rate=args.output_rate, dry_run=args.dry_run)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("chapter", "book", "all", "resume"):
        command = sub.add_parser(name)
        command.add_argument("project_root")
        command.add_argument("--provider", default="none")
        command.add_argument("--model")
        command.add_argument("--language", default="zh-CN")
        command.add_argument("--max-input-tokens", type=int, default=12000)
        command.add_argument("--max-output-tokens", type=int, default=1800)
        command.add_argument("--rollup-size", type=int, default=4)
        command.add_argument("--retries", type=int, default=1)
        command.add_argument("--run-id")
        command.add_argument("--input-rate", type=float)
        command.add_argument("--output-rate", type=float)
        command.add_argument("--dry-run", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("project_root")
    args = parser.parse_args(argv[1:])
    root = Path(args.project_root).resolve()
    try:
        if args.command == "status":
            from onebookwiki.checkpoints import CheckpointStore
            store = CheckpointStore.latest(root)
            print(json.dumps(store.data if store else {"status": "none"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "chapter":
            store = generate_chapters(root, options(args))
        elif args.command == "book":
            store = synthesize_book(root, options(args))
        elif args.command == "resume":
            store = resume_generation(root, options(args), args.run_id)
        else:
            store = generate_wiki(root, options(args))
        print(f"run: {store.run_id}")
        if args.dry_run:
            print(json.dumps(store.data, ensure_ascii=False, indent=2))
            return 0
        files = render_artifacts(root)
        print(f"rendered {len(files)} wiki file(s)")
        return 0
    except (GenerationError, ValueError, OSError) as error:
        print(f"generation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
