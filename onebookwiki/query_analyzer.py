"""Deterministic, orthogonal analysis of Grounded v2 questions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping

CONTRACT_VERSION = "grounded-v2"
QUERY_ANALYSIS_VERSION = 1

DOCUMENTARY_SCOPES = frozenset({"span", "section", "reading_unit", "rollup", "book"})
TARGET_TYPES = frozenset({"book", "chapter", "entity", "statement", "evidence"})
TASKS = frozenset({"summary", "definition", "explain", "compare", "causal", "quote", "fact_detail", "explore"})
SEMANTIC_KINDS = frozenset({"factual", "definitional", "causal", "comparative", "interpretive", "normative", "attribution", "quotation"})
ABSTRACTIONS = frozenset({"concrete_detail", "local_generalization", "cross_scope_synthesis"})
SUBJECT_BREADTHS = frozenset({"single_subject", "relation", "theme_multi_subject"})

_ANALYSIS_KEYS = {
    "contract_version", "analysis_version", "question", "normalized_question",
    "documentary_scope", "target_type", "task", "semantic_kind", "abstraction",
    "subject_breadth", "subjects", "chapter", "exact_wording", "terms", "query_hash",
}
_CHAPTER_PATTERNS = (
    re.compile(r"第\s*(\d{1,4})\s*[章节]"),
    re.compile(r"\bchapter\s*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bchap\.?\s*(\d{1,4})\b", re.IGNORECASE),
)


class QueryAnalysisError(ValueError):
    """A question or serialized analysis violates the Grounded v2 contract."""


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    return " ".join(normalized.replace("\r\n", "\n").replace("\r", "\n").split())


def _contains(value: str, terms: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(term.casefold() in lowered for term in terms)


def _chapter(question: str) -> int | None:
    for pattern in _CHAPTER_PATTERNS:
        match = pattern.search(question)
        if match:
            chapter = int(match.group(1))
            return chapter if chapter > 0 else None
    return None


def _quoted_terms(question: str) -> tuple[str, ...]:
    values: list[str] = []
    for left, body, right in re.findall(r"([\"'“‘《])([^\"'”’》]{1,120})([\"'”’》])", question):
        if (left, right) not in {("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"), ("《", "》")}:
            continue
        term = _normalize(body)
        if term and term not in values:
            values.append(term)
    return tuple(values)


def _subjects(question: str, terms: tuple[str, ...], breadth: str) -> tuple[str, ...]:
    if terms:
        return terms[:4]
    value = re.sub(r"[?？!！。,.，:：;；]", " ", question)
    value = re.sub(
        r"第\s*\d+\s*[章节]|chapter\s*\d+|请|概述|总结|解释|说明|比较|对比|为什么|为何|如何|"
        r"是什么|什么是|原文|引用|quote|summari[sz]e|explain|compare|define|definition",
        " ", value, flags=re.IGNORECASE,
    )
    parts = [item for item in re.split(r"\s+(?:和|与|及|and|versus|vs\.?|之间)\s+|\s+", value) if item]
    limit = 4 if breadth == "theme_multi_subject" else 2 if breadth == "relation" else 1
    return tuple(parts[:limit])


def _hash_payload(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class QueryAnalysis:
    question: str
    normalized_question: str
    documentary_scope: str
    target_type: str
    task: str
    semantic_kind: str
    abstraction: str
    subject_breadth: str
    subjects: tuple[str, ...]
    chapter: int | None
    exact_wording: bool
    terms: tuple[str, ...]
    query_hash: str
    contract_version: str = CONTRACT_VERSION
    analysis_version: int = QUERY_ANALYSIS_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "analysis_version": self.analysis_version,
            "question": self.question,
            "normalized_question": self.normalized_question,
            "documentary_scope": self.documentary_scope,
            "target_type": self.target_type,
            "task": self.task,
            "semantic_kind": self.semantic_kind,
            "abstraction": self.abstraction,
            "subject_breadth": self.subject_breadth,
            "subjects": list(self.subjects),
            "chapter": self.chapter,
            "exact_wording": self.exact_wording,
            "terms": list(self.terms),
            "query_hash": self.query_hash,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QueryAnalysis":
        value = dict(raw)
        if set(value) != _ANALYSIS_KEYS:
            raise QueryAnalysisError("query analysis has invalid keys")
        if value.get("contract_version") != CONTRACT_VERSION or value.get("analysis_version") != QUERY_ANALYSIS_VERSION:
            raise QueryAnalysisError("query analysis contract mismatch")
        question = value.get("question")
        normalized = value.get("normalized_question")
        if not isinstance(question, str) or not question.strip() or normalized != _normalize(question):
            raise QueryAnalysisError("query analysis question is invalid")
        controlled = (
            ("documentary_scope", DOCUMENTARY_SCOPES), ("target_type", TARGET_TYPES),
            ("task", TASKS), ("semantic_kind", SEMANTIC_KINDS),
            ("abstraction", ABSTRACTIONS), ("subject_breadth", SUBJECT_BREADTHS),
        )
        for key, allowed in controlled:
            if value.get(key) not in allowed:
                raise QueryAnalysisError(f"query analysis {key} is not controlled")
        chapter = value.get("chapter")
        if chapter is not None and (isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1):
            raise QueryAnalysisError("query analysis chapter is invalid")
        if not isinstance(value.get("exact_wording"), bool):
            raise QueryAnalysisError("query analysis exact_wording is invalid")
        subjects = value.get("subjects")
        terms = value.get("terms")
        if not isinstance(subjects, list) or not isinstance(terms, list):
            raise QueryAnalysisError("query analysis terms must be arrays")
        if any(not isinstance(item, str) or not item.strip() for item in subjects + terms):
            raise QueryAnalysisError("query analysis contains an invalid term")
        identity = {key: value[key] for key in _ANALYSIS_KEYS - {"query_hash"}}
        if value.get("query_hash") != _hash_payload(identity):
            raise QueryAnalysisError("query analysis hash mismatch")
        return cls(
            question=question, normalized_question=normalized,
            documentary_scope=value["documentary_scope"], target_type=value["target_type"],
            task=value["task"], semantic_kind=value["semantic_kind"], abstraction=value["abstraction"],
            subject_breadth=value["subject_breadth"], subjects=tuple(subjects), chapter=chapter,
            exact_wording=value["exact_wording"], terms=tuple(terms), query_hash=value["query_hash"],
        )


def analyze_query(question: str) -> QueryAnalysis:
    """Classify a question using deterministic local rules only."""
    original = unicodedata.normalize("NFC", str(question or "")).strip()
    normalized = _normalize(original)
    if not normalized:
        raise QueryAnalysisError("question must not be empty")
    if len(normalized) > 4000:
        raise QueryAnalysisError("question exceeds the analysis limit")

    chapter = _chapter(normalized)
    terms = _quoted_terms(normalized)
    exact = bool(terms) or _contains(normalized, ("原文", "逐字", "精确措辞", "怎么说", "引用", "quote", "exact wording", "verbatim"))

    if exact:
        task, semantic_kind = "quote", "quotation"
    elif _contains(normalized, ("比较", "对比", "区别", "异同", "关系", "compare", "versus", " vs ")):
        task, semantic_kind = "compare", "comparative"
    elif _contains(normalized, ("为什么", "为何", "原因", "导致", "影响", "因果", "cause", "why")):
        task, semantic_kind = "causal", "causal"
    elif _contains(normalized, ("是什么", "什么是", "定义", "含义", "define", "definition", "meaning")):
        task, semantic_kind = "definition", "definitional"
    elif _contains(normalized, ("总结", "概述", "概要", "主旨", "核心论点", "summary", "summarize", "overview", "thesis")):
        task, semantic_kind = "summary", "interpretive"
    elif _contains(normalized, ("谁说", "作者认为", "归因", "according to", "attribute")):
        task, semantic_kind = "fact_detail", "attribution"
    elif _contains(normalized, ("解释", "说明", "如何", "怎么", "explain", "how")):
        task, semantic_kind = "explain", "interpretive"
    elif re.search(r"\b\d{3,4}\b|\d{1,2}[年月日号]", normalized):
        task, semantic_kind = "fact_detail", "factual"
    else:
        task, semantic_kind = "explore", "factual"

    if _contains(normalized, ("全书", "整本书", "本书", "全局", "book-wide", "whole book")):
        scope, target = "book", "book"
    elif exact:
        scope, target = "span", "evidence"
    elif chapter is not None:
        scope, target = "reading_unit", "chapter"
    elif task == "summary" and _contains(normalized, ("章节", "各章", "跨章", "多章", "rollup")):
        scope, target = "rollup", "chapter"
    else:
        scope = "section" if task in {"definition", "fact_detail"} else "reading_unit"
        target = "entity" if task in {"definition", "compare", "causal", "explain", "explore"} else "statement"

    if scope in {"book", "rollup"} or task == "compare":
        abstraction = "cross_scope_synthesis"
    elif task in {"summary", "definition", "explain", "causal"}:
        abstraction = "local_generalization"
    else:
        abstraction = "concrete_detail"

    if task == "compare" or _contains(normalized, ("之间", "between", " vs ", "versus")):
        breadth = "relation"
    elif scope in {"book", "rollup"} or _contains(normalized, ("主题", "各章", "多个", "themes")):
        breadth = "theme_multi_subject"
    else:
        breadth = "single_subject"
    subjects = _subjects(normalized, terms, breadth)
    identity = {
        "contract_version": CONTRACT_VERSION, "analysis_version": QUERY_ANALYSIS_VERSION,
        "question": original, "normalized_question": normalized, "documentary_scope": scope,
        "target_type": target, "task": task, "semantic_kind": semantic_kind,
        "abstraction": abstraction, "subject_breadth": breadth, "subjects": list(subjects),
        "chapter": chapter, "exact_wording": exact, "terms": list(terms),
    }
    return QueryAnalysis(
        question=original, normalized_question=normalized, documentary_scope=scope,
        target_type=target, task=task, semantic_kind=semantic_kind, abstraction=abstraction,
        subject_breadth=breadth, subjects=subjects, chapter=chapter, exact_wording=exact,
        terms=terms, query_hash=_hash_payload(identity),
    )
