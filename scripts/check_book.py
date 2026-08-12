#!/usr/bin/env python3
"""Check book consistency (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.check_book.main.
For new usage, prefer the installed entry point: onebookwiki-check
"""
from __future__ import annotations

import sys

from onebookwiki.cli.check_book import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
