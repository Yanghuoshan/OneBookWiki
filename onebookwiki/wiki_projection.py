"""Fail-closed static Wiki projection of a healthy Grounded v2 revision.

The SQLite Grounded Knowledge Store is the semantic source of truth.  This
module deliberately does not read generation artifacts or rendered Markdown to
reconstruct knowledge: it validates a healthy, active revision and then emits
only display/navigation projections from its immutable records.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .evidence_registry import EvidenceRegistryError, load_evidence_revision
from .knowledge_store import CONTRACT_VERSION


class WikiProjectionError(RuntimeError):
    """A Grounded v2 revision cannot safely be projected to a Wiki."""


def _json(value: object, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise WikiProjectionError(f"{label} is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise WikiProjectionError(f"{label} must be a JSON object")
    return decoded


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if normalized:
        return normalized
    import hashlib

    return f"item-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}"


def _citation(evidence_revision_id: str) -> str:
    return f"[source](onebookwiki://evidence/{evidence_revision_id})"


def _source_metadata(root: Path) -> dict[str, Any]:
    path = root / ".onebookwiki" / "source.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _unit_metadata(source: dict[str, Any], chapter: int | None) -> dict[str, Any]:
    if chapter is None:
        return {}
    structure = source.get("source_structure")
    if not isinstance(structure, dict):
        return {}
    for raw in structure.get("units") or []:
        if not isinstance(raw, dict):
            continue
        try:
            if int(raw.get("chapter", 0)) == chapter:
                return dict(raw)
        except (TypeError, ValueError):
            continue
    return {}


def _safe_object_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WikiProjectionError(f"{label} must be an array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise WikiProjectionError(f"{label}[{index}] must be an object")
        result.append(dict(item))
    return result


@dataclass(frozen=True)
class ProjectedEvidence:
    evidence_revision_id: str
    source_path: str
    chapter: int
    source_line_start: int
    source_line_end: int
    locator: dict[str, Any]
    quote: str
    source_file_hash: str


@dataclass(frozen=True)
class ProjectedStatement:
    statement_revision_id: str
    canonical_key: str
    semantic_text: str
    kind: str
    scope: dict[str, Any]
    evidence_revision_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProjectedComposition:
    composition_revision_id: str
    canonical_key: str
    kind: str
    rendering_text: str | None
    statement_revision_ids: tuple[str, ...]
    child_composition_revision_ids: tuple[str, ...]
    evidence_revision_ids: tuple[str, ...]
    chapters: tuple[int, ...]


@dataclass(frozen=True)
class GroundedWikiProjection:
    book_id: int
    title: str
    book_revision_id: str
    statements: dict[str, ProjectedStatement]
    compositions: dict[str, ProjectedComposition]
    evidence: dict[str, ProjectedEvidence]

    @property
    def rendered_compositions(self) -> tuple[ProjectedComposition, ...]:
        return tuple(
            item
            for _, item in sorted(self.compositions.items(), key=lambda pair: (pair[1].canonical_key, pair[0]))
            if item.rendering_text
        )


def _validate_active_revision(
    conn: sqlite3.Connection, book_id: int, expected_revision_id: str | None
) -> tuple[str, str]:
    row = conn.execute(
        """SELECT b.title, b.contract_version AS book_contract, b.active_book_revision_id,
                  b.publication_health_json, br.id, br.book_id AS revision_book_id,
                  br.contract_version AS revision_contract, br.status AS revision_status
           FROM books AS b
           LEFT JOIN book_revisions AS br ON br.id = b.active_book_revision_id
           WHERE b.id = ?""",
        (int(book_id),),
    ).fetchone()
    if row is None:
        raise WikiProjectionError("book does not exist")
    revision_id = str(row["id"] or "")
    if (
        row["book_contract"] != CONTRACT_VERSION
        or row["revision_contract"] != CONTRACT_VERSION
        or row["revision_status"] != "healthy"
        or int(row["revision_book_id"] or 0) != int(book_id)
        or not revision_id
    ):
        raise WikiProjectionError("book has no healthy active Grounded v2 revision")
    if expected_revision_id is not None and revision_id != str(expected_revision_id):
        raise WikiProjectionError("active book revision does not match the expected revision")
    health = _json(row["publication_health_json"], "book publication health")
    if health.get("status") != "healthy" or health.get("book_revision_id") != revision_id:
        raise WikiProjectionError("book publication health does not match the active revision")
    return revision_id, str(row["title"] or "")


def _load_evidence(
    conn: sqlite3.Connection, root: Path, revision_id: str
) -> dict[str, ProjectedEvidence]:
    rows = conn.execute(
        """SELECT id, source_path, chapter, source_file_hash, source_body_hash, body_hash,
                  revision_hash, body_path, source_line_start, source_line_end, locator_json,
                  payload_json
           FROM evidence_revisions WHERE book_revision_id = ? ORDER BY id""",
        (revision_id,),
    ).fetchall()
    if not rows:
        raise WikiProjectionError("active revision has no evidence revisions")
    evidence: dict[str, ProjectedEvidence] = {}
    for row in rows:
        evidence_id = str(row["id"])
        payload = _json(row["payload_json"], f"evidence payload {evidence_id}")
        locator = _json(row["locator_json"], f"evidence locator {evidence_id}")
        try:
            registry_payload, body = load_evidence_revision(
                root, evidence_id, expected_source_file_hash=str(row["source_file_hash"])
            )
        except EvidenceRegistryError as exc:
            raise WikiProjectionError(f"evidence revision is not healthy: {evidence_id}") from exc
        if payload != registry_payload:
            raise WikiProjectionError(f"database evidence revision mismatch: {evidence_id}")
        for key in (
            "revision_hash", "source_path", "source_body_hash", "body_hash", "body_path",
            "source_line_start", "source_line_end",
        ):
            if str(registry_payload.get(key)) != str(row[key]):
                raise WikiProjectionError(f"database evidence metadata mismatch: {evidence_id}")
        if registry_payload.get("locator") != locator or registry_payload.get("quote") != body:
            raise WikiProjectionError(f"database evidence locator/body mismatch: {evidence_id}")
        evidence[evidence_id] = ProjectedEvidence(
            evidence_revision_id=evidence_id,
            source_path=str(row["source_path"]),
            chapter=int(row["chapter"]),
            source_line_start=int(row["source_line_start"]),
            source_line_end=int(row["source_line_end"]),
            locator=locator,
            quote=body,
            source_file_hash=str(row["source_file_hash"]),
        )
    return evidence


def _load_statements(
    conn: sqlite3.Connection, revision_id: str, evidence: dict[str, ProjectedEvidence]
) -> dict[str, ProjectedStatement]:
    rows = conn.execute(
        """SELECT sr.id, si.canonical_key, sr.semantic_text, sr.kind, sr.scope_json,
                  sr.payload_json, sr.status
           FROM statement_revisions AS sr
           JOIN statement_identities AS si ON si.id = sr.statement_identity_id
           WHERE sr.book_revision_id = ? ORDER BY si.canonical_key, sr.id""",
        (revision_id,),
    ).fetchall()
    if not rows:
        raise WikiProjectionError("active revision has no statement revisions")
    statements: dict[str, ProjectedStatement] = {}
    for row in rows:
        statement_id = str(row["id"])
        if row["status"] != "healthy":
            raise WikiProjectionError(f"statement revision is not healthy: {statement_id}")
        scope = _json(row["scope_json"], f"statement scope {statement_id}")
        payload = _json(row["payload_json"], f"statement payload {statement_id}")
        draft = payload.get("payload")
        if not isinstance(draft, dict) or draft.get("canonical_key") != row["canonical_key"]:
            raise WikiProjectionError(f"statement payload identity mismatch: {statement_id}")
        if str(draft.get("semantic_text", draft.get("truth_condition", ""))) != str(row["semantic_text"]):
            raise WikiProjectionError(f"statement semantic text mismatch: {statement_id}")
        closure_rows = conn.execute(
            """SELECT evidence_revision_id FROM evidence_closure
               WHERE statement_revision_id = ? AND positive = 1 ORDER BY evidence_revision_id""",
            (statement_id,),
        ).fetchall()
        evidence_ids = tuple(str(item["evidence_revision_id"]) for item in closure_rows)
        if not evidence_ids or any(item not in evidence for item in evidence_ids):
            raise WikiProjectionError(f"statement has invalid positive evidence closure: {statement_id}")
        statements[statement_id] = ProjectedStatement(
            statement_revision_id=statement_id,
            canonical_key=str(row["canonical_key"]),
            semantic_text=str(row["semantic_text"]),
            kind=str(row["kind"]),
            scope=scope,
            evidence_revision_ids=evidence_ids,
        )
    return statements


def _composition_members(conn: sqlite3.Connection, revision_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """SELECT cm.composition_revision_id, cm.ordinal, cm.member_type,
                  cm.statement_revision_id, cm.child_composition_revision_id, cm.role
           FROM composition_members AS cm
           JOIN composition_revisions AS cr ON cr.id = cm.composition_revision_id
           WHERE cr.book_revision_id = ? ORDER BY cm.composition_revision_id, cm.ordinal""",
        (revision_id,),
    ).fetchall()
    members: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = str(row["composition_revision_id"])
        entry = dict(row)
        expected = len(members.setdefault(parent, []))
        if int(entry["ordinal"]) != expected:
            raise WikiProjectionError(f"composition member ordinals are not contiguous: {parent}")
        members[parent].append(entry)
    return members


def _load_compositions(
    conn: sqlite3.Connection,
    revision_id: str,
    statements: dict[str, ProjectedStatement],
) -> dict[str, ProjectedComposition]:
    rows = conn.execute(
        """SELECT cr.id, ci.canonical_key, cr.kind, cr.payload_json, cr.status,
                  r.rendering_text, r.span_map_json, r.id AS rendering_id
           FROM composition_revisions AS cr
           JOIN composition_identities AS ci ON ci.id = cr.composition_identity_id
           LEFT JOIN composition_renderings AS r ON r.composition_revision_id = cr.id
           WHERE cr.book_revision_id = ? ORDER BY ci.canonical_key, cr.id, r.id""",
        (revision_id,),
    ).fetchall()
    if not rows:
        raise WikiProjectionError("active revision has no composition revisions")
    raw: dict[str, dict[str, Any]] = {}
    for row in rows:
        composition_id = str(row["id"])
        if composition_id in raw:
            raise WikiProjectionError(f"composition has multiple renderings: {composition_id}")
        raw[composition_id] = dict(row)
    members = _composition_members(conn, revision_id)
    working: dict[str, dict[str, Any]] = {}
    for composition_id, row in raw.items():
        if row["status"] != "healthy":
            raise WikiProjectionError(f"composition revision is not healthy: {composition_id}")
        payload = _json(row["payload_json"], f"composition payload {composition_id}")
        draft = payload.get("payload")
        if not isinstance(draft, dict) or draft.get("canonical_key") != row["canonical_key"]:
            raise WikiProjectionError(f"composition payload identity mismatch: {composition_id}")
        rows_for_composition = members.get(composition_id, [])
        if not rows_for_composition:
            raise WikiProjectionError(f"composition has no members: {composition_id}")
        statement_ids: list[str] = []
        child_ids: list[str] = []
        for member in rows_for_composition:
            if member["member_type"] == "statement":
                statement_id = str(member["statement_revision_id"] or "")
                if statement_id not in statements:
                    raise WikiProjectionError(f"composition references a non-active statement: {composition_id}")
                statement_ids.append(statement_id)
            elif member["member_type"] == "composition":
                child_id = str(member["child_composition_revision_id"] or "")
                if not child_id:
                    raise WikiProjectionError(f"composition has an invalid child: {composition_id}")
                child_ids.append(child_id)
            else:
                raise WikiProjectionError(f"composition has an invalid member type: {composition_id}")
        rendering_text = row["rendering_text"]
        if rendering_text is not None:
            if not isinstance(rendering_text, str) or not rendering_text.strip():
                raise WikiProjectionError(f"composition rendering is invalid: {composition_id}")
            _json(row["span_map_json"], f"composition rendering span map {composition_id}")
        working[composition_id] = {
            "canonical_key": str(row["canonical_key"]),
            "kind": str(row["kind"]),
            "rendering_text": str(rendering_text) if rendering_text is not None else None,
            "statement_ids": tuple(statement_ids),
            "child_ids": tuple(child_ids),
        }
    for composition_id, value in working.items():
        for child_id in value["child_ids"]:
            if child_id not in working:
                raise WikiProjectionError(f"composition references a non-active child: {composition_id}")

    resolved: dict[str, ProjectedComposition] = {}
    visiting: set[str] = set()

    def visit(composition_id: str) -> ProjectedComposition:
        if composition_id in resolved:
            return resolved[composition_id]
        if composition_id in visiting:
            raise WikiProjectionError(f"composition dependency cycle: {composition_id}")
        visiting.add(composition_id)
        value = working[composition_id]
        statement_ids = tuple(sorted(set(value["statement_ids"])))
        evidence_ids: set[str] = set()
        chapters: set[int] = set()
        for statement_id in statement_ids:
            statement = statements[statement_id]
            evidence_ids.update(statement.evidence_revision_ids)
            chapter = statement.scope.get("chapter")
            if isinstance(chapter, int) and chapter > 0:
                chapters.add(chapter)
        for child_id in value["child_ids"]:
            child = visit(child_id)
            statement_ids = tuple(sorted({*statement_ids, *child.statement_revision_ids}))
            evidence_ids.update(child.evidence_revision_ids)
            chapters.update(child.chapters)
        result = ProjectedComposition(
            composition_revision_id=composition_id,
            canonical_key=value["canonical_key"],
            kind=value["kind"],
            rendering_text=value["rendering_text"],
            statement_revision_ids=statement_ids,
            child_composition_revision_ids=tuple(value["child_ids"]),
            evidence_revision_ids=tuple(sorted(evidence_ids)),
            chapters=tuple(sorted(chapters)),
        )
        visiting.remove(composition_id)
        resolved[composition_id] = result
        return result

    for composition_id in sorted(working):
        visit(composition_id)
    if not any(item.rendering_text for item in resolved.values()):
        raise WikiProjectionError("active revision has no canonical composition renderings")
    return resolved


def load_grounded_wiki_projection(
    conn: sqlite3.Connection,
    root: Path,
    book_id: int,
    *,
    expected_revision_id: str | None = None,
) -> GroundedWikiProjection:
    """Load one fully validated active Grounded v2 revision or fail closed."""
    root = root.resolve()
    revision_id, title = _validate_active_revision(conn, int(book_id), expected_revision_id)
    evidence = _load_evidence(conn, root, revision_id)
    statements = _load_statements(conn, revision_id, evidence)
    compositions = _load_compositions(conn, revision_id, statements)
    return GroundedWikiProjection(
        book_id=int(book_id),
        title=title,
        book_revision_id=revision_id,
        statements=statements,
        compositions=compositions,
        evidence=evidence,
    )


def _outline_with_pages(nodes: Iterable[object], page_ids_by_chapter: dict[int, list[str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        node = dict(raw)
        attached: list[str] = []
        for chapter in node.get("chapters") or []:
            try:
                attached.extend(page_ids_by_chapter.get(int(chapter), []))
            except (TypeError, ValueError):
                continue
        # Importers may have already resolved page IDs; retain only new projection pages.
        node["pageIds"] = sorted(set(attached))
        node["children"] = _outline_with_pages(node.get("children") or [], page_ids_by_chapter)
        result.append(node)
    return result


def _composition_page_path(composition: ProjectedComposition) -> tuple[str, str]:
    slug = _slug(composition.canonical_key)
    # One page per canonical composition revision.  A chapter-local composition
    # receives a chapter route; all other scopes are derived knowledge pages.
    if len(composition.chapters) == 1:
        return f"chapters/{composition.chapters[0]:02d}-{slug}-{composition.composition_revision_id[-8:]}.md", "reading_unit"
    return f"knowledge/{slug}-{composition.composition_revision_id[-8:]}.md", "knowledge"


def render_grounded_projection(
    conn: sqlite3.Connection,
    root: Path,
    book_id: int,
    *,
    expected_revision_id: str | None = None,
) -> list[Path]:
    """Render static Wiki files from a validated Grounded v2 projection.

    All validation happens before the first output file is written, preventing a
    stale/unhealthy revision from replacing a previously healthy projection.
    """
    projection = load_grounded_wiki_projection(
        conn, root, book_id, expected_revision_id=expected_revision_id
    )
    source = _source_metadata(root)
    wiki = root / "wiki"
    rendered = projection.rendered_compositions
    page_rows: list[dict[str, Any]] = []
    rendered_paths: list[Path] = []
    page_ids_by_chapter: dict[int, list[str]] = {}

    # The most broadly scoped canonical rendering serves as the book synopsis;
    # canonical-key ordering makes the fallback deterministic.
    book_composition = next(
        (item for item in rendered if not item.chapters),
        rendered[0],
    )
    for item in rendered:
        path, kind = _composition_page_path(item)
        page_id = f"composition-{item.composition_revision_id}"
        chapter = item.chapters[0] if len(item.chapters) == 1 else None
        unit = _unit_metadata(source, chapter)
        citations = "\n".join(
            f"- {_citation(evidence_id)} · {projection.evidence[evidence_id].source_path}:"
            f"{projection.evidence[evidence_id].source_line_start}-"
            f"{projection.evidence[evidence_id].source_line_end}"
            for evidence_id in item.evidence_revision_ids
        ) or "- No positive evidence closure is available."
        statements = "\n".join(
            f"- {projection.statements[statement_id].semantic_text}"
            for statement_id in item.statement_revision_ids
        ) or "- No grounded statements are available."
        target = wiki / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"# {item.canonical_key}\n\n"
            f"> Grounded revision: `{projection.book_revision_id}`\n"
            f"> Composition revision: `{item.composition_revision_id}`\n"
            + (f"> Chapter: {chapter}\n" if chapter is not None else "")
            + "\n## Canonical rendering\n\n"
            + f"{item.rendering_text}\n\n"
            + "## Grounded statements\n\n"
            + f"{statements}\n\n"
            + "## Evidence\n\n"
            + f"{citations}\n",
            encoding="utf-8",
        )
        rendered_paths.append(target)
        row = {
            "id": page_id,
            "title": item.canonical_key,
            "path": path,
            "kind": kind,
            "bookTitle": projection.title,
            "bookRevisionId": projection.book_revision_id,
            "compositionRevisionIds": [item.composition_revision_id],
            "statementRevisionIds": list(item.statement_revision_ids),
            "evidenceRevisionIds": list(item.evidence_revision_ids),
            "relatedPages": ["book", "index"],
        }
        if chapter is not None:
            row.update({
                "chapter": chapter,
                "sourceUnitId": str(unit.get("source_unit_id", "")),
                "sourceTitle": str(unit.get("source_title") or unit.get("title") or item.canonical_key),
                "sourceKind": str(unit.get("kind", "")),
                "breadcrumb": [str(value) for value in unit.get("breadcrumb", [])],
                "physicalPageStart": unit.get("physical_page_start"),
                "physicalPageEnd": unit.get("physical_page_end"),
                "spine": unit.get("spine") or None,
                "spineIndex": unit.get("spine_index"),
                "href": unit.get("href") or None,
                "fragment": unit.get("fragment") or None,
                "sourceUnitLocator": dict(unit.get("locator") or {}),
                "part": unit.get("part"),
                "partCount": unit.get("part_count"),
                "rawSources": sorted({projection.evidence[item_id].source_path for item_id in item.evidence_revision_ids}),
            })
            page_ids_by_chapter.setdefault(chapter, []).append(page_id)
        page_rows.append(row)

    book_evidence = "\n".join(_citation(item) for item in book_composition.evidence_revision_ids)
    book_page = wiki / "book.md"
    book_page.parent.mkdir(parents=True, exist_ok=True)
    book_page.write_text(
        f"# {projection.title}\n\n"
        f"> Grounded revision: `{projection.book_revision_id}`\n\n"
        "## Canonical overview\n\n"
        f"{book_composition.rendering_text}\n\n"
        "## Evidence\n\n"
        f"{book_evidence or 'No positive evidence closure is available.'}\n",
        encoding="utf-8",
    )
    index_page = wiki / "index.md"
    index_page.write_text(
        f"# Reading map: {projection.title}\n\n"
        f"> Grounded revision: `{projection.book_revision_id}`\n\n"
        "## Canonical knowledge pages\n\n"
        + "\n".join(f"- [{item['title']}]({item['path']})" for item in page_rows)
        + "\n",
        encoding="utf-8",
    )
    rendered_paths.extend([book_page, index_page])

    pages = [
        {
            "id": "book", "title": projection.title, "path": "book.md", "kind": "book",
            "bookRevisionId": projection.book_revision_id,
            "compositionRevisionIds": [book_composition.composition_revision_id],
            "statementRevisionIds": list(book_composition.statement_revision_ids),
            "evidenceRevisionIds": list(book_composition.evidence_revision_ids),
            "relatedPages": ["index", *[item["id"] for item in page_rows]],
        },
        {
            "id": "index", "title": "Reading Map", "path": "index.md", "kind": "index",
            "bookRevisionId": projection.book_revision_id,
            "relatedPages": ["book", *[item["id"] for item in page_rows]],
        },
        *page_rows,
    ]
    source_structure = source.get("source_structure") if isinstance(source.get("source_structure"), dict) else {}
    structure = {
        "id": "grounded-v2-wiki",
        "contractVersion": CONTRACT_VERSION,
        "projectionStatus": "healthy",
        "bookRevisionId": projection.book_revision_id,
        "title": projection.title,
        "description": book_composition.rendering_text,
        "sourceOutline": _outline_with_pages(source_structure.get("outline") or [], page_ids_by_chapter),
        "pages": pages,
        "sections": [
            {"id": "overview", "title": "Overview", "pages": ["book", "index"]},
            {"id": "knowledge", "title": "Grounded Knowledge", "pages": [item["id"] for item in page_rows]},
        ],
    }
    evidence_records: dict[str, dict[str, Any]] = {}
    evidence_to_statements: dict[str, list[str]] = {item: [] for item in projection.evidence}
    evidence_to_compositions: dict[str, list[str]] = {item: [] for item in projection.evidence}
    for statement in projection.statements.values():
        for evidence_id in statement.evidence_revision_ids:
            evidence_to_statements[evidence_id].append(statement.statement_revision_id)
    for composition in projection.compositions.values():
        for evidence_id in composition.evidence_revision_ids:
            evidence_to_compositions[evidence_id].append(composition.composition_revision_id)
    for evidence_id, item in sorted(projection.evidence.items()):
        unit = _unit_metadata(source, item.chapter)
        evidence_records[evidence_id] = {
            "evidence_id": evidence_id,
            "evidenceRevisionId": evidence_id,
            "bookRevisionId": projection.book_revision_id,
            "source_path": item.source_path,
            "chapter": item.chapter,
            "start_line": item.source_line_start,
            "end_line": item.source_line_end,
            "quote": item.quote,
            "excerpt": item.quote,
            "excerpt_start_line": item.source_line_start,
            "excerpt_end_line": item.source_line_end,
            "excerpt_truncated": False,
            "source_hash": item.source_file_hash,
            "book_title": projection.title,
            "source_title": str(unit.get("source_title") or unit.get("title") or ""),
            "breadcrumb": [str(value) for value in unit.get("breadcrumb", [])],
            "source_type": str(unit.get("kind", "")),
            "physical_page_start": unit.get("physical_page_start"),
            "physical_page_end": unit.get("physical_page_end"),
            "spine": unit.get("spine") or None,
            "spine_index": unit.get("spine_index"),
            "href": unit.get("href") or None,
            "fragment": unit.get("fragment") or None,
            "locator": item.locator,
            "source_unit_locator": dict(unit.get("locator") or {}),
            "display_label": f"Chapter {item.chapter}, lines {item.source_line_start}-{item.source_line_end}",
            "statementRevisionIds": sorted(evidence_to_statements[evidence_id]),
            "compositionRevisionIds": sorted(evidence_to_compositions[evidence_id]),
        }
    evidence_index = {
        "schema_version": 3,
        "contractVersion": CONTRACT_VERSION,
        "projectionStatus": "healthy",
        "bookRevisionId": projection.book_revision_id,
        "evidence": evidence_records,
    }
    structure_path = wiki / "structure.json"
    evidence_path = wiki / "evidence.json"
    structure_path.write_text(json.dumps(structure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rendered_paths.extend([structure_path, evidence_path])
    return rendered_paths
