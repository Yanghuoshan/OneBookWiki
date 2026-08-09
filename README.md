# OneBookWiki

OneBookWiki is a retrieval-first Agent Skill for structured interpretation of one long book. It combines an immutable chapter archive and readable Markdown analysis with a persistent local chunk index, so later questions retrieve only relevant evidence instead of sending the whole book to an LLM.

## Design

```text
raw/chapters/  ->  deterministic chunks + line metadata  ->  .onebookwiki/
      \->  wiki/chapters, themes, concepts, arguments, review
```

The raw chapter files are the source of truth. The Markdown wiki is the curated interpretation. The derived index can be rebuilt. The default vector backend is local `BAAI/bge-m3` through SentenceTransformers; use `--backend lexical` for dependency-free retrieval or `--backend modelscope` for the optional remote endpoint. Queries can also fuse lexical and vector candidates with deterministic reciprocal-rank fusion, then apply a local query-aware reranker before bounded context assembly.

## Local BGE-M3 embeddings and PDFs

Install the local embedding dependency, download `BAAI/bge-m3` once or point the configuration to a local model directory, then build the vector index. SentenceTransformers selects an available device by default; set `ONEBOOKWIKI_BGE_M3_DEVICE=cpu` or `cuda` to override it:

```text
pip install -e ".[local-embeddings]"
set ONEBOOKWIKI_BGE_M3_MODEL=D:/models/BAAI/bge-m3
set ONEBOOKWIKI_BGE_M3_DEVICE=cuda
python scripts/ingest_pdf.py "D:/workspace/deepwiki4book/Fou Ren - Aleksandr Zvezdov.pdf" D:/books/fou-ren --pages-per-chapter 25
python scripts/ingest_book.py index D:/books/fou-ren
python scripts/query_book.py D:/books/fou-ren "这本书的核心论点是什么？" --retrieval-only
```

BGE-M3 vectors are persisted at `<book>/.onebookwiki/vectors.json`; unchanged chapters are reused only when the raw content, chunk profile, backend, and configured model all match. The default chunk profile is `400/60/520` tokens for Chinese/CJK and `500/75/650` for English (target/overlap/hard maximum). Re-run the index command after changing the chunk profile, switching from ModelScope, or changing the model path so derived vectors are rebuilt with one consistent identity.

## ModelScope embeddings

To use the optional remote ModelScope backend instead, install the cloud integrations, set the ModelScope token outside source control, and select `--backend modelscope`:

```text
pip install -e ".[cloud]"
set MODELSCOPE_API_KEY=your-token                 # PowerShell: $env:MODELSCOPE_API_KEY="your-token"
python scripts/ingest_pdf.py "D:/workspace/deepwiki4book/Fou Ren - Aleksandr Zvezdov.pdf" D:/books/fou-ren --pages-per-chapter 25
python scripts/ingest_book.py index D:/books/fou-ren --backend modelscope
python scripts/query_book.py D:/books/fou-ren "这本书的核心论点是什么？" --backend modelscope --retrieval-only
# Hybrid retrieval: lexical candidates + ModelScope vectors, then local reranking.
python scripts/query_book.py D:/books/fou-ren "这本书的核心论点是什么？" --retrieval hybrid --generate --provider openai-compatible --max-output-tokens 512
```

The cloud backend sends changed chunk text to `https://api-inference.modelscope.cn/v1` using `Qwen/Qwen3-Embedding-8B`. Vectors are persisted at `<book>/.onebookwiki/vectors.json`; a chapter is reused only when its raw content, chunk profile, provider, and model all match. The token is read only from `MODELSCOPE_API_KEY` (or `ONEBOOKWIKI_EMBEDDING_API_KEY`) and is never stored in the manifest, logs, or source files. Copy `.env.example` for the variable names, but do not commit a real credential.

`ingest_pdf.py` preserves PDF page boundaries as Markdown headings. Its simple page-count splitting is deterministic but is not semantic chapter detection; pass `--pages-per-chapter` conservatively, then rename/split the resulting raw files after checking the book's table of contents.

PDF import first tries native bookmarks, printed contents, and page-top headings before it falls back to ranges; it never performs OCR. For a known unusual layout, pass `--structure-manifest manifests/pdf/<book>.json` (or `--manifest`) explicitly. A rules-only manifest contributes marker, row-format, title, and heading hints but still requires automatic TOC/heading alignment. Only a manifest with validated physical `units` selects the manual `manifest` method. The import snapshots an accepted manifest under `.onebookwiki/structure-manifest.json` and records matching provenance in both `source.json` and `structure-report.json`; a manifest merely present in the repository is never auto-loaded.

For PDFs that already contain an OCR-generated text layer, PDF import applies conservative, coordinate-based cleanup by default before it writes raw chapters: `--pdf-postprocess auto` removes corroborated page-edge headers/footers and isolated page labels, normalizes Unicode/spacing, repairs safe English line-wrap hyphens, restores paragraphs, and orders clear two-column pages. It does **not** run OCR, an LLM, a spelling corrector, Chinese segmentation, or Traditional/Simplified conversion. `analyse_pdf()` still derives the outline from the native extracted view, so this cleanup does not change chapter boundaries; the processed view is only used for raw/RAG text after those boundaries are fixed. Use `--pdf-postprocess off` to retain the unmodified text layer or `--pdf-postprocess strict` to retain a page if a conservative transformation would empty it. Complex tables, footnotes, sidebars, and mixed layouts are kept conservatively rather than inferred. Changing this setting changes raw chapter text, so reimport with `--force` before reindexing.

## EPUB/PDF import and generation

EPUB import uses only the Python standard library and preserves spine/href provenance:

```text
python scripts/ingest_epub.py path/to/book.epub path/to/project
python scripts/ingest_pdf.py path/to/book.pdf path/to/project --pages-per-chapter 25 --pdf-postprocess auto
python scripts/ingest_book.py index path/to/project --backend lexical
```

After indexing, a configured OpenAI-compatible provider can generate structured chapter interpretations and hierarchical book synthesis. The model returns JSON evidence references; the renderer creates deterministic Wikipedia-like Markdown pages and citations. The optional `cloud` install includes `json-repair`, so generation can repair common malformed model JSON (for example, missing commas) before validation. Responses that cannot be repaired still fail safely and leave a resumable failed checkpoint:

```text
python scripts/generate_wiki.py all path/to/project --provider openai-compatible --model gpt-4o-mini
python scripts/generate_wiki.py status path/to/project
python scripts/generate_wiki.py resume path/to/project
python scripts/report_cost.py path/to/project --json
python scripts/chat_book.py path/to/project "这本书的核心论点是什么？" --provider openai-compatible
```

Use `--dry-run` to build a bounded, resumable plan without calling a model. Checkpoints and generated JSON artifacts live under `.onebookwiki/`; usage is append-only in `usage.jsonl`. Set `ONEBOOKWIKI_INPUT_USD_PER_1M` and `ONEBOOKWIKI_OUTPUT_USD_PER_1M` to enable cost estimates. Missing provider usage or prices is reported as estimated/unknown rather than fabricated. Raw chapters remain the source of truth and are never replaced by generation.

## One-command build and wiki-first chat

The complete build can be run from one PDF or EPUB. It imports immutable raw chapters, builds the selected index, resumes or creates the structured generation run, renders Markdown, writes `wiki/structure.json` and `wiki/log.md`, and checks the result. During generation, per-node request, parse/repair, reuse, and completion updates are written to stderr; checker JSON remains clean on stdout:

```text
python scripts/build_wiki.py path/to/book.epub path/to/project --provider openai-compatible --model gpt-4o-mini
python scripts/build_wiki.py path/to/book.pdf path/to/project --pages-per-chapter 25 --provider openai-compatible --model gpt-4o-mini
python scripts/build_wiki.py path/to/book.epub path/to/project --resume --provider openai-compatible --model gpt-4o-mini
# Use --backend lexical for no local model, or --backend modelscope for the remote embedding service.
```

The resulting organization follows the wiki pattern:

```text
raw/chapters/                 immutable source
.onebookwiki/                 rebuildable chunks, vectors, artifacts, checkpoints, usage
wiki/book.md                  book overview
wiki/index.md                 chapter reading map and navigation
wiki/chapters/*.md            compiled chapter articles
wiki/structure.json           canonical page/section/related-page graph
wiki/log.md                   build/operation log
```

`chat_book.py` is wiki-first by default. It retrieves in this order: rendered wiki pages, structured JSON artifacts, then raw lexical/vector/hybrid evidence. Wiki text provides navigation and synthesis; raw evidence remains the factual authority:

```text
python scripts/chat_book.py path/to/project "这本书的核心论点是什么？" --retrieval hybrid --provider openai-compatible --model gpt-4o-mini
python scripts/chat_book.py path/to/project "列出第一章的关键概念" --retrieval-only
python scripts/chat_book.py path/to/project --interactive --provider openai-compatible --model gpt-4o-mini
```

Use `--no-wiki` or `--no-artifact-context` to narrow the source layers. Use `--max-wiki-pages`, `--max-artifacts`, `--max-raw-per-chapter`, and `--max-tokens` to bound context. `wiki/structure.json` uses page IDs, sections, paths and related pages so it can be adapted to a future wiki UI without changing the book domain model.

## Commands

```text
python scripts/ingest_book.py index path/to/book --backend lexical
python scripts/query_book.py path/to/book "What is the central argument?" --retrieval-only
python scripts/check_book.py path/to/book
python scripts/check_book.py path/to/book --json
python -m unittest discover -s tests -v
```

Use one Markdown/text file per chapter where possible. Do not ingest one giant book file: chapter boundaries enable incremental updates, chapter filtering, summaries, and smaller prompts. `query_book.py` enforces a context token budget, removes duplicate chunks, and limits the number of chunks per chapter. `--max-tokens` remains the hard retrieved-evidence budget; `--max-output-tokens` is a separate answer budget. Generation uses only the assembled evidence, cites chapter/source/line metadata, and must state when the evidence is insufficient. Set `ONEBOOKWIKI_LLM_CONTEXT_WINDOW` when the generation endpoint has a known prompt limit so evidence is reduced before the request.

## 前端阅读器

前端位于 `frontend/`，是一个独立的 React/Vite 阅读器。启动前，请先为目标书籍生成 `wiki/structure.json`、Markdown 页面和 `wiki/evidence.json`；书籍目录放在 `books/<book-id>/` 下。建议使用当前 Node.js LTS 版本。

在 PowerShell 中启动开发服务器：

```powershell
cd D:\workspace\deepwiki4book\onebookwiki\frontend
npm install
npm run dev -- --host 127.0.0.1
```

启动后在浏览器打开：

```text
http://127.0.0.1:5173/book                  # 默认书籍 books/zhenshi
http://127.0.0.1:5173/book/zhenshi          # 指定书籍
http://127.0.0.1:5173/book/aideduo          # 另一本已生成的书籍
http://127.0.0.1:5173/book/aideduo?page=... # 直接打开 structure.json 中的页面 ID
```

`<book-id>` 只能包含字母、数字、`_` 和 `-`，并且必须对应 `books/` 下的目录。开发服务器会从 `books/<book-id>/wiki/` 读取 JSON 和 Markdown 文件。

构建生产版本并在本机预览：

```powershell
cd D:\workspace\deepwiki4book\onebookwiki\frontend
npm run build
npm run preview -- --host 127.0.0.1
```

`npm run build` 只会生成 `frontend/dist/`，不会复制 `books/`。部署时还需要发布书籍的 `books/<book-id>/wiki/` 静态文件，并将 `/book`、`/book/<book-id>` 等页面请求重写到 `frontend/dist/index.html`；不要重写 `/book/<book-id>/wiki/...`，这些请求必须返回实际的 JSON 或 Markdown 文件。

更多路由和部署配置请参见 [`frontend/README.md`](frontend/README.md)。

## Skill

Copy `SKILL.md` and `references/` into an Agent Skills-compatible directory. The Skill defines Ingest, Query, Review, and Lint behavior; the CLI provides deterministic indexing and checking.
