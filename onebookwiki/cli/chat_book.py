"""Ask a book through wiki pages, artifacts, and grounded RAG evidence."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from onebookwiki.answer_generation import generate_validated_answer
from onebookwiki.chat_retrieval import (
    ChatRetrievalError,
    annotate_validated_raw_context,
    resolve_validated_evidence,
)
from onebookwiki.index import LocalIndex
from onebookwiki.ledger import append_usage
from onebookwiki.providers import CANONICAL_GENERATION_MAX_OUTPUT_TOKENS, ProviderUnavailable, build_embedder
from onebookwiki.remote_index import CloudVectorIndex
from onebookwiki.retrieval import HybridRetriever, LexicalRetriever, rerank_results, search_project, vector_results
from onebookwiki.wiki_retrieval import (
    assemble_wiki_first_context,
    build_wiki_first_prompt,
    render_context_blocks,
    search_artifacts,
    search_wiki,
)


def raw_results(root: Path, args: argparse.Namespace):
    mode = args.retrieval or ("vector" if args.backend in {"bge-m3", "modelscope"} else "lexical")
    if mode == "lexical":
        return search_project(root, args.question, args.top_k, args.chapter)
    try:
        embedder = build_embedder(args.backend)
        vector_index = CloudVectorIndex(root, embedder)
        if mode == "vector":
            return vector_results(vector_index.search(args.question, args.top_k, args.chapter))
        lexical = LexicalRetriever(LocalIndex(root).load())
        hybrid = HybridRetriever(lexical, lambda query, limit, chapter: vector_results(vector_index.search(query, limit, chapter)))
        return hybrid.search(args.question, args.top_k, args.chapter, lexical_top_k=args.lexical_top_k, vector_top_k=args.vector_top_k, lexical_weight=args.lexical_weight, vector_weight=args.vector_weight, rrf_k=args.rrf_k)
    except ProviderUnavailable:
        if args.strict_hybrid:
            raise
        print("warning: vector retrieval unavailable; using lexical raw evidence", file=sys.stderr)
        return search_project(root, args.question, args.top_k, args.chapter)


def ask(root: Path, question: str, args: argparse.Namespace) -> int:
    args.question = question
    wiki = search_wiki(root, question, args.max_wiki_pages, args.chapter) if args.wiki else []
    artifacts = search_artifacts(root, question, args.max_artifacts, args.chapter) if args.artifacts else []
    raw = rerank_results(question, raw_results(root, args), args.rerank)
    context, chunks = assemble_wiki_first_context(wiki, artifacts, raw, args.max_tokens, args.max_wiki_pages, args.max_artifacts, args.max_raw_per_chapter)
    if not context:
        print("No wiki page, artifact, or indexed raw evidence matched.")
        return 0
    if args.retrieval_only:
        print("# Retrieved OneBookWiki Context\n")
        print(context)
        return 0
    selected_chunk_ids = {
        str(item.get("chunk_id", ""))
        for item in chunks
        if item.get("source_kind") == "raw" and str(item.get("chunk_id", ""))
    }
    raw_candidates = [
        item for item in raw
        if str(item.chunk.get("chunk_id", "")) in selected_chunk_ids
    ]
    evidence = resolve_validated_evidence(root, raw_candidates, limit=max(1, len(raw_candidates)))
    allowed_evidence_ids = annotate_validated_raw_context(chunks, evidence)
    if not allowed_evidence_ids:
        print("\nNo validated raw evidence was available for generation.", file=sys.stderr)
        return 1
    context = render_context_blocks(chunks)
    print("# Retrieved OneBookWiki Context\n")
    print(context)
    prompt = build_wiki_first_prompt(question, context, sorted(allowed_evidence_ids))
    try:
        validated = generate_validated_answer(
            prompt,
            allowed_evidence_ids,
            provider=args.provider,
            model=args.model,
            max_output_tokens=args.max_output_tokens,
        )
        response = validated.response
        append_usage(
            root,
            run_id="chat",
            node_id="chat",
            stage="chat_answer" if not validated.repair_count else "chat_answer_repair",
            attempt=1 + validated.repair_count,
            provider=args.provider,
            model=response.model or args.model or "unknown",
            usage=response.usage,
            estimated=response.estimated_usage,
            input_rate=args.input_rate,
            output_rate=args.output_rate,
            prompt=validated.prompt,
        )
    except (ProviderUnavailable, ChatRetrievalError) as error:
        print(f"\nGeneration unavailable: {error}", file=sys.stderr)
        return 1
    print("\n# Answer\n")
    print(response.text)
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--provider", default="none")
    parser.add_argument("--model")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--chapter", type=int)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--max-output-tokens", type=int, default=CANONICAL_GENERATION_MAX_OUTPUT_TOKENS)
    parser.add_argument("--max-wiki-pages", type=int, default=3)
    parser.add_argument("--max-artifacts", type=int, default=2)
    parser.add_argument("--max-raw-per-chapter", type=int, default=3)
    parser.add_argument("--no-wiki", dest="wiki", action="store_false")
    parser.add_argument("--no-artifact-context", dest="artifacts", action="store_false")
    parser.set_defaults(wiki=True, artifacts=True)
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--retrieval", choices=("lexical", "vector", "hybrid"))
    parser.add_argument("--backend", default="bge-m3", choices=("lexical", "bge-m3", "modelscope"))
    parser.add_argument("--strict-hybrid", action="store_true")
    parser.add_argument("--lexical-top-k", type=int)
    parser.add_argument("--vector-top-k", type=int)
    parser.add_argument("--rerank", choices=("local", "none"), default="local")
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--lexical-weight", type=float, default=1.0)
    parser.add_argument("--vector-weight", type=float, default=1.0)
    parser.add_argument("--input-rate", type=float)
    parser.add_argument("--output-rate", type=float)
    args = parser.parse_args(argv[1:])
    root = Path(args.project_root).resolve()
    if not args.question and not args.interactive:
        parser.error("question is required unless --interactive is used")
    if args.question:
        return ask(root, args.question, args)
    while True:
        try:
            question = input("book> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in {"exit", "quit"}:
            return 0
        if question:
            status = ask(root, question, args)
            if status:
                return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
