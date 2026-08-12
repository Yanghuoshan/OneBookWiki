"""Extract an EPUB into immutable chapter Markdown files."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from onebookwiki.importers import import_epub


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub")
    parser.add_argument("project_root")
    parser.add_argument("--book-title")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-front-matter", action="store_true")
    args = parser.parse_args(argv[1:])
    source = Path(args.epub).resolve()
    root = Path(args.project_root).resolve()
    if not source.is_file():
        print(f"EPUB not found: {source}", file=sys.stderr)
        return 1
    try:
        files = import_epub(source, root, args.book_title, args.force, args.keep_front_matter)
    except (OSError, ValueError, KeyError) as error:
        print(f"EPUB import failed: {error}", file=sys.stderr)
        return 1
    for path in files:
        print(f"wrote {path.relative_to(root)}")
    print(f"imported {len(files)} EPUB chapter file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
