---
name: onebookwiki
description: "Use when interpreting, indexing, querying, reviewing, or linting one book with chapter-level evidence and retrieval-bounded context. Triggers: OneBookWiki, analyze this book, ingest a chapter, query this book, review a chapter, or lint the book wiki."
---

# OneBookWiki

Maintain a structured interpretation of one book. The human chooses the book and asks questions; the agent maintains durable chapter and book-level Markdown while a local retrieval index prevents repeated full-book context.

## Architecture

- `raw/chapters/` is immutable source text. Never silently rewrite an imported chapter.
- `wiki/` is compiled interpretation: `book.md`, `index.md`, ordered `chapters/`, and optional `themes/`, `concepts/`, `arguments/`, and `review/`.
- `.onebookwiki/` is derived retrieval state: manifest, hashes, chunks, and optional vectors. It may be rebuilt.
- Use `scripts/ingest_book.py` to index chapters incrementally, `scripts/query_book.py` to retrieve bounded evidence, and `scripts/check_book.py` to report structure/evidence/index health.

## Grounding invariant

Every load-bearing exact number, date, and direct quotation in `wiki/` must occur verbatim in the linked raw chapter. Locate evidence before writing it. Preserve the source spelling (`42K` remains `42K`). Keep conflicting interpretations and annotate them with `Status: Outdated` or `Status: Disputed`.

## Ingest

1. Initialize only missing directories/files on the first ingest; never overwrite existing work.
2. Store one bounded chapter in `raw/chapters/<number>-<slug>.md` with Book, Edition, Chapter, Pages/Location, Source, Collected, Published, and Format metadata.
3. Index the chapter with section-aware chunks, source line ranges, token counts, and content hashes. Reuse unchanged chunks; embed only changed chunks.
4. Compile the chapter page from retrieved evidence and update only affected theme/concept/argument/review pages. Process batches sequentially because index and log are shared state.
5. Update `wiki/index.md` and append `wiki/log.md`.

Do not summarize every chunk by default. Summarize at chapter/section level only when requested or when it creates a durable page. For a whole-book interpretation, use cached chapter summaries first, then retrieve raw chunks from selected chapters.

## Query and Review

Read `wiki/index.md` and search compiled pages first. Use semantic/lexical retrieval over `.onebookwiki/` only for missing evidence or raw text. Use `query_book.py` with a hard context budget, a small top-k, per-chapter quotas, and duplicate removal. For semantic coverage, `--retrieval hybrid` fuses lexical and vector candidates and applies deterministic local reranking before context assembly. Default queries are read-only and may run with `--retrieval-only` without any LLM tokens. `--generate` sends only the bounded evidence to an OpenAI-compatible generation endpoint; `--max-tokens` limits evidence and `--max-output-tokens` limits the answer separately. Cite chapter and line/page/location metadata, and state when evidence is insufficient. A saved review or archive is an explicit write operation and must be logged.

Never pass the entire book or unbounded conversation history to a model. For broad questions, retrieve chapter candidates first and expand only the relevant chapters. Deep research is opt-in and has a fixed iteration/context ceiling.

## Lint

Run `check_book.py`. Safe fixes are limited to unambiguous index/link drift. Evidence, interpretation, terminology, conflict, and RAG health findings are reports for human/agent judgment. Check chapter numbering/order, Raw ownership, Markdown links, raw inventory, source fidelity, manifest hashes, chunk line ranges, and provider/index compatibility. Never modify `raw/` automatically.
