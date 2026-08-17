"""Generate resumable chapter interpretations and book-level wiki pages."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from onebookwiki.generation import GenerationError, GenerationOptions, generate_chapters, generate_wiki as do_generate_wiki, resume_generation, synthesize_book, write_generation_snapshot
from onebookwiki.providers import CANONICAL_GENERATION_MAX_OUTPUT_TOKENS, ProviderUnavailable, build_embedder
from onebookwiki.rendering import render_artifacts
from onebookwiki.wiki_vector_index import build_wiki_vector_indexes


def options(args: argparse.Namespace) -> GenerationOptions:
    return GenerationOptions(provider=args.provider, model=args.model, language=args.language, max_input_tokens=args.max_input_tokens, max_output_tokens=args.max_output_tokens, rollup_size=args.rollup_size, retries=args.retries, run_id=getattr(args, "run_id", None), input_rate=args.input_rate, output_rate=args.output_rate, dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("chapter", "book", "all", "resume"):
        command = sub.add_parser(name)
        command.add_argument("project_root")
        command.add_argument("--provider", default="none")
        command.add_argument("--model")
        command.add_argument("--embedding-backend", choices=("bge-m3", "modelscope"), default="bge-m3")
        command.add_argument("--language", default="zh-CN")
        command.add_argument("--max-input-tokens", type=int, default=12000)
        command.add_argument("--max-output-tokens", type=int, default=CANONICAL_GENERATION_MAX_OUTPUT_TOKENS)
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
        generation_options = options(args)
        if not generation_options.dry_run:
            write_generation_snapshot(root, generation_options)
        if args.command == "chapter":
            store = generate_chapters(root, generation_options)
        elif args.command == "book":
            store = synthesize_book(root, generation_options)
        elif args.command == "resume":
            store = resume_generation(root, generation_options, args.run_id)
        else:
            store = do_generate_wiki(root, generation_options)
        print(f"run: {store.run_id}")
        if args.dry_run:
            print(json.dumps(store.data, ensure_ascii=False, indent=2))
            return 0
        files = render_artifacts(root)
        print(f"rendered {len(files)} wiki file(s)")
        layers = build_wiki_vector_indexes(root, build_embedder(args.embedding_backend))
        print("built rendered-wiki vector layers: " + ", ".join(
            f"{item.layer}: {item.documents} documents/{item.chunks} chunks" for item in layers
        ))
        return 0
    except (GenerationError, ProviderUnavailable, ValueError, OSError) as error:
        print(f"generation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
