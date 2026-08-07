# Example Book

`sample-book/` is a small, complete OneBookWiki project. Index it with the dependency-free lexical backend, then retrieve bounded evidence:

```text
python scripts/ingest_book.py index examples/sample-book --backend lexical
python scripts/query_book.py examples/sample-book "How do notes preserve evidence?" --retrieval-only
python scripts/check_book.py examples/sample-book
```
