"""Report token usage and estimated generation cost."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from onebookwiki.ledger import summarize_usage


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root")
    parser.add_argument("--run")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])
    report = summarize_usage(Path(args.project_root).resolve(), args.run)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"records: {report['records']}")
    print(f"total cost (USD): {report['total_cost']:.6f}")
    print(f"unknown-cost records: {report['unknown_cost_records']}")
    for key, value in report["groups"].items():
        print(f"{key}: {value['total_tokens']} tokens, ${value['cost']:.6f}, {value['calls']} call(s)")
    for error in report["parse_errors"]:
        print(f"warning: {error}", file=sys.stderr)
    return 0 if not report["parse_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
