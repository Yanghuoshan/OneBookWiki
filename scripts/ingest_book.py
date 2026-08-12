#!/usr/bin/env python3
"""Index raw book chapters (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.ingest_book.main.
For new usage, prefer the installed entry point: onebookwiki-ingest
"""
from __future__ import annotations

import sys

from onebookwiki.cli.ingest_book import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
