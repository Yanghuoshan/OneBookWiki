"""Generate resumable chapter interpretations and book-level wiki pages."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from onebookwiki.evidence_registry import EvidenceRegistryError, register_project
from onebookwiki.generation import GenerationError, GenerationOptions, generate_chapters, generate_wiki as do_generate_wiki, resume_generation, synthesize_book, write_generation_snapshot
from onebookwiki.manifest import Manifest
from onebookwiki.providers import CANONICAL_GENERATION_MAX_OUTPUT_TOKENS, ProviderUnavailable, build_embedder
from onebookwiki.wiki_projection import WikiProjectionError, render_grounded_projection
from onebookwiki.wiki_vector_index import WikiVectorIndexError, build_wiki_vector_indexes
from server.database import publish_registry_snapshot


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
        command.add_argument("--embedding-backend", choices=("lexical", "bge-m3", "modelscope"), default="bge-m3")
        command.add_argument("--database", help="Grounded v2 SQLite database path")
        command.add_argument("--book-id", type=int, help="numeric book ID in the Grounded v2 database")
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
    conn: sqlite3.Connection | None = None
    try:
        release_command = args.command in {"all", "resume"}
        if release_command and not args.dry_run and (args.database is None or args.book_id is None):
            raise ValueError("--database and --book-id are required for a non-dry-run full release")
        if args.database is not None and args.book_id is not None:
            conn = sqlite3.connect(Path(args.database).resolve(), check_same_thread=False)
            conn.row_factory = sqlite3.Row
        generation_options = options(args)
        if not generation_options.dry_run:
            write_generation_snapshot(root, generation_options)
        if args.command in {"all", "resume"} and not generation_options.dry_run:
            snapshot = register_project(root)
            publish_registry_snapshot(conn, args.book_id, snapshot)
            manifest = Manifest.load(root)
            if manifest.book_revision_id != snapshot.book_revision_id:
                raise GenerationError("generation manifest does not match the registered book revision")
        if args.command == "chapter":
            store = generate_chapters(root, generation_options)
        elif args.command == "book":
            store = synthesize_book(root, generation_options)
        elif args.command == "resume":
            store = resume_generation(
                root,
                generation_options,
                args.run_id,
                knowledge_connection=conn,
                book_id=args.book_id,
            )
        elif args.command == "all":
            store = do_generate_wiki(
                root,
                generation_options,
                knowledge_connection=conn,
                book_id=args.book_id,
            )
        else:
            from onebookwiki.checkpoints import CheckpointStore
            latest = CheckpointStore.latest(root)
            print(json.dumps(latest.data if latest else {"status": "none"}, ensure_ascii=False, indent=2))
            return 0
        print(f"run: {store.run_id}")
        if args.dry_run:
            print(json.dumps(store.data, ensure_ascii=False, indent=2))
            return 0
        if args.command not in {"all", "resume"}:
            return 0
        expected_revision_id = Manifest.load(root).book_revision_id
        files = render_grounded_projection(
            conn, root, args.book_id, expected_revision_id=expected_revision_id
        )
        print(f"rendered {len(files)} Grounded v2 Wiki file(s)")
        if args.embedding_backend in {"bge-m3", "modelscope"}:
            layers = build_wiki_vector_indexes(
                conn,
                root,
                args.book_id,
                build_embedder(args.embedding_backend),
                expected_revision_id=expected_revision_id,
            )
            print("built grounded semantic vector layers: " + ", ".join(
                f"{item.layer}: {item.documents} documents/{item.chunks} chunks" for item in layers
            ))
        else:
            print("skipping grounded semantic vector layers because lexical backend has no semantic embedder")
        return 0
    except (GenerationError, ProviderUnavailable, EvidenceRegistryError, WikiProjectionError, WikiVectorIndexError, ValueError, OSError) as error:
        print(f"generation failed: {error}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
