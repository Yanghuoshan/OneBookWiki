"""Structured artifacts produced by the evidence-grounded book pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    chunk_id: str
    source_path: str
    chapter: int
    start_line: int
    end_line: int
    quote: str = ""
    page: int | None = None
    spine: str | None = None
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Claim:
    text: str
    evidence_ids: tuple[str, ...] = ()
    kind: str = "interpretation"
    confidence: str = "medium"
    qualification: str = ""


@dataclass
class ChapterInterpretation:
    chapter: int
    title: str
    source_unit_id: str = ""
    source_title: str = ""
    source_type: str = ""
    breadcrumb: list[str] = field(default_factory=list)
    physical_page_start: int | None = None
    physical_page_end: int | None = None
    spine: str = ""
    spine_index: int | None = None
    href: str = ""
    fragment: str = ""
    structure_confidence: float = 0.0
    part: int = 1
    part_count: int = 1
    purpose: str = ""
    executive_summary: str = ""
    core_thesis: str = ""
    argument_map: str = ""
    key_concepts: list[str] = field(default_factory=list)
    evidence_examples: list[Claim] = field(default_factory=list)
    important_quotations: list[Claim] = field(default_factory=list)
    relation_to_previous: str = ""
    relation_to_following: str = ""
    cross_chapter_connections: list[Claim] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    review_questions: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    source_fingerprint: str = ""
    generator_fingerprint: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass
class Rollup:
    node_id: str
    chapters: list[int]
    summary: str = ""
    themes: list[Claim] = field(default_factory=list)
    concepts: list[Claim] = field(default_factory=list)
    arguments: list[Claim] = field(default_factory=list)
    tensions: list[Claim] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    input_fingerprint: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass
class BookSynthesis:
    title: str
    overview: str = ""
    core_thesis: str = ""
    argument_chain: str = ""
    themes: list[Claim] = field(default_factory=list)
    concepts: list[Claim] = field(default_factory=list)
    arguments: list[Claim] = field(default_factory=list)
    terminology: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    chapter_summaries: dict[str, str] = field(default_factory=dict)
    evidence: list[EvidenceRef] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


def to_dict(value: Any) -> Any:
    """Convert nested artifact dataclasses into JSON-compatible values."""
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_dict(item) for item in value]
    if isinstance(value, list):
        return [to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: to_dict(item) for key, item in value.items()}
    return value


def _evidence(value: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=str(value.get("evidence_id", "")),
        chunk_id=str(value.get("chunk_id", "")),
        source_path=str(value.get("source_path", "")),
        chapter=int(value.get("chapter", 0)),
        start_line=int(value.get("start_line", 0)),
        end_line=int(value.get("end_line", 0)),
        quote=str(value.get("quote", "")),
        page=int(value["page"]) if value.get("page") is not None else None,
        spine=str(value["spine"]) if value.get("spine") is not None else None,
        locator=dict(value.get("locator") or {}),
    )


def _claim(value: Any) -> Claim:
    if isinstance(value, str):
        return Claim(text=value)
    value = value or {}
    return Claim(
        text=str(value.get("text", "")),
        evidence_ids=tuple(str(item) for item in value.get("evidence_ids", ())),
        kind=str(value.get("kind", "interpretation")),
        confidence=str(value.get("confidence", "medium")),
        qualification=str(value.get("qualification", "")),
    )


def chapter_from_dict(value: dict[str, Any]) -> ChapterInterpretation:
    return ChapterInterpretation(
        chapter=int(value.get("chapter", 0)),
        title=str(value.get("title", "")),
        source_unit_id=str(value.get("source_unit_id", "")),
        source_title=str(value.get("source_title", "")),
        source_type=str(value.get("source_type", value.get("kind", ""))),
        breadcrumb=[str(item) for item in value.get("breadcrumb", ())],
        physical_page_start=int(value["physical_page_start"]) if value.get("physical_page_start") is not None else None,
        physical_page_end=int(value["physical_page_end"]) if value.get("physical_page_end") is not None else None,
        spine=str(value.get("spine", "")),
        spine_index=int(value["spine_index"]) if value.get("spine_index") is not None else None,
        href=str(value.get("href", "")),
        fragment=str(value.get("fragment", "")),
        structure_confidence=float(value.get("structure_confidence", value.get("confidence", 0.0)) or 0.0),
        part=int(value.get("part", 1) or 1),
        part_count=int(value.get("part_count", 1) or 1),
        purpose=str(value.get("purpose", "")),
        executive_summary=str(value.get("executive_summary", "")),
        core_thesis=str(value.get("core_thesis", "")),
        argument_map=str(value.get("argument_map", "")),
        key_concepts=[str(item) for item in value.get("key_concepts", ())],
        evidence_examples=[_claim(item) for item in value.get("evidence_examples", ())],
        important_quotations=[_claim(item) for item in value.get("important_quotations", ())],
        relation_to_previous=str(value.get("relation_to_previous", "")),
        relation_to_following=str(value.get("relation_to_following", "")),
        cross_chapter_connections=[_claim(item) for item in value.get("cross_chapter_connections", ())],
        open_questions=[str(item) for item in value.get("open_questions", ())],
        review_questions=[str(item) for item in value.get("review_questions", ())],
        claims=[_claim(item) for item in value.get("claims", ())],
        evidence=[_evidence(item) for item in value.get("evidence", ())],
        source_fingerprint=str(value.get("source_fingerprint", "")),
        generator_fingerprint=str(value.get("generator_fingerprint", "")),
        schema_version=int(value.get("schema_version", SCHEMA_VERSION)),
    )


def rollup_from_dict(value: dict[str, Any]) -> Rollup:
    return Rollup(
        node_id=str(value.get("node_id", "")),
        chapters=[int(item) for item in value.get("chapters", ())],
        summary=str(value.get("summary", "")),
        themes=[_claim(item) for item in value.get("themes", ())],
        concepts=[_claim(item) for item in value.get("concepts", ())],
        arguments=[_claim(item) for item in value.get("arguments", ())],
        tensions=[_claim(item) for item in value.get("tensions", ())],
        evidence=[_evidence(item) for item in value.get("evidence", ())],
        input_fingerprint=str(value.get("input_fingerprint", "")),
        schema_version=int(value.get("schema_version", SCHEMA_VERSION)),
    )


def _chapter_summaries(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(summary) for key, summary in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for index, item in enumerate(value, 1):
            if isinstance(item, dict):
                key = item.get("chapter", item.get("chapter_number", index))
                summary = item.get("summary", item.get("text", item.get("content", "")))
                result[str(key)] = str(summary)
            else:
                result[str(index)] = str(item)
        return result
    return {}


def book_from_dict(value: dict[str, Any]) -> BookSynthesis:
    return BookSynthesis(
        title=str(value.get("title", "Untitled book")),
        overview=str(value.get("overview", "")),
        core_thesis=str(value.get("core_thesis", "")),
        argument_chain=str(value.get("argument_chain", "")),
        themes=[_claim(item) for item in value.get("themes", ())],
        concepts=[_claim(item) for item in value.get("concepts", ())],
        arguments=[_claim(item) for item in value.get("arguments", ())],
        terminology=[str(item) for item in value.get("terminology", ())],
        unresolved_questions=[str(item) for item in value.get("unresolved_questions", ())],
        chapter_summaries=_chapter_summaries(value.get("chapter_summaries", {})),
        evidence=[_evidence(item) for item in value.get("evidence", ())],
        schema_version=int(value.get("schema_version", SCHEMA_VERSION)),
    )
