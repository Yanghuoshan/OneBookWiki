"""Ask a book through wiki pages, artifacts, and grounded RAG evidence."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from onebookwiki.index import LocalIndex
from onebookwiki.ledger import append_usage
from onebookwiki.providers import ProviderUnavailable, build_embedder, generate_response
from onebookwiki.remote_index import CloudVectorIndex
from onebookwiki.retrieval import HybridRetriever, LexicalRetriever, rerank_results, search_project, vector_results
from onebookwiki.wiki_retrieval import assemble_wiki_first_context, build_wiki_first_prompt, search_artifacts, search_wiki


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
    print("# Retrieved OneBookWiki Context\n")
    print(context)
    if args.retrieval_only:
        return 0
    prompt = build_wiki_first_prompt(question, context)
    try:
        response = generate_response(prompt, args.provider, args.model, args.max_output_tokens)
        append_usage(root, run_id="chat", node_id="chat", stage="chat", attempt=1, provider=args.provider, model=response.model or args.model or "unknown", usage=response.usage, estimated=response.estimated_usage, input_rate=args.input_rate, output_rate=args.output_rate, prompt=prompt)
    except ProviderUnavailable as error:
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
    parser.add_argument("--max-output-tokens", type=int, default=512)
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
