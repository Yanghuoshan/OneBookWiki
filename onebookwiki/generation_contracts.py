"""Strict Grounded v2 draft contracts for model-owned generation output."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping


class ContractError(ValueError):
    """A generated Grounded v2 draft does not satisfy its contract."""


CONTRACT_VERSION = "grounded-v2"
EVIDENCE_REVISION_ID_RE = re.compile(r"^evr-[0-9a-f]{32}$")
DRAFT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
CONFIDENCE_VALUES = {"high", "medium", "low"}
STATEMENT_KINDS = {
    "factual",
    "definitional",
    "causal",
    "comparative",
    "interpretive",
    "normative",
    "attribution",
    "quotation",
}
SUPPORT_TYPES = {"positive", "negative", "qualifying", "attribution", "quotation"}
DOCUMENTARY_SCOPES = {"span", "section", "reading_unit", "rollup", "book"}
TARGET_TYPES = {"book", "chapter", "entity", "statement", "evidence"}
ABSTRACTIONS = {"concrete_detail", "local_generalization", "cross_scope_synthesis"}
COMPOSITION_KINDS = {
    "purpose",
    "summary",
    "thesis",
    "argument_map",
    "concept_profile",
    "overview",
    "core_thesis",
    "argument_chain",
    "chapter_summary",
    "rollup",
}

STATEMENT_SCHEMA: dict[str, Any] = {
    "draft_id": "string",
    "canonical_key": "string",
    "kind": "factual|definitional|causal|comparative|interpretive|normative|attribution|quotation",
    "subject": "string",
    "truth_condition": "string",
    "scope": {
        "documentary_scope": "span|section|reading_unit|rollup|book",
        "target_type": "book|chapter|entity|statement|evidence",
        "chapter": "integer|null",
    },
    "abstraction": "concrete_detail|local_generalization|cross_scope_synthesis",
    "qualification": "string|null",
    "confidence": "high|medium|low",
    "supports": [{
        "evidence_revision_id": "evr-<32 lowercase hex>",
        "support_type": "positive|negative|qualifying|attribution|quotation",
        "quote": "exact evidence body",
        "span_map": {"source_line_start": "integer", "source_line_end": "integer"},
    }],
}
COMPOSITION_SCHEMA: dict[str, Any] = {
    "draft_id": "string",
    "canonical_key": "string",
    "kind": "purpose|summary|thesis|argument_map|concept_profile|overview|core_thesis|argument_chain|chapter_summary|rollup",
    "members": [{
        "member_type": "statement|composition",
        "draft_id": "string",
        "role": "string",
    }],
    "rendering": {"text": "string", "span_map": "object"},
}
CHAPTER_SCHEMA: dict[str, Any] = {
    "statements": [STATEMENT_SCHEMA],
    "compositions": [COMPOSITION_SCHEMA],
}
ROLLUP_SCHEMA: dict[str, Any] = {
    "statements": [STATEMENT_SCHEMA],
    "compositions": [COMPOSITION_SCHEMA],
}
BOOK_SCHEMA: dict[str, Any] = {
    "statements": [STATEMENT_SCHEMA],
    "compositions": [COMPOSITION_SCHEMA],
}


def schema_for(stage: str, chapter: int | None = None) -> dict[str, Any]:
    """Return a JSON-safe copy of the model-owned v2 draft schema."""
    schema = {"chapter": CHAPTER_SCHEMA, "rollup": ROLLUP_SCHEMA, "book": BOOK_SCHEMA}.get(stage)
    if schema is None:
        raise ContractError(f"unknown structured stage: {stage}")
    return json.loads(json.dumps(schema, ensure_ascii=False))


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


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    return dict(value)


def _string(value: Any, path: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        requirement = "non-empty string" if nonempty else "string"
        raise ContractError(f"{path} must be a {requirement}")
    return value


def _nullable_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path, nonempty=True)


def _integer_or_null(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{path} must be a positive integer or null")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{path} must be an array")
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]", nonempty=True)
    return [str(item) for item in value]


def _span_map(value: Any, path: str) -> dict[str, int]:
    span = _object(value, path)
    _exact_keys(span, {"source_line_start", "source_line_end"}, path)
    start = _integer_or_null(span["source_line_start"], f"{path}.source_line_start")
    end = _integer_or_null(span["source_line_end"], f"{path}.source_line_end")
    if start is None or end is None or end < start:
        raise ContractError(f"{path} line range is invalid")
    return {"source_line_start": start, "source_line_end": end}


def _allowed_evidence_ids(
    chunks: Iterable[Mapping[str, Any]] | None,
    evidence: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
) -> tuple[set[str], dict[str, Any]]:
    records: dict[str, Any] = {}
    if isinstance(evidence, Mapping):
        records.update({str(key): value for key, value in evidence.items()})
    elif evidence is not None:
        for item in evidence:
            if isinstance(item, Mapping):
                identifier = item.get("evidence_revision_id")
                if isinstance(identifier, str):
                    records[identifier] = item
    if chunks is not None:
        for item in chunks:
            identifier = item.get("evidence_revision_id")
            if isinstance(identifier, str):
                records.setdefault(identifier, item)
    return set(records), records


def validate_support(
    value: Any,
    path: str,
    *,
    allowed_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    item = _object(value, path)
    _exact_keys(item, {"evidence_revision_id", "support_type", "quote", "span_map"}, path)
    evidence_id = _string(item["evidence_revision_id"], f"{path}.evidence_revision_id", nonempty=True)
    if not EVIDENCE_REVISION_ID_RE.fullmatch(evidence_id):
        raise ContractError(f"{path}.evidence_revision_id is not a Grounded v2 evidence revision")
    record = allowed_evidence.get(evidence_id)
    if record is None:
        raise ContractError(f"{path} references unavailable evidence revision")
    support_type = _string(item["support_type"], f"{path}.support_type", nonempty=True)
    if support_type not in SUPPORT_TYPES:
        raise ContractError(f"{path}.support_type is not controlled")
    quote = _string(item["quote"], f"{path}.quote", nonempty=True)
    expected_quote = record.get("quote") if isinstance(record, Mapping) else None
    if expected_quote is None:
        expected_quote = record.get("body") if isinstance(record, Mapping) else None
    if not isinstance(expected_quote, str) or quote != expected_quote:
        raise ContractError(f"{path}.quote must exactly match the fixed evidence body")
    span = _span_map(item["span_map"], f"{path}.span_map")
    start = record.get("source_line_start") if isinstance(record, Mapping) else None
    end = record.get("source_line_end") if isinstance(record, Mapping) else None
    if start is not None and (span["source_line_start"] < int(start) or span["source_line_end"] > int(end)):
        raise ContractError(f"{path}.span_map is outside the evidence revision")
    return {
        "evidence_revision_id": evidence_id,
        "support_type": support_type,
        "quote": quote,
        "span_map": span,
    }


def validate_statement_draft(
    value: Any,
    path: str = "statement",
    *,
    allowed_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    item = _object(value, path)
    _exact_keys(item, {
        "draft_id", "canonical_key", "kind", "subject", "truth_condition", "scope",
        "abstraction", "qualification", "confidence", "supports",
    }, path)
    draft_id = _string(item["draft_id"], f"{path}.draft_id", nonempty=True)
    if not DRAFT_ID_RE.fullmatch(draft_id):
        raise ContractError(f"{path}.draft_id has invalid format")
    canonical_key = _string(item["canonical_key"], f"{path}.canonical_key", nonempty=True)
    kind = _string(item["kind"], f"{path}.kind", nonempty=True)
    if kind not in STATEMENT_KINDS:
        raise ContractError(f"{path}.kind is not controlled")
    subject = _string(item["subject"], f"{path}.subject", nonempty=True)
    truth_condition = _string(item["truth_condition"], f"{path}.truth_condition", nonempty=True)
    if truth_condition.endswith(("。", ".", "!", "！", "?", "？")) is False:
        raise ContractError(f"{path}.truth_condition must be a complete proposition")
    scope = _object(item["scope"], f"{path}.scope")
    _exact_keys(scope, {"documentary_scope", "target_type", "chapter"}, f"{path}.scope")
    documentary_scope = _string(scope["documentary_scope"], f"{path}.scope.documentary_scope", nonempty=True)
    target_type = _string(scope["target_type"], f"{path}.scope.target_type", nonempty=True)
    if documentary_scope not in DOCUMENTARY_SCOPES:
        raise ContractError(f"{path}.scope.documentary_scope is not controlled")
    if target_type not in TARGET_TYPES:
        raise ContractError(f"{path}.scope.target_type is not controlled")
    chapter = _integer_or_null(scope["chapter"], f"{path}.scope.chapter")
    if documentary_scope in {"span", "section", "reading_unit"} and chapter is None:
        raise ContractError(f"{path}.scope.chapter is required for local scope")
    abstraction = _string(item["abstraction"], f"{path}.abstraction", nonempty=True)
    if abstraction not in ABSTRACTIONS:
        raise ContractError(f"{path}.abstraction is not controlled")
    qualification = _nullable_string(item["qualification"], f"{path}.qualification")
    confidence = _string(item["confidence"], f"{path}.confidence", nonempty=True)
    if confidence not in CONFIDENCE_VALUES:
        raise ContractError(f"{path}.confidence is not controlled")
    supports = item["supports"]
    if not isinstance(supports, list) or not supports:
        raise ContractError(f"{path}.supports must contain at least one support")
    evidence = allowed_evidence or {}
    normalized_supports = [
        validate_support(support, f"{path}.supports[{index}]", allowed_evidence=evidence)
        for index, support in enumerate(supports)
    ]
    if not any(support["support_type"] == "positive" for support in normalized_supports):
        raise ContractError(f"{path} has no positive evidence closure")
    return {
        "draft_id": draft_id,
        "canonical_key": canonical_key,
        "kind": kind,
        "subject": subject,
        "truth_condition": truth_condition,
        "scope": {"documentary_scope": documentary_scope, "target_type": target_type, "chapter": chapter},
        "abstraction": abstraction,
        "qualification": qualification,
        "confidence": confidence,
        "supports": normalized_supports,
    }


def validate_composition_draft(
    value: Any,
    path: str = "composition",
    *,
    statement_ids: set[str],
    composition_ids: set[str] | None = None,
    upstream_statement_ids: set[str] | None = None,
    upstream_composition_ids: set[str] | None = None,
) -> dict[str, Any]:
    item = _object(value, path)
    _exact_keys(item, {"draft_id", "canonical_key", "kind", "members", "rendering"}, path)
    draft_id = _string(item["draft_id"], f"{path}.draft_id", nonempty=True)
    if not DRAFT_ID_RE.fullmatch(draft_id):
        raise ContractError(f"{path}.draft_id has invalid format")
    canonical_key = _string(item["canonical_key"], f"{path}.canonical_key", nonempty=True)
    kind = _string(item["kind"], f"{path}.kind", nonempty=True)
    if kind not in COMPOSITION_KINDS:
        raise ContractError(f"{path}.kind is not controlled")
    members = item["members"]
    if not isinstance(members, list) or not members:
        raise ContractError(f"{path}.members must contain at least one member")
    normalized_members: list[dict[str, str]] = []
    known_compositions = composition_ids or set()
    for index, member in enumerate(members):
        value_member = _object(member, f"{path}.members[{index}]")
        _exact_keys(value_member, {"member_type", "draft_id", "role"}, f"{path}.members[{index}]")
        member_type = _string(value_member["member_type"], f"{path}.members[{index}].member_type", nonempty=True)
        member_id = _string(value_member["draft_id"], f"{path}.members[{index}].draft_id", nonempty=True)
        role = _string(value_member["role"], f"{path}.members[{index}].role", nonempty=True)
        known_statements = statement_ids | (upstream_statement_ids or set())
        known_compositions = known_compositions | (upstream_composition_ids or set())
        if member_type == "statement" and member_id not in known_statements:
            raise ContractError(f"{path}.members[{index}] references unknown statement draft")
        if member_type == "composition" and member_id not in known_compositions:
            raise ContractError(f"{path}.members[{index}] references unknown composition draft")
        if member_type not in {"statement", "composition"}:
            raise ContractError(f"{path}.members[{index}].member_type is not controlled")
        normalized_members.append({"member_type": member_type, "draft_id": member_id, "role": role})
    rendering = item["rendering"]
    normalized_rendering: dict[str, Any] | None = None
    if rendering is not None:
        value_rendering = _object(rendering, f"{path}.rendering")
        _exact_keys(value_rendering, {"text", "span_map"}, f"{path}.rendering")
        normalized_rendering = {
            "text": _string(value_rendering["text"], f"{path}.rendering.text", nonempty=True),
            "span_map": _object(value_rendering["span_map"], f"{path}.rendering.span_map"),
        }
    return {
        "draft_id": draft_id,
        "canonical_key": canonical_key,
        "kind": kind,
        "members": normalized_members,
        "rendering": normalized_rendering,
    }


def validate_grounded_payload(
    value: Any,
    *,
    stage: str,
    allowed_evidence: Mapping[str, Any],
    upstream_statement_ids: set[str] | None = None,
    upstream_composition_ids: set[str] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """Validate a complete stage draft and return normalized payload plus IDs."""
    item = _object(value, stage)
    _exact_keys(item, {"statements", "compositions"}, stage)
    statements_raw = item["statements"]
    compositions_raw = item["compositions"]
    if not isinstance(statements_raw, list) or not isinstance(compositions_raw, list):
        raise ContractError(f"{stage}.statements and {stage}.compositions must be arrays")
    upstream_statements = upstream_statement_ids or set()
    upstream_compositions = upstream_composition_ids or set()
    statements: list[dict[str, Any]] = []
    statement_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for index, statement in enumerate(statements_raw):
        normalized = validate_statement_draft(statement, f"{stage}.statements[{index}]", allowed_evidence=allowed_evidence)
        if normalized["draft_id"] in statement_ids:
            raise ContractError(f"duplicate statement draft_id: {normalized['draft_id']}")
        if normalized["draft_id"] in upstream_statements:
            raise ContractError(
                f"statement draft_id reuses an upstream draft_id: {normalized['draft_id']}"
            )
        statement_ids.add(normalized["draft_id"])
        evidence_ids.update(item["evidence_revision_id"] for item in normalized["supports"])
        statements.append(normalized)
    compositions: list[dict[str, Any]] = []
    composition_ids: set[str] = set()
    for index, composition in enumerate(compositions_raw):
        normalized = validate_composition_draft(
            composition,
            f"{stage}.compositions[{index}]",
            statement_ids=statement_ids,
            composition_ids=composition_ids,
            upstream_statement_ids=upstream_statement_ids,
            upstream_composition_ids=upstream_composition_ids,
        )
        if normalized["draft_id"] in composition_ids:
            raise ContractError(f"duplicate composition draft_id: {normalized['draft_id']}")
        if normalized["draft_id"] in upstream_compositions:
            raise ContractError(
                f"composition draft_id reuses an upstream draft_id: {normalized['draft_id']}"
            )
        composition_ids.add(normalized["draft_id"])
        compositions.append(normalized)
    if not statements and not compositions:
        raise ContractError(f"{stage} draft must contain a statement or composition")
    return {"statements": statements, "compositions": compositions}, evidence_ids


def validate_chapter_payload(
    value: dict[str, Any],
    *,
    chapter: int,
    chunks: list[dict[str, Any]] | None = None,
    evidence: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
) -> set[str]:
    """Validate a chapter draft; old CnEn and free-text payloads are rejected."""
    allowed_ids, records = _allowed_evidence_ids(chunks, evidence)
    if not allowed_ids:
        raise ContractError("chapter has no pinned evidence_revision allowlist")
    normalized, evidence_ids = validate_grounded_payload(value, stage="chapter", allowed_evidence=records)
    for statement in normalized["statements"]:
        scope = statement["scope"]
        if scope["chapter"] is not None and scope["chapter"] != chapter:
            raise ContractError("chapter statement scope does not match the chapter")
    return evidence_ids


def validate_rollup_payload(
    value: dict[str, Any],
    *,
    allowed_ids: set[str] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> set[str]:
    """Validate a rollup draft against a fixed evidence revision allowlist."""
    records = evidence or {identifier: {"evidence_revision_id": identifier, "quote": "", "source_line_start": 1, "source_line_end": 1} for identifier in (allowed_ids or set())}
    # A rollup still requires exact quotes; callers must provide records for real validation.
    if not evidence:
        raise ContractError("rollup requires fixed evidence revision records")
    _, evidence_ids = validate_grounded_payload(value, stage="rollup", allowed_evidence=records)
    if allowed_ids is not None and not evidence_ids.issubset(allowed_ids):
        raise ContractError("rollup references evidence outside its fixed allowlist")
    return evidence_ids


def validate_book_payload(
    value: dict[str, Any],
    *,
    allowed_ids: set[str] | None = None,
    chapters: set[int] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> set[str]:
    """Validate a book draft; compositions must be supported by local draft nodes."""
    if not evidence:
        raise ContractError("book requires fixed evidence revision records")
    _, evidence_ids = validate_grounded_payload(value, stage="book", allowed_evidence=evidence)
    if allowed_ids is not None and not evidence_ids.issubset(allowed_ids):
        raise ContractError("book references evidence outside its fixed allowlist")
    return evidence_ids


def collect_prompt_evidence_ids(items: Iterable[Mapping[str, Any]]) -> set[str]:
    """Collect only Grounded v2 evidence revision IDs recursively."""
    result: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            direct = value.get("evidence_revision_id")
            if isinstance(direct, str) and EVIDENCE_REVISION_ID_RE.fullmatch(direct):
                result.add(direct)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for item in items:
        visit(item)
    return result


def collect_claim_ids(value: dict[str, Any]) -> set[str]:
    """Collect evidence revision IDs from normalized v2 support-bearing nodes."""
    result: set[str] = set()
    for statement in value.get("statements", []) if isinstance(value.get("statements"), list) else []:
        if isinstance(statement, Mapping):
            for support in statement.get("supports", []) if isinstance(statement.get("supports"), list) else []:
                identifier = support.get("evidence_revision_id") if isinstance(support, Mapping) else None
                if isinstance(identifier, str):
                    result.add(identifier)
    return result
