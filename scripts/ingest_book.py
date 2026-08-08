#!/usr/bin/env python3
"""Incrementally index raw book chapters without calling an LLM."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from onebookwiki.chunking import chunk_text
from onebookwiki.index import LocalIndex
from onebookwiki.markdown import metadata, parse_document
from onebookwiki.providers import ProviderUnavailable, build_embedder
from onebookwiki.remote_index import CloudVectorIndex

CHAPTER_RE = re.compile(r"^(\d+)[-_]")


def chapter_number(path: Path) -> int | None:
    document = parse_document(path.read_text(encoding="utf-8"))
    value = metadata(document).get("chapter", "").strip()
    if value.isdigit():
        return int(value)
    match = CHAPTER_RE.match(path.name)
    return int(match.group(1)) if match else None


def index_project(root: Path, backend: str = "lexical") -> tuple[int, int, int]:
    raw_dir = root / "raw" / "chapters"
    if not raw_dir.is_dir():
        raise ValueError(f"no raw/chapters directory under {root}")
    index = LocalIndex(root)
    if index.manifest.embedding_backend != backend:
        index.manifest.embedding_backend = backend
        index.manifest.embedding_model = "none" if backend == "lexical" else "configured"
        index.manifest.chunks.clear()
        index.manifest.chapters.clear()
    removed = index.manifest.prune_missing_chapters(root)
    added = changed = reused = 0
    for path in sorted(raw_dir.glob("*.md")):
        chapter = chapter_number(path)
        if chapter is None:
            print(f"warning: cannot determine chapter number: {path.relative_to(root)}", file=sys.stderr)
            continue
        relative = path.relative_to(root)
        key = relative.as_posix()
        digest = index.manifest.chapter_hash(path)
        known = index.manifest.chapters.get(key)
        if known and known.get("content_hash") == digest:
            reused += 1
            continue
        chunks = chunk_text(path.read_text(encoding="utf-8"), key, chapter)
        old_ids, new_ids = index.update(relative, chapter, chunks)
        added += len(new_ids)
        changed += 1
        print(f"indexed chapter {chapter}: {len(chunks)} chunk(s), {len(old_ids)} replaced")
    index.save()
    if removed["paths"]:
        print(f"pruned {len(removed['paths'])} stale raw chapter(s)", file=sys.stderr)
    return changed, added, reused


def index_cloud(root: Path, provider: str = "bge-m3") -> tuple[int, int, int]:
    raw_dir = root / "raw" / "chapters"
    if not raw_dir.is_dir():
        raise ValueError(f"no raw/chapters directory under {root}")
    embedder = build_embedder(provider)
    index = CloudVectorIndex(root, embedder)
    removed = index.manifest.prune_missing_chapters(root)
    if removed["chunk_ids"]:
        vectors = index._load_vectors()
        for chunk_id in removed["chunk_ids"]:
            vectors.pop(chunk_id, None)
        index._save_vectors(vectors)
    changed = added = reused = 0
    for path in sorted(raw_dir.glob("*.md")):
        chapter = chapter_number(path)
        if chapter is None:
            print(f"warning: cannot determine chapter number: {path.relative_to(root)}", file=sys.stderr)
            continue
        relative = path.relative_to(root)
        key = relative.as_posix()
        digest = index.manifest.chapter_hash(path)
        known = index.manifest.chapters.get(key)
        identity = embedder.identity()
        if (
            known
            and known.get("content_hash") == digest
            and index.manifest.embedding_backend == str(identity.get("provider"))
            and index.manifest.embedding_model == str(identity.get("model"))
        ):
            reused += 1
            continue
        chunks = chunk_text(path.read_text(encoding="utf-8"), key, chapter)
        replaced, new_count = index.update_chapter(relative, chapter, chunks)
        changed += 1
        added += new_count
        print(f"embedded chapter {chapter}: {new_count} chunk(s), {replaced} replaced")
    if removed["paths"]:
        print(f"pruned {len(removed['paths'])} stale raw chapter(s)", file=sys.stderr)
    return changed, added, reused


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    index_parser = subcommands.add_parser("index", help="build or incrementally update a local index")
    index_parser.add_argument("project_root")
    index_parser.add_argument("--backend", default="bge-m3", choices=("lexical", "bge-m3", "modelscope"))
    args = parser.parse_args(argv[1:])
    root = Path(args.project_root).resolve()
    try:
        if args.backend in {"bge-m3", "modelscope"}:
            changed, added, reused = index_cloud(root, args.backend)
        else:
            changed, added, reused = index_project(root, args.backend)
    except (ValueError, ProviderUnavailable) as error:
        print(error, file=sys.stderr)
        return 1
    action = "embedded" if args.backend in {"bge-m3", "modelscope"} else "indexed"
    print(f"{action} complete: {changed} changed chapter(s), {added} new chunk(s), {reused} reused chapter(s)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
