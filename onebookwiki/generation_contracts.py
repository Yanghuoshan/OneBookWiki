"""Strict contracts for newly generated chapter, rollup, and book payloads."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping


class ContractError(ValueError):
    """A generated payload does not satisfy its stage contract."""


EVIDENCE_ID_RE = re.compile(r"^C[1-9]\d*E[1-9]\d*$")
CONFIDENCE_VALUES = {"high", "medium", "low"}
CLAIM_KEYS = {"text", "evidence_ids"}
CONFIDENT_CLAIM_KEYS = CLAIM_KEYS | {"confidence"}

CHAPTER_SCHEMA: dict[str, Any] = {
    "purpose": "string",
    "executive_summary": "string",
    "core_thesis": "string",
    "argument_map": "string",
    "key_concepts": ["string"],
    "observations": [{"text": "string", "evidence_ids": ["CnEn"]}],
    "quotations": [{"text": "exact quote", "evidence_ids": ["CnEn"]}],
    "relation_to_previous": "string",
    "relation_to_following": "string",
    "cross_chapter_connections": [{"text": "string", "evidence_ids": ["CnEn"]}],
    "open_questions": ["string"],
    "review_questions": ["string"],
    "claims": [{"text": "string", "evidence_ids": ["CnEn"], "confidence": "high|medium|low"}],
}
ROLLUP_SCHEMA: dict[str, Any] = {
    "summary": "string",
    "themes": [{"text": "string", "evidence_ids": ["CnEn"]}],
    "concepts": [{"text": "string", "evidence_ids": ["CnEn"]}],
    "arguments": [{"text": "string", "evidence_ids": ["CnEn"]}],
    "tensions": [{"text": "string", "evidence_ids": ["CnEn"]}],
}
BOOK_SCHEMA: dict[str, Any] = {
    "overview": "string",
    "core_thesis": "string",
    "argument_chain": "string",
    "themes": [{"text": "string", "evidence_ids": ["CnEn"], "kind": "interpretation", "confidence": "high|medium|low"}],
    "concepts": [{"text": "string", "evidence_ids": ["CnEn"]}],
    "arguments": [{"text": "string", "evidence_ids": ["CnEn"]}],
    "terminology": ["string"],
    "unresolved_questions": ["string"],
    "chapter_summaries": {"chapter-number": "string"},
}


def schema_for(stage: str, chapter: int | None = None) -> dict[str, Any]:
    """Return the model-owned schema rendered in prompts."""
    schema = {"chapter": CHAPTER_SCHEMA, "rollup": ROLLUP_SCHEMA, "book": BOOK_SCHEMA}.get(stage)
    if schema is None:
        raise ContractError(f"unknown structured stage: {stage}")
    rendered = json.loads(json.dumps(schema))
    if stage == "chapter" and chapter is not None:
        marker = f"C{chapter}E1"
        for key in ("observations", "quotations", "cross_chapter_connections", "claims"):
            rendered[key][0]["evidence_ids"] = [marker]
    return rendered


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def strict_json_object(text: str) -> dict[str, Any]:
    """Decode exactly one JSON object with no duplicate keys or trailing content."""
    if not isinstance(text, str) or not text.strip():
        raise ContractError("structured response is empty")
    decoder = json.JSONDecoder(object_pairs_hook=_pairs)
    try:
        source = text.lstrip()
        value, end = decoder.raw_decode(source)
    except (json.JSONDecodeError, ContractError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc
    if source[end:].strip():
        raise ContractError("trailing content after JSON object")
    if not isinstance(value, dict):
        raise ContractError("structured response must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], path: str) -> None:
    wanted = set(expected)
    actual = set(value)
    missing = sorted(wanted - actual)
    extra = sorted(actual - wanted)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise ContractError(f"{path} has invalid keys ({', '.join(details)})")


def _string(value: Any, path: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        requirement = "non-empty string" if nonempty else "string"
        raise ContractError(f"{path} must be a {requirement}")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{path} must be an array")
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]", nonempty=True)
    return value


def _claim(
    value: Any,
    path: str,
    allowed_ids: set[str],
    *,
    confidence: bool = False,
    kind: bool = False,
) -> set[str]:
    if not isinstance(value, dict):
        raise ContractError(f"{path} must be an object")
    expected = set(CONFIDENT_CLAIM_KEYS if confidence else CLAIM_KEYS)
    if kind:
        expected.add("kind")
    _exact_keys(value, expected, path)
    _string(value["text"], f"{path}.text", nonempty=True)
    ids = _string_list(value["evidence_ids"], f"{path}.evidence_ids")
    if not ids:
        raise ContractError(f"{path}.evidence_ids must contain at least one canonical ID")
    result: set[str] = set()
    for evidence_id in ids:
        if not EVIDENCE_ID_RE.fullmatch(evidence_id):
            raise ContractError(f"{path} has malformed evidence ID: {evidence_id}")
        if evidence_id not in allowed_ids:
            raise ContractError(f"{path} has unavailable evidence ID: {evidence_id}")
        result.add(evidence_id)
    if confidence and value["confidence"] not in CONFIDENCE_VALUES:
        raise ContractError(f"{path}.confidence must be high, medium, or low")
    if kind and value["kind"] != "interpretation":
        raise ContractError(f"{path}.kind must be interpretation")
    return result


def _claim_list(
    value: Any,
    path: str,
    allowed_ids: set[str],
    *,
    confidence: bool = False,
    kind: bool = False,
) -> set[str]:
    if not isinstance(value, list):
        raise ContractError(f"{path} must be an array")
    ids: set[str] = set()
    for index, item in enumerate(value):
        ids.update(_claim(item, f"{path}[{index}]", allowed_ids, confidence=confidence, kind=kind))
    return ids


def _normalized(text: str) -> str:
    return " ".join(text.split())


def validate_chapter_payload(
    value: dict[str, Any],
    *,
    chapter: int,
    chunks: list[dict[str, Any]],
) -> set[str]:
    """Validate one model-owned chapter payload and its quotations."""
    _exact_keys(value, CHAPTER_SCHEMA, "chapter")
    for key in ("purpose", "executive_summary", "core_thesis", "argument_map", "relation_to_previous", "relation_to_following"):
        _string(value[key], f"chapter.{key}")
    for key in ("key_concepts", "open_questions", "review_questions"):
        _string_list(value[key], f"chapter.{key}")
    allowed = {f"C{chapter}E{index}" for index in range(1, len(chunks) + 1)}
    ids: set[str] = set()
    ids.update(_claim_list(value["observations"], "chapter.observations", allowed))
    ids.update(_claim_list(value["cross_chapter_connections"], "chapter.cross_chapter_connections", allowed))
    ids.update(_claim_list(value["claims"], "chapter.claims", allowed, confidence=True))
    ids.update(_claim_list(value["quotations"], "chapter.quotations", allowed))
    by_id = {f"C{chapter}E{index}": str(chunk.get("text", "")) for index, chunk in enumerate(chunks, 1)}
    for index, quote in enumerate(value["quotations"]):
        quoted = _normalized(quote["text"])
        if not any(quoted and quoted in _normalized(by_id[evidence_id]) for evidence_id in quote["evidence_ids"]):
            raise ContractError(f"chapter.quotations[{index}] is not present in its cited source text")
    return ids


def validate_rollup_payload(value: dict[str, Any], *, allowed_ids: set[str]) -> set[str]:
    _exact_keys(value, ROLLUP_SCHEMA, "rollup")
    _string(value["summary"], "rollup.summary")
    ids: set[str] = set()
    for key in ("themes", "concepts", "arguments", "tensions"):
        ids.update(_claim_list(value[key], f"rollup.{key}", allowed_ids))
    return ids


def validate_book_payload(
    value: dict[str, Any],
    *,
    allowed_ids: set[str],
    chapters: set[int],
) -> set[str]:
    _exact_keys(value, BOOK_SCHEMA, "book")
    for key in ("overview", "core_thesis", "argument_chain"):
        _string(value[key], f"book.{key}")
    for key in ("terminology", "unresolved_questions"):
        _string_list(value[key], f"book.{key}")
    ids: set[str] = set()
    ids.update(_claim_list(value["themes"], "book.themes", allowed_ids, confidence=True, kind=True))
    ids.update(_claim_list(value["concepts"], "book.concepts", allowed_ids))
    ids.update(_claim_list(value["arguments"], "book.arguments", allowed_ids))
    summaries = value["chapter_summaries"]
    if not isinstance(summaries, dict):
        raise ContractError("book.chapter_summaries must be an object/map")
    actual: set[int] = set()
    for key, summary in summaries.items():
        if not isinstance(key, str) or not key.isdigit() or int(key) < 1:
            raise ContractError(f"invalid chapter_summaries key: {key}")
        actual.add(int(key))
        _string(summary, f"book.chapter_summaries.{key}", nonempty=True)
    if actual != chapters:
        raise ContractError(f"book.chapter_summaries must cover exactly {sorted(chapters)}")
    return ids


def collect_prompt_evidence_ids(items: Iterable[Mapping[str, Any]]) -> set[str]:
    """Collect the canonical IDs that prompts expose from input evidence blocks."""
    result: set[str] = set()
    for item in items:
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list):
            continue
        for record in evidence:
            if not isinstance(record, Mapping):
                continue
            direct = record.get("evidence_id")
            if isinstance(direct, str) and EVIDENCE_ID_RE.fullmatch(direct):
                result.add(direct)
            nested = record.get("evidence_ids", [])
            if isinstance(nested, list):
                result.update(
                    value for value in nested
                    if isinstance(value, str) and EVIDENCE_ID_RE.fullmatch(value)
                )
    return result


def collect_claim_ids(value: dict[str, Any]) -> set[str]:
    """Collect IDs from validated claim-bearing fields."""
    result: set[str] = set()
    for key in ("observations", "quotations", "cross_chapter_connections", "claims", "themes", "concepts", "arguments", "tensions"):
        items = value.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("evidence_ids"), list):
                result.update(str(entry) for entry in item["evidence_ids"])
    return result
