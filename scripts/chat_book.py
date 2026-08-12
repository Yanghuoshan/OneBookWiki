#!/usr/bin/env python3
"""Chat with a book (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.chat_book.main.
For new usage, prefer the installed entry point: onebookwiki-chat
"""
from __future__ import annotations

import sys

from onebookwiki.cli.chat_book import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
