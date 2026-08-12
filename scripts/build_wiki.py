#!/usr/bin/env python3
"""Build a complete evidence-grounded book wiki (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.build_wiki.main.
For new usage, prefer the installed entry point: onebookwiki-build
"""
from __future__ import annotations

import sys

from onebookwiki.cli.build_wiki import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
