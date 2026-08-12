#!/usr/bin/env python3
"""Extract EPUB chapters (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.ingest_epub.main.
For new usage, prefer the installed entry point: onebookwiki-ingest-epub
"""
from __future__ import annotations

import sys

from onebookwiki.cli.ingest_epub import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
