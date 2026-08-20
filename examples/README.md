# Example Book

`sample-book/` is a small, complete OneBookWiki project. Index it with the dependency-free lexical backend, then check it:

```text
python scripts/ingest_book.py index examples/sample-book --backend lexical
python scripts/check_book.py examples/sample-book
```
