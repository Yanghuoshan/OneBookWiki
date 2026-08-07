"""Append-only token usage and cost accounting for generation runs."""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _rate(value: float | None, env_name: str) -> float | None:
    if value is not None:
        return max(0.0, float(value))
    raw = os.getenv(env_name, "")
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


def cost_for(usage: dict[str, int] | None, input_rate: float | None, output_rate: float | None) -> float | None:
    if not usage or input_rate is None or output_rate is None:
        return None
    prompt = int(usage.get("prompt_tokens", 0))
    completion = int(usage.get("completion_tokens", 0))
    return prompt * input_rate / 1_000_000 + completion * output_rate / 1_000_000


def append_usage(root: Path, *, run_id: str, node_id: str, stage: str, attempt: int, provider: str, model: str, usage: dict[str, int] | None = None, estimated: bool = False, input_rate: float | None = None, output_rate: float | None = None, status: str = "completed", prompt: str = "", error_type: str | None = None, error_detail: str | None = None) -> dict[str, Any]:
    input_rate = _rate(input_rate, "ONEBOOKWIKI_INPUT_USD_PER_1M")
    output_rate = _rate(output_rate, "ONEBOOKWIKI_OUTPUT_USD_PER_1M")
    record: dict[str, Any] = {
        "run_id": run_id,
        "node_id": node_id,
        "stage": stage,
        "attempt": int(attempt),
        "provider": provider,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt_tokens": int((usage or {}).get("prompt_tokens", 0)),
        "completion_tokens": int((usage or {}).get("completion_tokens", 0)),
        "total_tokens": int((usage or {}).get("total_tokens", 0)),
        "estimated": bool(estimated),
        "input_rate": input_rate,
        "output_rate": output_rate,
        "currency": "USD",
        "estimated_cost": cost_for(usage, input_rate, output_rate),
        "status": status,
        "prompt_hash": prompt_hash(prompt) if prompt else "",
    }
    if error_type:
        record["error_type"] = error_type
    if error_detail:
        record["error_detail"] = " ".join(str(error_detail).split())[:1000]
    target = root / ".onebookwiki" / "usage.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_usage(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    target = root / ".onebookwiki" / "usage.jsonl"
    if not target.is_file():
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
            records.append(value)
        except (ValueError, TypeError) as exc:
            errors.append(f"usage.jsonl:{line_number}: {exc}")
    return records, errors


def summarize_usage(root: Path, run_id: str | None = None) -> dict[str, Any]:
    records, errors = read_usage(root)
    if run_id:
        records = [record for record in records if record.get("run_id") == run_id]
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "unknown_cost": 0, "estimated_calls": 0, "retries": 0, "failures": 0})
    for record in records:
        key = f"{record.get('stage', 'unknown')}:{record.get('provider', 'unknown')}:{record.get('model', 'unknown')}"
        group = groups[key]
        group["calls"] += 1
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            group[name] += int(record.get(name, 0) or 0)
        if record.get("estimated"):
            group["estimated_calls"] += 1
        if record.get("attempt", 1) > 1:
            group["retries"] += 1
        if record.get("status") == "failed":
            group["failures"] += 1
        if record.get("estimated_cost") is None:
            group["unknown_cost"] += 1
        else:
            group["cost"] += float(record["estimated_cost"])
    return {"run_id": run_id, "records": len(records), "parse_errors": errors, "groups": dict(groups), "total_cost": sum(item["cost"] for item in groups.values()), "unknown_cost_records": sum(item["unknown_cost"] for item in groups.values())}
