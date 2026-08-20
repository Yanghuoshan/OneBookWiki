"""Grounded v2 Knowledge Store materialization and publication."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_registry import EvidenceRegistryError, load_evidence_revision

CONTRACT_VERSION = "grounded-v2"
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
_REVISION_ID_RE = re.compile(r"^(?:str|cmpr)-[0-9a-f]{32}$")


class KnowledgeStoreError(ValueError):
    """A draft or publication cannot satisfy Grounded v2 invariants."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeStoreError(f"{path} must be a non-empty string")
    return value.strip()


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise KnowledgeStoreError(f"{path} must be an object")
    return dict(value)


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise KnowledgeStoreError(f"{path} must be an array")
    return list(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing={missing}")
        if extra:
            detail.append(f"extra={extra}")
        raise KnowledgeStoreError(f"{path} has invalid keys ({', '.join(detail)})")


def _span_map(value: Any, path: str) -> dict[str, Any]:
    span = _object(value, path)
    _exact_keys(span, {"source_line_start", "source_line_end"}, path)
    try:
        start = int(span["source_line_start"])
        end = int(span["source_line_end"])
    except (TypeError, ValueError) as exc:
        raise KnowledgeStoreError(f"{path} line range must be integers") from exc
    if start < 1 or end < start:
        raise KnowledgeStoreError(f"{path} line range is invalid")
    return {"source_line_start": start, "source_line_end": end}


def _validate_support(
    support: Any,
    path: str,
    evidence: Mapping[str, dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    item = _object(support, path)
    _exact_keys(item, {"evidence_revision_id", "support_type", "span_map", "quote"}, path)
    evidence_id = _nonempty_string(item["evidence_revision_id"], f"{path}.evidence_revision_id")
    record = evidence.get(evidence_id)
    if record is None:
        raise KnowledgeStoreError(f"{path} references evidence outside the pinned book revision")
    support_type = _nonempty_string(item["support_type"], f"{path}.support_type")
    if support_type not in SUPPORT_TYPES:
        raise KnowledgeStoreError(f"{path}.support_type is not controlled")
    span = _span_map(item["span_map"], f"{path}.span_map")
    quote = _nonempty_string(item["quote"], f"{path}.quote")
    try:
        _, body = load_evidence_revision(root, evidence_id)
    except EvidenceRegistryError as exc:
        raise KnowledgeStoreError(f"{path} evidence revision is not healthy: {exc}") from exc
    if quote != body:
        raise KnowledgeStoreError(f"{path}.quote must exactly match the pinned evidence body")
    if span["source_line_start"] < int(record["source_line_start"]):
        raise KnowledgeStoreError(f"{path}.span_map starts before evidence")
    if span["source_line_end"] > int(record["source_line_end"]):
        raise KnowledgeStoreError(f"{path}.span_map ends after evidence")
    return {
        "evidence_revision_id": evidence_id,
        "support_type": support_type,
        "span_map": span,
        "quote": quote,
    }


@dataclass(frozen=True)
class StatementRevision:
    draft_id: str
    statement_revision_id: str
    statement_identity_id: str
    book_revision_id: str
    content_hash: str
    semantic_text: str
    kind: str
    scope: dict[str, Any]
    supports: tuple[dict[str, Any], ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class CompositionRevision:
    draft_id: str
    composition_revision_id: str
    composition_identity_id: str
    book_revision_id: str
    content_hash: str
    kind: str
    members: tuple[dict[str, Any], ...]
    rendering: dict[str, Any] | None
    payload: dict[str, Any]


class GroundedKnowledgeStore:
    """Materialize and publish immutable knowledge for one pinned book revision."""

    def __init__(self, conn: sqlite3.Connection, root: Path, book_id: int, book_revision_id: str):
        self.conn = conn
        self.root = root.resolve()
        self.book_id = int(book_id)
        self.book_revision_id = str(book_revision_id)
        row = conn.execute(
            "SELECT id, book_id, contract_version, status, payload_json FROM book_revisions WHERE id = ?",
            (self.book_revision_id,),
        ).fetchone()
        if row is None or int(row["book_id"]) != self.book_id or row["contract_version"] != CONTRACT_VERSION:
            raise KnowledgeStoreError("book revision is not a pinned Grounded v2 revision")
        self._book_status = str(row["status"])
        evidence_rows = conn.execute(
            """SELECT id, source_path, chapter, source_file_hash, source_body_hash,
                      body_hash, revision_hash, body_path, source_line_start,
                      source_line_end, locator_json, payload_json
               FROM evidence_revisions WHERE book_revision_id = ?""",
            (self.book_revision_id,),
        ).fetchall()
        self.evidence: dict[str, dict[str, Any]] = {}
        for row in evidence_rows:
            value = dict(row)
            evidence_id = str(value["id"])
            try:
                value["locator"] = json.loads(value.pop("locator_json"))
                value["payload"] = json.loads(value.pop("payload_json"))
                revision, body = load_evidence_revision(
                    self.root,
                    evidence_id,
                    expected_source_file_hash=str(value["source_file_hash"]),
                )
            except (TypeError, ValueError, EvidenceRegistryError) as exc:
                raise KnowledgeStoreError(
                    f"evidence revision is not healthy: {evidence_id}"
                ) from exc
            if (
                revision != value["payload"]
                or revision.get("revision_hash") != value["revision_hash"]
                or revision.get("source_path") != value["source_path"]
                or int(revision.get("chapter", 0)) != int(value["chapter"])
                or revision.get("source_body_hash") != value["source_body_hash"]
                or revision.get("body_hash") != value["body_hash"]
                or revision.get("body_path") != value["body_path"]
                or int(revision.get("source_line_start", 0))
                != int(value["source_line_start"])
                or int(revision.get("source_line_end", 0))
                != int(value["source_line_end"])
                or revision.get("locator") != value["locator"]
                or revision.get("quote") != body
            ):
                raise KnowledgeStoreError(
                    f"database evidence revision mismatch: {evidence_id}"
                )
            self.evidence[evidence_id] = value
        if not self.evidence:
            raise KnowledgeStoreError("book revision has no evidence revisions")

    def validate_statement_draft(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        """Validate the normalized Grounded v2 statement draft shape."""
        value = _object(draft, "statement")
        expected = {
            "draft_id", "canonical_key", "kind", "subject", "truth_condition", "scope",
            "abstraction", "qualification", "confidence", "supports",
        }
        _exact_keys(value, expected, "statement")
        draft_id = _nonempty_string(value["draft_id"], "statement.draft_id")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", draft_id):
            raise KnowledgeStoreError("statement.draft_id has invalid format")
        canonical_key = _nonempty_string(value["canonical_key"], "statement.canonical_key")
        kind = _nonempty_string(value["kind"], "statement.kind")
        if kind not in STATEMENT_KINDS:
            raise KnowledgeStoreError("statement.kind is not controlled")
        subject = _nonempty_string(value["subject"], "statement.subject")
        truth_condition = _nonempty_string(value["truth_condition"], "statement.truth_condition")
        if truth_condition[-1] not in "。.!！？?":
            raise KnowledgeStoreError("statement.truth_condition must be a complete proposition")
        scope = _object(value["scope"], "statement.scope")
        _exact_keys(scope, {"documentary_scope", "target_type", "chapter"}, "statement.scope")
        if scope["documentary_scope"] not in {"span", "section", "reading_unit", "rollup", "book"}:
            raise KnowledgeStoreError("statement.scope.documentary_scope is not controlled")
        if scope["target_type"] not in {"book", "chapter", "entity", "statement", "evidence"}:
            raise KnowledgeStoreError("statement.scope.target_type is not controlled")
        chapter = scope["chapter"]
        if chapter is not None and (isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1):
            raise KnowledgeStoreError("statement.scope.chapter must be a positive integer or null")
        abstraction = _nonempty_string(value["abstraction"], "statement.abstraction")
        if abstraction not in {"concrete_detail", "local_generalization", "cross_scope_synthesis"}:
            raise KnowledgeStoreError("statement.abstraction is not controlled")
        qualification = value["qualification"]
        if qualification is not None:
            qualification = _nonempty_string(qualification, "statement.qualification")
        confidence = _nonempty_string(value["confidence"], "statement.confidence")
        if confidence not in {"high", "medium", "low"}:
            raise KnowledgeStoreError("statement.confidence is not controlled")
        supports = _list(value["supports"], "statement.supports")
        if not supports:
            raise KnowledgeStoreError("statement.supports must not be empty")
        normalized_supports = tuple(
            _validate_support(item, f"statement.supports[{index}]", self.evidence, self.root)
            for index, item in enumerate(supports)
        )
        if not any(item["support_type"] == "positive" for item in normalized_supports):
            raise KnowledgeStoreError("statement requires positive evidence closure")
        return {
            "draft_id": draft_id,
            "canonical_key": canonical_key,
            "kind": kind,
            "subject": subject,
            "truth_condition": truth_condition,
            "semantic_text": truth_condition,
            "scope": scope,
            "abstraction": abstraction,
            "qualification": qualification,
            "confidence": confidence,
            "supports": list(normalized_supports),
        }

    def _statement_ids(self, canonical_key: str, payload: dict[str, Any]) -> tuple[str, str, str]:
        identity_payload = {"book_id": self.book_id, "canonical_key": canonical_key}
        identity_id = f"sti-{_hash(identity_payload)[:32]}"
        material_payload = {
            "contract_version": CONTRACT_VERSION,
            "kind": "statement_revision",
            "statement_identity_id": identity_id,
            "book_revision_id": self.book_revision_id,
            "payload": payload,
        }
        content_hash = _hash(material_payload)
        revision_id = f"str-{content_hash[:32]}"
        return identity_id, revision_id, content_hash

    def materialize_statement(self, draft: Mapping[str, Any]) -> StatementRevision:
        payload = self.validate_statement_draft(draft)
        identity_id, revision_id, content_hash = self._statement_ids(payload["canonical_key"], payload)
        material_payload = {
            "contract_version": CONTRACT_VERSION,
            "kind": "statement_revision",
            "statement_identity_id": identity_id,
            "book_revision_id": self.book_revision_id,
            "payload": payload,
        }
        return StatementRevision(
            draft_id=payload["draft_id"],
            statement_revision_id=revision_id,
            statement_identity_id=identity_id,
            book_revision_id=self.book_revision_id,
            content_hash=content_hash,
            semantic_text=payload["semantic_text"],
            kind=payload["kind"],
            scope=payload["scope"],
            supports=tuple(payload["supports"]),
            payload=material_payload,
        )

    def validate_composition_draft(
        self,
        draft: Mapping[str, Any],
        statement_revisions: Mapping[str, StatementRevision],
        composition_revisions: Mapping[str, CompositionRevision] | None = None,
    ) -> dict[str, Any]:
        value = _object(draft, "composition")
        _exact_keys(value, {"draft_id", "canonical_key", "kind", "members", "rendering"}, "composition")
        draft_id = _nonempty_string(value["draft_id"], "composition.draft_id")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", draft_id):
            raise KnowledgeStoreError("composition.draft_id has invalid format")
        canonical_key = _nonempty_string(value["canonical_key"], "composition.canonical_key")
        kind = _nonempty_string(value["kind"], "composition.kind")
        if kind not in COMPOSITION_KINDS:
            raise KnowledgeStoreError("composition.kind is not controlled")
        members = _list(value["members"], "composition.members")
        if not members:
            raise KnowledgeStoreError("composition.members must not be empty")
        normalized_members: list[dict[str, Any]] = []
        children = composition_revisions or {}
        for index, member in enumerate(members):
            item = _object(member, f"composition.members[{index}]")
            _exact_keys(item, {"member_type", "draft_id", "role"}, f"composition.members[{index}]")
            member_type = _nonempty_string(item["member_type"], f"composition.members[{index}].member_type")
            revision_id = _nonempty_string(item["draft_id"], f"composition.members[{index}].draft_id")
            role = _nonempty_string(item["role"], f"composition.members[{index}].role")
            if member_type == "statement":
                if revision_id not in statement_revisions:
                    raise KnowledgeStoreError(f"composition member references unknown statement: {revision_id}")
                if not statement_revisions[revision_id].supports:
                    raise KnowledgeStoreError("composition cannot include unsupported statement")
            elif member_type == "composition":
                if revision_id not in children:
                    raise KnowledgeStoreError(f"composition member references unknown composition: {revision_id}")
            else:
                raise KnowledgeStoreError(f"composition.members[{index}].member_type is not controlled")
            normalized_members.append({"member_type": member_type, "draft_id": revision_id, "revision_id": revision_id, "role": role})
        rendering = value["rendering"]
        normalized_rendering: dict[str, Any] | None = None
        if rendering is not None:
            rendering_value = _object(rendering, "composition.rendering")
            _exact_keys(rendering_value, {"text", "span_map"}, "composition.rendering")
            normalized_rendering = {
                "text": _nonempty_string(rendering_value["text"], "composition.rendering.text"),
                "span_map": _object(rendering_value["span_map"], "composition.rendering.span_map"),
            }
        return {
            "draft_id": draft_id,
            "canonical_key": canonical_key,
            "kind": kind,
            "members": normalized_members,
            "rendering": normalized_rendering,
        }

    def materialize_composition(
        self,
        draft: Mapping[str, Any],
        statement_revisions: Mapping[str, StatementRevision],
        composition_revisions: Mapping[str, CompositionRevision] | None = None,
    ) -> CompositionRevision:
        payload = self.validate_composition_draft(draft, statement_revisions, composition_revisions)
        identity_payload = {"book_id": self.book_id, "canonical_key": payload["canonical_key"]}
        identity_id = f"cmi-{_hash(identity_payload)[:32]}"
        material_payload = {
            "contract_version": CONTRACT_VERSION,
            "kind": "composition_revision",
            "composition_identity_id": identity_id,
            "book_revision_id": self.book_revision_id,
            "payload": payload,
        }
        content_hash = _hash(material_payload)
        revision_id = f"cmpr-{content_hash[:32]}"
        return CompositionRevision(
            draft_id=payload["draft_id"],
            composition_revision_id=revision_id,
            composition_identity_id=identity_id,
            book_revision_id=self.book_revision_id,
            content_hash=content_hash,
            kind=payload["kind"],
            members=tuple(payload["members"]),
            rendering=payload["rendering"],
            payload=material_payload,
        )

    def _insert_statement(self, revision: StatementRevision, now: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO statement_identities (id, book_id, canonical_key, created_at) VALUES (?, ?, ?, ?)",
            (revision.statement_identity_id, self.book_id, revision.payload["payload"]["canonical_key"], now),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO statement_revisions
               (id, statement_identity_id, book_revision_id, content_hash, semantic_text, kind,
                scope_json, payload_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staging', ?)""",
            (
                revision.statement_revision_id,
                revision.statement_identity_id,
                self.book_revision_id,
                revision.content_hash,
                revision.semantic_text,
                revision.kind,
                json.dumps(revision.scope, ensure_ascii=False, sort_keys=True),
                json.dumps(revision.payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        closure: dict[str, bool] = {}
        for support in revision.supports:
            self.conn.execute(
                """INSERT OR IGNORE INTO support_edges
                   (statement_revision_id, evidence_revision_id, support_type, span_map_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    revision.statement_revision_id,
                    support["evidence_revision_id"],
                    support["support_type"],
                    json.dumps(support["span_map"], ensure_ascii=False, sort_keys=True),
                ),
            )
            evidence_id = support["evidence_revision_id"]
            closure[evidence_id] = closure.get(evidence_id, False) or support["support_type"] == "positive"
        for evidence_id, positive in closure.items():
            self.conn.execute(
                """INSERT INTO evidence_closure
                   (statement_revision_id, evidence_revision_id, distance, positive)
                   VALUES (?, ?, 0, ?)
                   ON CONFLICT(statement_revision_id, evidence_revision_id)
                   DO UPDATE SET positive = MAX(evidence_closure.positive, excluded.positive)""",
                (revision.statement_revision_id, evidence_id, int(positive)),
            )

    def _insert_composition(
        self,
        revision: CompositionRevision,
        statement_revisions: Mapping[str, StatementRevision],
        composition_revisions: Mapping[str, CompositionRevision],
        now: str,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO composition_identities (id, book_id, canonical_key, created_at) VALUES (?, ?, ?, ?)",
            (revision.composition_identity_id, self.book_id, revision.payload["payload"]["canonical_key"], now),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO composition_revisions
               (id, composition_identity_id, book_revision_id, content_hash, kind, payload_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'staging', ?)""",
            (
                revision.composition_revision_id,
                revision.composition_identity_id,
                self.book_revision_id,
                revision.content_hash,
                revision.kind,
                json.dumps(revision.payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        for ordinal, member in enumerate(revision.members):
            self.conn.execute(
                """INSERT OR IGNORE INTO composition_members
                   (composition_revision_id, ordinal, member_type, statement_revision_id,
                    child_composition_revision_id, role)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    revision.composition_revision_id,
                    ordinal,
                    member["member_type"],
                    member["revision_id"] if member["member_type"] == "statement" else None,
                    member["revision_id"] if member["member_type"] == "composition" else None,
                    member["role"],
                ),
            )
        if revision.rendering is not None:
            rendering_hash = _hash(revision.rendering)
            rendering_id = f"rend-{rendering_hash[:32]}"
            self.conn.execute(
                """INSERT OR IGNORE INTO composition_renderings
                   (id, composition_revision_id, content_hash, rendering_text, span_map_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    rendering_id,
                    revision.composition_revision_id,
                    rendering_hash,
                    revision.rendering["text"],
                    json.dumps(revision.rendering["span_map"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

    def publish(
        self,
        statement_drafts: Sequence[Mapping[str, Any]] = (),
        composition_drafts: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[tuple[StatementRevision, ...], tuple[CompositionRevision, ...]]:
        """Materialize all drafts, validate closure, and atomically publish them."""
        import time

        statements: dict[str, StatementRevision] = {}
        compositions: dict[str, CompositionRevision] = {}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            for draft in statement_drafts:
                revision = self.materialize_statement(draft)
                existing = statements.setdefault(revision.statement_revision_id, revision)
                if existing != revision:
                    raise KnowledgeStoreError("statement revision collision")
            statement_by_draft = {
                revision.draft_id: revision for revision in statements.values()
            }
            composition_by_draft: dict[str, CompositionRevision] = {}
            pending_compositions = [dict(draft) for draft in composition_drafts]
            while pending_compositions:
                progressed = False
                remaining: list[dict[str, Any]] = []
                for draft in pending_compositions:
                    member_ids = [
                        str(member.get("draft_id", ""))
                        for member in draft.get("members", [])
                        if isinstance(member, Mapping)
                        and member.get("member_type") == "composition"
                    ]
                    if any(
                        child_id not in composition_by_draft
                        for child_id in member_ids
                    ):
                        remaining.append(draft)
                        continue
                    revision = self.materialize_composition(
                        draft,
                        statement_by_draft,
                        composition_by_draft,
                    )
                    resolved_members = []
                    for member in revision.members:
                        if member["member_type"] == "statement":
                            target = statement_by_draft.get(member["draft_id"])
                            if target is None:
                                raise KnowledgeStoreError(
                                    f"composition member draft is unavailable: {member['draft_id']}"
                                )
                            resolved_members.append(
                                {**member, "revision_id": target.statement_revision_id}
                            )
                        else:
                            target = composition_by_draft.get(member["draft_id"])
                            if target is None:
                                raise KnowledgeStoreError(
                                    f"composition child draft is unavailable: {member['draft_id']}"
                                )
                            resolved_members.append(
                                {**member, "revision_id": target.composition_revision_id}
                            )
                    resolved_payload = {
                        **revision.payload["payload"],
                        "members": resolved_members,
                    }
                    material_payload = {
                        "contract_version": CONTRACT_VERSION,
                        "kind": "composition_revision",
                        "composition_identity_id": revision.composition_identity_id,
                        "book_revision_id": self.book_revision_id,
                        "payload": resolved_payload,
                    }
                    resolved_hash = _hash(material_payload)
                    revision = CompositionRevision(
                        draft_id=revision.draft_id,
                        composition_revision_id=f"cmpr-{resolved_hash[:32]}",
                        composition_identity_id=revision.composition_identity_id,
                        book_revision_id=self.book_revision_id,
                        content_hash=resolved_hash,
                        kind=revision.kind,
                        members=tuple(resolved_members),
                        rendering=revision.rendering,
                        payload=material_payload,
                    )
                    existing = compositions.setdefault(
                        revision.composition_revision_id, revision
                    )
                    if existing != revision:
                        raise KnowledgeStoreError("composition revision collision")
                    composition_by_draft[revision.draft_id] = revision
                    progressed = True
                if not progressed:
                    unresolved = sorted(
                        str(draft.get("draft_id", ""))
                        for draft in remaining
                    )
                    raise KnowledgeStoreError(
                        "composition dependency cycle or unavailable child: "
                        + ", ".join(unresolved)
                    )
                pending_compositions = remaining
            if not statements and not compositions:
                raise KnowledgeStoreError("cannot publish an empty knowledge set")
            self.conn.execute("BEGIN IMMEDIATE")
            for revision in statements.values():
                self._insert_statement(revision, now)
            for revision in compositions.values():
                self._insert_composition(revision, statements, compositions, now)
            for revision in statements.values():
                self.conn.execute(
                    "UPDATE statement_revisions SET status = 'healthy' WHERE id = ? AND book_revision_id = ?",
                    (revision.statement_revision_id, self.book_revision_id),
                )
            for revision in compositions.values():
                self.conn.execute(
                    "UPDATE composition_revisions SET status = 'healthy' WHERE id = ? AND book_revision_id = ?",
                    (revision.composition_revision_id, self.book_revision_id),
                )
            health = {
                "status": "healthy",
                "book_revision_id": self.book_revision_id,
                "statement_revision_ids": sorted(statements),
                "composition_revision_ids": sorted(compositions),
            }
            self.conn.execute(
                "UPDATE statement_revisions SET status = 'superseded' "
                "WHERE book_revision_id = ? AND status = 'healthy' "
                "AND id NOT IN ({})".format(
                    ",".join("?" for _ in statements) or "''"
                ),
                (self.book_revision_id, *sorted(statements)),
            )
            self.conn.execute(
                "UPDATE composition_revisions SET status = 'superseded' "
                "WHERE book_revision_id = ? AND status = 'healthy' "
                "AND id NOT IN ({})".format(
                    ",".join("?" for _ in compositions) or "''"
                ),
                (self.book_revision_id, *sorted(compositions)),
            )
            self.conn.execute(
                "UPDATE book_revisions SET status = 'healthy' WHERE id = ?",
                (self.book_revision_id,),
            )
            self.conn.execute(
                """UPDATE books SET active_book_revision_id = ?, publication_health_json = ?, updated_at = ?
                   WHERE id = ?""",
                (self.book_revision_id, json.dumps(health, ensure_ascii=False, sort_keys=True), now, self.book_id),
            )
            self.conn.commit()
            return tuple(statements.values()), tuple(compositions.values())
        except Exception:
            self.conn.rollback()
            raise


def positive_evidence_closure(conn: sqlite3.Connection, statement_revision_id: str) -> set[str]:
    """Return only evidence reachable through positive support edges."""
    rows = conn.execute(
        """SELECT evidence_revision_id FROM evidence_closure
           WHERE statement_revision_id = ? AND positive = 1""",
        (statement_revision_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}
