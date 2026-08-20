"""Typed retrieval and deterministic coverage evaluation for Grounded v2."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .query_analyzer import QueryAnalysis

CONTRACT_VERSION = "grounded-v2"
ANSWER_MODES = frozenset({"canonical_passthrough", "knowledge_synthesis", "raw_synthesis"})


class CoverageError(ValueError):
    """Pinned knowledge cannot be read or evaluated safely."""


def _tokens(value: str) -> set[str]:
    normalized = str(value or "").casefold()
    latin = re.findall(r"[a-z0-9]+", normalized)
    cjk = re.findall(r"[㐀-鿿]", normalized)
    return {item for item in latin + cjk if item}


def _score(question: str, text: str) -> float:
    query = _tokens(question)
    candidate = _tokens(text)
    if not query or not candidate:
        return 0.0
    overlap = len(query & candidate)
    return overlap / math.sqrt(len(query) * len(candidate))


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise CoverageError(f"{label} is invalid JSON") from exc
    if not isinstance(result, dict):
        raise CoverageError(f"{label} must be an object")
    return result


@dataclass(frozen=True)
class RetrievedStatement:
    statement_revision_id: str
    kind: str
    semantic_text: str
    scope: dict[str, Any]
    confidence: str
    evidence_revision_ids: tuple[str, ...]
    score: float


@dataclass(frozen=True)
class RetrievedComposition:
    composition_revision_id: str
    kind: str
    rendering_text: str
    statement_revision_ids: tuple[str, ...]
    child_composition_revision_ids: tuple[str, ...]
    evidence_revision_ids: tuple[str, ...]
    score: float


@dataclass(frozen=True)
class RetrievedEvidence:
    evidence_revision_id: str
    chapter: int
    quote: str
    source_path: str
    source_line_start: int
    source_line_end: int
    locator: dict[str, Any]
    score: float


@dataclass(frozen=True)
class RetrievalSet:
    book_revision_id: str
    statements: tuple[RetrievedStatement, ...]
    compositions: tuple[RetrievedComposition, ...]
    evidence: tuple[RetrievedEvidence, ...]


@dataclass(frozen=True)
class CoverageEvaluation:
    answer_mode: str
    sufficient: bool
    canonical_complete: bool
    knowledge_complete: bool
    raw_complete: bool
    conflict: bool
    reasons: tuple[str, ...]
    selected_statement_revision_ids: tuple[str, ...]
    selected_composition_revision_ids: tuple[str, ...]
    selected_evidence_revision_ids: tuple[str, ...]
    scores: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_mode": self.answer_mode,
            "sufficient": self.sufficient,
            "canonical_complete": self.canonical_complete,
            "knowledge_complete": self.knowledge_complete,
            "raw_complete": self.raw_complete,
            "conflict": self.conflict,
            "reasons": list(self.reasons),
            "selected_statement_revision_ids": list(self.selected_statement_revision_ids),
            "selected_composition_revision_ids": list(self.selected_composition_revision_ids),
            "selected_evidence_revision_ids": list(self.selected_evidence_revision_ids),
            "scores": dict(self.scores),
        }


class KnowledgeRetriever:
    """Read only healthy nodes from exactly one pinned healthy book revision."""

    def __init__(self, conn: sqlite3.Connection, book_id: int, book_revision_id: str):
        self.conn = conn
        self.book_id = int(book_id)
        self.book_revision_id = str(book_revision_id)
        row = conn.execute(
            """SELECT br.status, br.contract_version, b.active_book_revision_id
               FROM book_revisions br JOIN books b ON b.id = br.book_id
               WHERE br.id = ? AND br.book_id = ?""",
            (self.book_revision_id, self.book_id),
        ).fetchone()
        if row is None:
            raise CoverageError("pinned book revision does not exist")
        if row["contract_version"] != CONTRACT_VERSION or row["status"] != "healthy":
            raise CoverageError("pinned book revision is not healthy Grounded v2 knowledge")
        if str(row["active_book_revision_id"] or "") != self.book_revision_id:
            raise CoverageError("pinned book revision is not the active healthy revision")

    def _statement(self, row: sqlite3.Row, score: float) -> RetrievedStatement:
        payload = _json_object(row["payload_json"], "statement payload").get("payload")
        if not isinstance(payload, Mapping):
            raise CoverageError("statement payload is malformed")
        scope = _json_object(row["scope_json"], "statement scope")
        closure = self.conn.execute(
            """SELECT ec.evidence_revision_id FROM evidence_closure ec
               JOIN evidence_revisions er ON er.id = ec.evidence_revision_id
               WHERE ec.statement_revision_id = ? AND ec.positive = 1
               AND er.book_revision_id = ? ORDER BY ec.evidence_revision_id""",
            (row["id"], self.book_revision_id),
        ).fetchall()
        evidence_ids = tuple(str(item[0]) for item in closure)
        if not evidence_ids:
            raise CoverageError(f"statement lacks positive pinned closure: {row['id']}")
        return RetrievedStatement(
            statement_revision_id=str(row["id"]), kind=str(row["kind"]),
            semantic_text=str(row["semantic_text"]), scope=scope,
            confidence=str(payload.get("confidence", "")), evidence_revision_ids=evidence_ids,
            score=score,
        )

    def search_statements(self, analysis: QueryAnalysis, limit: int = 8) -> tuple[RetrievedStatement, ...]:
        rows = self.conn.execute(
            """SELECT id, semantic_text, kind, scope_json, payload_json
               FROM statement_revisions WHERE book_revision_id = ? AND status = 'healthy'""",
            (self.book_revision_id,),
        ).fetchall()
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            scope = _json_object(row["scope_json"], "statement scope")
            chapter = scope.get("chapter")
            if analysis.chapter is not None and chapter != analysis.chapter:
                continue
            base = _score(analysis.normalized_question, str(row["semantic_text"]))
            if base <= 0:
                continue
            score = base
            if row["kind"] == analysis.semantic_kind:
                score += 0.35
            if scope.get("documentary_scope") == analysis.documentary_scope:
                score += 0.15
            ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], str(item[1]["id"])))
        return tuple(self._statement(row, score) for score, row in ranked[:max(0, limit)] if score > 0)

    def _composition_closure(self, composition_id: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        pending = [composition_id]
        seen: set[str] = set()
        statements: set[str] = set()
        children: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                raise CoverageError("composition graph contains a cycle")
            seen.add(current)
            rows = self.conn.execute(
                """SELECT member_type, statement_revision_id, child_composition_revision_id
                   FROM composition_members WHERE composition_revision_id = ? ORDER BY ordinal""",
                (current,),
            ).fetchall()
            if not rows:
                raise CoverageError(f"composition has no members: {current}")
            for row in rows:
                if row["member_type"] == "statement":
                    statements.add(str(row["statement_revision_id"]))
                elif row["member_type"] == "composition":
                    child = str(row["child_composition_revision_id"])
                    children.add(child)
                    pending.append(child)
                else:
                    raise CoverageError("composition member type is invalid")
        if statements:
            placeholders = ",".join("?" for _ in statements)
            rows = self.conn.execute(
                f"""SELECT sr.id, ec.evidence_revision_id FROM statement_revisions sr
                    LEFT JOIN evidence_closure ec ON ec.statement_revision_id = sr.id AND ec.positive = 1
                    WHERE sr.id IN ({placeholders}) AND sr.book_revision_id = ? AND sr.status = 'healthy'""",
                (*sorted(statements), self.book_revision_id),
            ).fetchall()
            valid_statements = {str(row["id"]) for row in rows}
            if valid_statements != statements or any(row["evidence_revision_id"] is None for row in rows):
                raise CoverageError("composition includes an unhealthy or unsupported statement")
            evidence = {str(row["evidence_revision_id"]) for row in rows}
        else:
            raise CoverageError("composition has no statement truth source")
        return tuple(sorted(statements)), tuple(sorted(children)), tuple(sorted(evidence))

    def search_compositions(self, analysis: QueryAnalysis, limit: int = 5) -> tuple[RetrievedComposition, ...]:
        rows = self.conn.execute(
            """SELECT cr.id, cr.kind, cr.payload_json, rr.rendering_text
               FROM composition_revisions cr
               JOIN composition_renderings rr ON rr.composition_revision_id = cr.id
               WHERE cr.book_revision_id = ? AND cr.status = 'healthy'""",
            (self.book_revision_id,),
        ).fetchall()
        preferred = {
            "summary": {"summary", "chapter_summary", "rollup", "overview"},
            "explore": {"overview", "concept_profile", "argument_map"},
            "causal": {"argument_chain", "argument_map"},
            "compare": {"concept_profile", "argument_map"},
        }.get(analysis.task, set())
        ranked = []
        for row in rows:
            score = _score(analysis.normalized_question, str(row["rendering_text"]))
            if row["kind"] in preferred:
                score += 0.35
            ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], str(item[1]["id"])))
        output: list[RetrievedComposition] = []
        for score, row in ranked[:max(0, limit)]:
            if score <= 0:
                continue
            statements, children, evidence = self._composition_closure(str(row["id"]))
            output.append(RetrievedComposition(
                composition_revision_id=str(row["id"]), kind=str(row["kind"]),
                rendering_text=str(row["rendering_text"]), statement_revision_ids=statements,
                child_composition_revision_ids=children, evidence_revision_ids=evidence, score=score,
            ))
        return tuple(output)

    def search_evidence(self, analysis: QueryAnalysis, limit: int = 8) -> tuple[RetrievedEvidence, ...]:
        rows = self.conn.execute(
            """SELECT id, chapter, source_path, source_line_start, source_line_end, locator_json, payload_json
               FROM evidence_revisions WHERE book_revision_id = ?""",
            (self.book_revision_id,),
        ).fetchall()
        ranked: list[tuple[float, sqlite3.Row, str, dict[str, Any]]] = []
        for row in rows:
            if analysis.chapter is not None and int(row["chapter"]) != analysis.chapter:
                continue
            payload = _json_object(row["payload_json"], "evidence payload")
            quote = payload.get("quote")
            if not isinstance(quote, str) or not quote:
                raise CoverageError("evidence payload has no fixed quote")
            locator = _json_object(row["locator_json"], "evidence locator")
            score = _score(analysis.normalized_question, quote)
            if analysis.terms and any(term.casefold() in quote.casefold() for term in analysis.terms):
                score += 0.8
            ranked.append((score, row, quote, locator))
        ranked.sort(key=lambda item: (-item[0], str(item[1]["id"])))
        return tuple(
            RetrievedEvidence(
                evidence_revision_id=str(row["id"]), chapter=int(row["chapter"]), quote=quote,
                source_path=str(row["source_path"]), source_line_start=int(row["source_line_start"]),
                source_line_end=int(row["source_line_end"]), locator=locator, score=score,
            )
            for score, row, quote, locator in ranked[:max(0, limit)] if score > 0
        )

    def retrieve(self, analysis: QueryAnalysis) -> RetrievalSet:
        statements = self.search_statements(analysis)
        compositions = self.search_compositions(analysis)
        evidence = self.search_evidence(analysis)
        return RetrievalSet(self.book_revision_id, statements, compositions, evidence)


def evaluate_coverage(analysis: QueryAnalysis, retrieval: RetrievalSet) -> CoverageEvaluation:
    """Select an answer mode without delegating grounding policy to a model."""
    if retrieval.book_revision_id == "":
        raise CoverageError("retrieval has no pinned book revision")
    statements = retrieval.statements
    compositions = retrieval.compositions
    evidence = retrieval.evidence
    statement_ids: set[str] = set()
    composition_ids: set[str] = set()
    evidence_ids: set[str] = set()
    reasons: list[str] = []

    canonical_complete = False
    canonical = None
    if not analysis.exact_wording and analysis.task in {"summary", "definition", "explain", "explore"}:
        canonical = next((item for item in compositions if item.score >= 0.35 and item.rendering_text.strip()), None)
        if canonical is not None:
            canonical_complete = True
            composition_ids.add(canonical.composition_revision_id)
            statement_ids.update(canonical.statement_revision_ids)
            evidence_ids.update(canonical.evidence_revision_ids)

    selected_statements = [item for item in statements if item.score >= 0.2][:6]
    knowledge_complete = bool(selected_statements)
    if analysis.subject_breadth == "relation" and len(selected_statements) < 2:
        knowledge_complete = False
        reasons.append("relation_requires_multiple_knowledge_nodes")
    if analysis.documentary_scope in {"book", "rollup"} and len(selected_statements) < 2 and not canonical_complete:
        knowledge_complete = False
        reasons.append("cross_scope_knowledge_is_incomplete")
    for item in selected_statements:
        statement_ids.add(item.statement_revision_id)
        evidence_ids.update(item.evidence_revision_ids)

    raw_candidates = [item for item in evidence if item.score >= 0.15]
    if analysis.exact_wording and analysis.terms:
        raw_candidates = [
            item for item in evidence
            if all(term.casefold() in item.quote.casefold() for term in analysis.terms)
        ]
    raw_complete = bool(raw_candidates)
    if analysis.subject_breadth == "relation" and len(raw_candidates) < 2:
        raw_complete = False
    for item in raw_candidates[:6]:
        evidence_ids.add(item.evidence_revision_id)

    positive_truths: dict[str, set[str]] = {}
    for item in selected_statements:
        key = " ".join(item.semantic_text.casefold().split())
        positive_truths.setdefault(key, set()).update(item.evidence_revision_ids)
    conflict = False
    negative = {"不", "非", "没有", "never", "not", "no"}
    texts = list(positive_truths)
    for index, left in enumerate(texts):
        for right in texts[index + 1:]:
            left_tokens, right_tokens = _tokens(left), _tokens(right)
            if len((left_tokens - negative) & (right_tokens - negative)) >= 3 and bool(left_tokens & negative) != bool(right_tokens & negative):
                conflict = True
                break
        if conflict:
            break
    if conflict:
        reasons.append("knowledge_conflict_detected")
        canonical_complete = False
        knowledge_complete = False

    if canonical_complete:
        mode = "canonical_passthrough"
        reasons.append("complete_canonical_composition")
    elif knowledge_complete and not analysis.exact_wording:
        mode = "knowledge_synthesis"
        reasons.append("positive_knowledge_closure")
    else:
        mode = "raw_synthesis"
        reasons.append("raw_evidence_required")
    sufficient = canonical_complete or knowledge_complete or raw_complete
    if not sufficient:
        reasons.append("insufficient_pinned_evidence")

    scores = {
        "statement_max": max((item.score for item in statements), default=0.0),
        "composition_max": max((item.score for item in compositions), default=0.0),
        "evidence_max": max((item.score for item in evidence), default=0.0),
    }
    return CoverageEvaluation(
        answer_mode=mode, sufficient=sufficient, canonical_complete=canonical_complete,
        knowledge_complete=knowledge_complete, raw_complete=raw_complete, conflict=conflict,
        reasons=tuple(reasons), selected_statement_revision_ids=tuple(sorted(statement_ids)),
        selected_composition_revision_ids=tuple(sorted(composition_ids)),
        selected_evidence_revision_ids=tuple(sorted(evidence_ids)), scores=scores,
    )
