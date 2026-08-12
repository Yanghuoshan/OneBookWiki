#!/usr/bin/env python3
"""Query book evidence (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.query_book.main.
For new usage, prefer the installed entry point: onebookwiki-query
"""
from __future__ import annotations

import sys

from onebookwiki.cli.query_book import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
