"""Retrieve bounded, cited book evidence and optionally answer with an LLM."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from onebookwiki.chunking import count_tokens
from onebookwiki.context import assemble_context
from onebookwiki.index import LocalIndex
from onebookwiki.providers import (
    GenerationConfig,
    ProviderUnavailable,
    build_embedder,
    build_grounded_prompt,
    generate,
)
from onebookwiki.remote_index import CloudVectorIndex
from onebookwiki.retrieval import (
    HybridRetriever,
    LexicalRetriever,
    Result,
    chunk_key,
    rerank_results,
    search_project,
    vector_results,
)


def _effective_context(
    results: list[Result],
    question: str,
    max_tokens: int,
    max_per_chapter: int,
    output_tokens: int,
    generation_config: GenerationConfig,
) -> tuple[str, list[dict]]:
    """Reserve provider prompt/output capacity without relaxing max_tokens."""
    budget = max(1, max_tokens)
    if generation_config.context_window:
        prompt_overhead = count_tokens(build_grounded_prompt(question, ""))
        budget = min(budget, generation_config.context_window - output_tokens - prompt_overhead)
        budget = max(0, budget)
    context, chunks = assemble_context(results, budget, max_per_chapter)
    if not generation_config.context_window:
        return context, chunks
    available = max(1, generation_config.context_window - output_tokens)
    while chunks and count_tokens(build_grounded_prompt(question, context)) > available:
        keep = {chunk_key(item) for item in chunks[:-1]}
        reduced = [result for result in results if chunk_key(result.chunk) in keep]
        context, chunks = assemble_context(reduced, budget, max_per_chapter)
    return context, chunks


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root")
    parser.add_argument("query")
    parser.add_argument("--chapter", type=int)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--lexical-top-k", type=int)
    parser.add_argument("--vector-top-k", type=int)
    parser.add_argument("--max-tokens", type=int, default=8000, help="hard retrieved-evidence token budget")
    parser.add_argument("--max-per-chapter", type=int, default=3)
    parser.add_argument("--retrieval-only", action="store_true", help="do not call an LLM")
    parser.add_argument("--generate", action="store_true", help="answer from retrieved evidence")
    parser.add_argument("--strict-generation", action="store_true", help="fail if answer generation is unavailable")
    parser.add_argument("--strict-hybrid", action="store_true", help="fail instead of falling back if vector retrieval is unavailable")
    parser.add_argument("--provider", help="LLM provider name (or ONEBOOKWIKI_LLM_PROVIDER)")
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--rerank", choices=("local", "none"), default="local")
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--lexical-weight", type=float, default=1.0)
    parser.add_argument("--vector-weight", type=float, default=1.0)
    parser.add_argument("--retrieval", choices=("lexical", "vector", "hybrid"), help="retrieval strategy")
    parser.add_argument("--backend", default="bge-m3", choices=("lexical", "bge-m3", "modelscope"), help="embedding backend for vector retrieval")
    args = parser.parse_args(argv[1:])
    root = Path(args.project_root).resolve()
    mode = "vector" if args.backend in {"bge-m3", "modelscope"} else "lexical"
    if args.retrieval_only:
        args.generate = False
    retrieval_mode = getattr(args, "retrieval", None) or mode
    try:
        if retrieval_mode == "lexical":
            results = search_project(root, args.query, args.top_k, args.chapter)
        else:
            if retrieval_mode == "hybrid":
                lexical = LexicalRetriever(LocalIndex(root).load())
            embedder = build_embedder(args.backend)
            vector_index = CloudVectorIndex(root, embedder)
            if retrieval_mode == "vector":
                results = vector_results(vector_index.search(args.query, args.top_k, args.chapter))
            else:
                hybrid = HybridRetriever(
                    lexical,
                    lambda query, limit, chapter: vector_results(vector_index.search(query, limit, chapter)),
                )
                results = hybrid.search(
                    args.query,
                    args.top_k,
                    args.chapter,
                    lexical_top_k=args.lexical_top_k,
                    vector_top_k=args.vector_top_k,
                    lexical_weight=args.lexical_weight,
                    vector_weight=args.vector_weight,
                    rrf_k=args.rrf_k,
                )
    except ProviderUnavailable as error:
        if retrieval_mode == "hybrid" and not args.strict_hybrid:
            print("warning: vector retrieval unavailable; using lexical evidence only", file=sys.stderr)
            results = search_project(root, args.query, args.top_k, args.chapter)
        else:
            print(error, file=sys.stderr)
            return 1
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if not results:
        print("No indexed evidence matched. Run `onebookwiki-ingest index <project-root>` or broaden the query.")
        return 0
    results = rerank_results(args.query, results, args.rerank)
    generation_config = GenerationConfig.from_env(args.provider, args.model)
    if args.generate:
        context, chunks = _effective_context(
            results,
            args.query,
            args.max_tokens,
            args.max_per_chapter,
            args.max_output_tokens,
            generation_config,
        )
    else:
        context, chunks = assemble_context(results, args.max_tokens, args.max_per_chapter)
    print("# Retrieved Evidence\n")
    print(f"{len(chunks)} chunk(s), bounded to {args.max_tokens} tokens\n")
    print(context)
    if args.generate:
        prompt = build_grounded_prompt(args.query, context)
        try:
            answer = generate(prompt, args.provider, args.model, args.max_output_tokens)
        except ProviderUnavailable as error:
            print(f"\nGeneration unavailable: {error}", file=sys.stderr)
            if args.strict_generation:
                return 1
        else:
            print("\n# Answer\n")
            print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
