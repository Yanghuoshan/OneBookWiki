#!/usr/bin/env python3
"""Report generation cost (backward-compatible wrapper).

This script is a thin wrapper around onebookwiki.cli.report_cost.main.
For new usage, prefer the installed entry point: onebookwiki-cost
"""
from __future__ import annotations

import sys

from onebookwiki.cli.report_cost import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
