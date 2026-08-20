"""Immutable AnswerPlan, AnswerEvidenceBundle, and citation snapshots for Grounded v2."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .coverage import CoverageEvaluation, KnowledgeRetriever, evaluate_coverage
from .knowledge_store import GroundedKnowledgeStore, KnowledgeStoreError
from .query_analyzer import QueryAnalysis

CONTRACT_VERSION = "grounded-v2"
ANSWER_PLAN_VERSION = 1
_SEGMENT_ID_RE = re.compile(r"^seg-[0-9]+$")


class AnswerPlanError(ValueError):
    """A plan, evidence bundle, or answer segment violates Grounded v2 invariants."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise AnswerPlanError(f"{label} is invalid JSON") from exc
    if not isinstance(result, dict):
        raise AnswerPlanError(f"{label} must be an object")
    return result


@dataclass(frozen=True)
class PlanNode:
    node_type: str
    node_revision_id: str
    role: str


@dataclass(frozen=True)
class AnswerEvidenceBundle:
    """The fixed, closed set of evidence revisions an AnswerPlan may cite."""

    book_revision_id: str
    evidence_revision_ids: tuple[str, ...]
    quotes: dict[str, str]
    spans: dict[str, dict[str, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_revision_id": self.book_revision_id,
            "evidence_revision_ids": list(self.evidence_revision_ids),
            "quotes": dict(self.quotes),
            "spans": {key: dict(value) for key, value in self.spans.items()},
        }


@dataclass(frozen=True)
class AnswerPlan:
    answer_plan_id: str
    book_revision_id: str
    answer_mode: str
    content_hash: str
    query_analysis: dict[str, Any]
    policy_snapshot: dict[str, Any]
    model_input_hash: str | None
    nodes: tuple[PlanNode, ...]
    evidence_bundle: AnswerEvidenceBundle

    @property
    def statement_revision_ids(self) -> tuple[str, ...]:
        return tuple(node.node_revision_id for node in self.nodes if node.node_type == "statement")

    @property
    def composition_revision_ids(self) -> tuple[str, ...]:
        return tuple(node.node_revision_id for node in self.nodes if node.node_type == "composition")


def _build_evidence_bundle(
    conn: sqlite3.Connection,
    root: Path,
    book_id: int,
    book_revision_id: str,
    evidence_ids: Sequence[str],
) -> AnswerEvidenceBundle:
    """Re-verify selected evidence against the immutable registry before pinning it."""
    try:
        store = GroundedKnowledgeStore(conn, root, book_id, book_revision_id)
    except KnowledgeStoreError as exc:
        raise AnswerPlanError(f"pinned book revision evidence failed verification: {exc}") from exc
    quotes: dict[str, str] = {}
    spans: dict[str, dict[str, int]] = {}
    for evidence_id in evidence_ids:
        record = store.evidence.get(evidence_id)
        if record is None:
            raise AnswerPlanError(f"selected evidence is outside the pinned book revision: {evidence_id}")
        quotes[evidence_id] = str(record["payload"]["quote"])
        spans[evidence_id] = {
            "source_line_start": int(record["source_line_start"]),
            "source_line_end": int(record["source_line_end"]),
        }
    return AnswerEvidenceBundle(book_revision_id, tuple(sorted(evidence_ids)), quotes, spans)


def build_answer_plan(
    conn: sqlite3.Connection,
    root: Path,
    book_id: int,
    book_revision_id: str,
    analysis: QueryAnalysis,
    *,
    policy_snapshot: Mapping[str, Any],
    model_input_hash: str | None = None,
    retriever: KnowledgeRetriever | None = None,
) -> tuple[AnswerPlan, CoverageEvaluation]:
    """Select a deterministic answer mode and pin its closed evidence and knowledge."""
    if not isinstance(policy_snapshot, Mapping):
        raise AnswerPlanError("policy_snapshot must be an object")
    if model_input_hash is not None and (not isinstance(model_input_hash, str) or not model_input_hash.strip()):
        raise AnswerPlanError("model_input_hash must be a non-empty string or None")

    retriever = retriever or KnowledgeRetriever(conn, book_id, book_revision_id)
    retrieval = retriever.retrieve(analysis)
    coverage = evaluate_coverage(analysis, retrieval)
    if not coverage.sufficient:
        raise AnswerPlanError(
            "insufficient pinned evidence for this question: " + ", ".join(coverage.reasons)
        )

    nodes = sorted(
        (
            *(PlanNode("statement", item, "support") for item in coverage.selected_statement_revision_ids),
            *(PlanNode("composition", item, "support") for item in coverage.selected_composition_revision_ids),
            *(PlanNode("evidence", item, "support") for item in coverage.selected_evidence_revision_ids),
        ),
        key=lambda item: (item.node_type, item.node_revision_id),
    )
    if not nodes:
        raise AnswerPlanError("coverage evaluation selected no plan nodes")

    bundle = _build_evidence_bundle(conn, root, book_id, book_revision_id, coverage.selected_evidence_revision_ids)

    payload = {
        "contract_version": CONTRACT_VERSION,
        "plan_version": ANSWER_PLAN_VERSION,
        "book_revision_id": book_revision_id,
        "answer_mode": coverage.answer_mode,
        "query_analysis": analysis.to_dict(),
        "policy_snapshot": dict(policy_snapshot),
        "model_input_hash": model_input_hash,
        "nodes": [
            {"node_type": node.node_type, "node_revision_id": node.node_revision_id, "role": node.role}
            for node in nodes
        ],
        "evidence_bundle": bundle.to_dict(),
    }
    content_hash = _hash(payload)
    plan = AnswerPlan(
        answer_plan_id=f"aplan-{content_hash[:32]}",
        book_revision_id=book_revision_id,
        answer_mode=coverage.answer_mode,
        content_hash=content_hash,
        query_analysis=analysis.to_dict(),
        policy_snapshot=dict(policy_snapshot),
        model_input_hash=model_input_hash,
        nodes=tuple(nodes),
        evidence_bundle=bundle,
    )
    return plan, coverage


def persist_answer_plan(conn: sqlite3.Connection, plan: AnswerPlan) -> None:
    """Content-address and persist an AnswerPlan; identical plans are deduplicated."""
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT content_hash FROM answer_plans WHERE id = ?", (plan.answer_plan_id,)
        ).fetchone()
        if existing is not None and str(existing["content_hash"]) != plan.content_hash:
            raise AnswerPlanError("answer plan identity collision")
        conn.execute(
            """INSERT OR IGNORE INTO answer_plans
               (id, turn_id, book_revision_id, content_hash, answer_mode, query_analysis_json,
                policy_snapshot_json, evidence_bundle_json, model_input_hash, status, created_at)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 'validated', ?)""",
            (
                plan.answer_plan_id, plan.book_revision_id, plan.content_hash, plan.answer_mode,
                json.dumps(plan.query_analysis, ensure_ascii=False, sort_keys=True),
                json.dumps(plan.policy_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(plan.evidence_bundle.to_dict(), ensure_ascii=False, sort_keys=True),
                plan.model_input_hash, now,
            ),
        )
        for ordinal, node in enumerate(plan.nodes):
            conn.execute(
                """INSERT OR IGNORE INTO answer_plan_nodes
                   (answer_plan_id, ordinal, node_type, node_revision_id, role)
                   VALUES (?, ?, ?, ?, ?)""",
                (plan.answer_plan_id, ordinal, node.node_type, node.node_revision_id, node.role),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def load_answer_plan(conn: sqlite3.Connection, root: Path, book_id: int, answer_plan_id: str) -> AnswerPlan:
    """Reload a persisted plan and re-verify its evidence has not diverged since."""
    row = conn.execute("SELECT * FROM answer_plans WHERE id = ?", (answer_plan_id,)).fetchone()
    if row is None:
        raise AnswerPlanError(f"answer plan not found: {answer_plan_id}")
    node_rows = conn.execute(
        "SELECT node_type, node_revision_id, role FROM answer_plan_nodes WHERE answer_plan_id = ? ORDER BY ordinal",
        (answer_plan_id,),
    ).fetchall()
    if not node_rows:
        raise AnswerPlanError(f"answer plan has no nodes: {answer_plan_id}")
    nodes = tuple(
        PlanNode(str(item["node_type"]), str(item["node_revision_id"]), str(item["role"])) for item in node_rows
    )
    evidence_ids = tuple(sorted({node.node_revision_id for node in nodes if node.node_type == "evidence"}))
    book_revision_id = str(row["book_revision_id"])
    bundle = _build_evidence_bundle(conn, root, book_id, book_revision_id, evidence_ids)
    query_analysis = _json_object(row["query_analysis_json"], "answer plan query analysis")
    policy_snapshot = _json_object(row["policy_snapshot_json"], "answer plan policy snapshot")
    model_input_hash = row["model_input_hash"]
    payload = {
        "contract_version": CONTRACT_VERSION,
        "plan_version": ANSWER_PLAN_VERSION,
        "book_revision_id": book_revision_id,
        "answer_mode": str(row["answer_mode"]),
        "query_analysis": query_analysis,
        "policy_snapshot": policy_snapshot,
        "model_input_hash": model_input_hash,
        "nodes": [
            {"node_type": node.node_type, "node_revision_id": node.node_revision_id, "role": node.role}
            for node in nodes
        ],
        "evidence_bundle": bundle.to_dict(),
    }
    content_hash = _hash(payload)
    if content_hash != str(row["content_hash"]) or answer_plan_id != f"aplan-{content_hash[:32]}":
        raise AnswerPlanError("persisted answer plan payload no longer matches its pinned identity")
    return AnswerPlan(
        answer_plan_id=answer_plan_id, book_revision_id=book_revision_id, answer_mode=str(row["answer_mode"]),
        content_hash=content_hash, query_analysis=query_analysis, policy_snapshot=policy_snapshot,
        model_input_hash=model_input_hash, nodes=nodes, evidence_bundle=bundle,
    )


def build_citation_snapshot(plan: AnswerPlan) -> dict[str, Any]:
    """Derive the fixed, content-addressed citation snapshot for one AnswerPlan."""
    citations = [
        {
            "evidence_revision_id": evidence_id,
            "quote": plan.evidence_bundle.quotes[evidence_id],
            "span_map": dict(plan.evidence_bundle.spans[evidence_id]),
        }
        for evidence_id in plan.evidence_bundle.evidence_revision_ids
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "book_revision_id": plan.book_revision_id,
        "answer_plan_id": plan.answer_plan_id,
        "citations": citations,
    }


def persist_citation_snapshot(conn: sqlite3.Connection, plan: AnswerPlan) -> str:
    """Persist the content-addressed citation snapshot for one AnswerPlan."""
    snapshot = build_citation_snapshot(plan)
    content_hash = _hash(snapshot)
    snapshot_id = f"csnap-{content_hash[:32]}"
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT content_hash FROM citation_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if existing is not None and str(existing["content_hash"]) != content_hash:
            raise AnswerPlanError("citation snapshot identity collision")
        conn.execute(
            """INSERT OR IGNORE INTO citation_snapshots
               (id, answer_plan_id, book_revision_id, content_hash, citations_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'healthy', ?)""",
            (
                snapshot_id, plan.answer_plan_id, plan.book_revision_id, content_hash,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return snapshot_id


def load_citation_snapshot(conn: sqlite3.Connection, answer_plan_id: str) -> dict[str, Any]:
    """Load a persisted citation snapshot and fail closed if it is not healthy."""
    row = conn.execute(
        "SELECT content_hash, citations_json, status FROM citation_snapshots WHERE answer_plan_id = ?",
        (answer_plan_id,),
    ).fetchone()
    if row is None:
        raise AnswerPlanError(f"no citation snapshot for answer plan: {answer_plan_id}")
    if str(row["status"]) != "healthy":
        raise AnswerPlanError(f"citation snapshot is not healthy: {answer_plan_id}")
    snapshot = _json_object(row["citations_json"], "citation snapshot")
    if _hash(snapshot) != str(row["content_hash"]):
        raise AnswerPlanError("citation snapshot payload hash mismatch")
    return snapshot


def validate_answer_payload(plan: AnswerPlan, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate a model answer against exactly the pinned AnswerPlan, failing closed."""
    if not isinstance(payload, Mapping) or set(payload) != {"segments"}:
        raise AnswerPlanError("answer payload must contain exactly one 'segments' key")
    segments = payload["segments"]
    if not isinstance(segments, list) or not segments:
        raise AnswerPlanError("answer payload segments must be a non-empty array")
    allowed_statements = set(plan.statement_revision_ids)
    allowed_evidence = set(plan.evidence_bundle.evidence_revision_ids)
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(segments, start=1):
        if not isinstance(raw, Mapping) or set(raw) != {
            "segment_id", "text", "statement_revision_ids", "evidence_revision_ids",
        }:
            raise AnswerPlanError(f"segment[{index}] has invalid keys")
        segment_id = raw["segment_id"]
        if not isinstance(segment_id, str) or not _SEGMENT_ID_RE.fullmatch(segment_id):
            raise AnswerPlanError(f"segment[{index}] has an invalid segment_id")
        if segment_id != f"seg-{index}":
            raise AnswerPlanError(f"segment[{index}] segment_id must be sequential (expected seg-{index})")
        text = raw["text"]
        if not isinstance(text, str) or not text.strip():
            raise AnswerPlanError(f"segment[{index}] text must be a non-empty string")
        statement_ids = raw["statement_revision_ids"]
        evidence_ids = raw["evidence_revision_ids"]
        if not isinstance(statement_ids, list) or not isinstance(evidence_ids, list):
            raise AnswerPlanError(f"segment[{index}] revision id fields must be arrays")
        if any(not isinstance(item, str) or not item for item in (*statement_ids, *evidence_ids)):
            raise AnswerPlanError(f"segment[{index}] contains an invalid revision id")
        unknown_statements = set(statement_ids) - allowed_statements
        if unknown_statements:
            raise AnswerPlanError(
                f"segment[{index}] cites statements outside the pinned AnswerPlan: {sorted(unknown_statements)}"
            )
        if not evidence_ids:
            raise AnswerPlanError(f"segment[{index}] must cite at least one pinned evidence revision")
        unknown_evidence = set(evidence_ids) - allowed_evidence
        if unknown_evidence:
            raise AnswerPlanError(
                f"segment[{index}] cites evidence outside the pinned AnswerEvidenceBundle: {sorted(unknown_evidence)}"
            )
        normalized.append({
            "segment_id": segment_id,
            "text": text.strip(),
            "statement_revision_ids": list(statement_ids),
            "evidence_revision_ids": list(evidence_ids),
        })
    return normalized


def verify_exact_quote(plan: AnswerPlan, evidence_revision_id: str, text: str) -> bool:
    """Return whether ``text`` is byte-for-byte the pinned evidence body."""
    expected = plan.evidence_bundle.quotes.get(evidence_revision_id)
    return expected is not None and expected == text
