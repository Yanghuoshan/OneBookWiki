#!/usr/bin/env python3
"""Generate wiki artifacts (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.generate_wiki.main.
For new usage, prefer the installed entry point: onebookwiki-generate
"""
from __future__ import annotations

import sys

from onebookwiki.cli.generate_wiki import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
