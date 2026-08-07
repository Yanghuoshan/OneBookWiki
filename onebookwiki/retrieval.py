"""Lexical, vector, and hybrid retrieval with deterministic reranking."""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .index import LocalIndex

WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)
CJK_RE = re.compile(r"[㐀-鿿]")


@dataclass(frozen=True)
class Result:
    score: float
    chunk: dict


def chunk_key(chunk: dict) -> str:
    """Return a stable identity for deduplicating retrieval candidates."""
    return str(
        chunk.get("content_hash")
        or chunk.get("chunk_id")
        or f"{chunk.get('source_path', '')}:{chunk.get('start_line', '')}:{chunk.get('end_line', '')}"
    )


class LexicalRetriever:
    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.document_frequency: dict[str, int] = {}
        for chunk in chunks:
            for term in set(self._terms(chunk.get("text", ""))):
                self.document_frequency[term] = self.document_frequency.get(term, 0) + 1

    @staticmethod
    def _terms(text: str) -> list[str]:
        terms = [term.lower() for term in WORD_RE.findall(text)]
        for run in re.findall(r"[㐀-鿿]+", text):
            terms.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
        return terms

    def search(self, query: str, top_k: int = 8, chapter: int | None = None) -> list[Result]:
        query_terms = self._terms(query)
        if not query_terms or top_k <= 0:
            return []
        total = max(1, len(self.chunks))
        scored: list[Result] = []
        for chunk in self.chunks:
            if chapter is not None and chunk.get("chapter") != chapter:
                continue
            terms = self._terms(chunk.get("text", ""))
            counts = {term: terms.count(term) for term in set(terms)}
            score = 0.0
            for term in query_terms:
                if term in counts:
                    idf = math.log((total + 1) / (self.document_frequency.get(term, 0) + 1)) + 1
                    score += (1 + math.log(counts[term])) * idf
            if score:
                scored.append(Result(score, chunk))
        scored.sort(key=lambda item: (-item.score, item.chunk.get("chapter", 0), item.chunk.get("start_line", 0), chunk_key(item.chunk)))
        return scored[:top_k]


def vector_results(items: Sequence[tuple[float, dict]]) -> list[Result]:
    """Adapt the cloud index's tuple result format to ``Result``."""
    return [Result(float(score), chunk) for score, chunk in items]


def reciprocal_rank_fusion(
    lexical: Sequence[Result],
    vector: Sequence[Result],
    top_k: int = 8,
    *,
    lexical_weight: float = 1.0,
    vector_weight: float = 1.0,
    rrf_k: int = 60,
) -> list[Result]:
    """Merge two ranked lists without allowing either list to duplicate context."""
    if top_k <= 0:
        return []
    scores: dict[str, float] = {}
    chunks: dict[str, dict] = {}
    ranks: dict[str, tuple[int, int]] = {}
    for source, items, weight, offset in (
        ("lexical", lexical, lexical_weight, 0),
        ("vector", vector, vector_weight, 1),
    ):
        if weight <= 0:
            continue
        for rank, result in enumerate(items, 1):
            key = chunk_key(result.chunk)
            scores[key] = scores.get(key, 0.0) + weight / (max(1, rrf_k) + rank)
            chunks.setdefault(key, result.chunk)
            current = list(ranks.get(key, (10**9, 10**9)))
            current[offset] = min(current[offset], rank)
            ranks[key] = tuple(current)
    ordered = sorted(
        scores,
        key=lambda key: (-scores[key], ranks[key], chunks[key].get("chapter", 0), chunks[key].get("start_line", 0), key),
    )
    return [Result(scores[key], chunks[key]) for key in ordered[:top_k]]


class HybridRetriever:
    """Fuse a local lexical retriever with a callable vector search adapter."""

    def __init__(self, lexical: LexicalRetriever, vector_search: Callable[[str, int, int | None], Sequence[Result]]):
        self.lexical = lexical
        self.vector_search = vector_search

    def search(
        self,
        query: str,
        top_k: int = 8,
        chapter: int | None = None,
        *,
        lexical_top_k: int | None = None,
        vector_top_k: int | None = None,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
        rrf_k: int = 60,
    ) -> list[Result]:
        lexical_limit = lexical_top_k if lexical_top_k is not None else max(top_k * 2, top_k)
        vector_limit = vector_top_k if vector_top_k is not None else max(top_k * 2, top_k)
        lexical_results = self.lexical.search(query, lexical_limit, chapter)
        vector_results_found = list(self.vector_search(query, vector_limit, chapter))
        return reciprocal_rank_fusion(
            lexical_results,
            vector_results_found,
            top_k,
            lexical_weight=lexical_weight,
            vector_weight=vector_weight,
            rrf_k=rrf_k,
        )


class LocalReranker:
    """Cheap deterministic query-aware reranking that needs no extra model."""

    def rerank(self, query: str, results: Sequence[Result]) -> list[Result]:
        if not results:
            return []
        query_terms = set(LexicalRetriever._terms(query))
        normalized_query = " ".join(query.lower().split())
        ranked: list[tuple[float, int, Result]] = []
        for original_rank, result in enumerate(results):
            text = result.chunk.get("text", "")
            terms = set(LexicalRetriever._terms(text))
            overlap = len(query_terms & terms) / max(1, len(query_terms))
            phrase_bonus = 1.0 if normalized_query and normalized_query in " ".join(text.lower().split()) else 0.0
            score = result.score + overlap + phrase_bonus
            ranked.append((score, phrase_bonus, overlap, original_rank, result))
        ranked.sort(key=lambda item: (-item[1], -item[2], -item[0], item[3], item[4].chunk.get("chapter", 0), item[4].chunk.get("start_line", 0), chunk_key(item[4].chunk)))
        return [Result(score, result.chunk) for score, _, _, _, result in ranked]


def rerank_results(query: str, results: Sequence[Result], mode: str = "local") -> list[Result]:
    if mode == "none":
        return list(results)
    if mode == "local":
        return LocalReranker().rerank(query, results)
    raise ValueError(f"Unknown reranker: {mode}")


def search_project(root: Path, query: str, top_k: int = 8, chapter: int | None = None) -> list[Result]:
    return LexicalRetriever(LocalIndex(root).load()).search(query, top_k, chapter)
