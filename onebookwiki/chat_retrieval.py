"""Strict, evidence-first retrieval service for persisted Web chat turns."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

from .citations import validate_evidence
from .models import EvidenceRef
from .providers import ProviderUnavailable, build_embedder
from .remote_index import CloudVectorIndex
from .retrieval import HybridRetriever, LexicalRetriever, Result, rerank_results, vector_results
from .index import LocalIndex
from .wiki_retrieval import assemble_wiki_first_context, build_wiki_first_prompt, search_artifacts, search_wiki
from .cli.chat_book import raw_results


class ChatRetrievalError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_EVIDENCE_ID_RE = re.compile(r"\bC(\d+)E(\d+)\b")


def _strict_raw_results(root: Path, question: str, *, top_k: int = 8, backend: str = "bge-m3", retrieval: str | None = None) -> list[Result]:
    """Retrieve raw evidence without the CLI's permissive lexical fallback."""
    mode = retrieval or ("vector" if backend in {"bge-m3", "modelscope"} else "lexical")
    if mode == "lexical":
        return raw_results(root, type("Args", (), {
            "retrieval": "lexical", "backend": backend, "question": question,
            "top_k": top_k, "chapter": None, "strict_hybrid": True,
        })())
    try:
        embedder = build_embedder(backend)
        vector_index = CloudVectorIndex(root, embedder)
        if mode == "vector":
            return vector_results(vector_index.search(question, top_k))
        lexical = LexicalRetriever(LocalIndex(root).load())
        hybrid = HybridRetriever(lexical, lambda query, limit, chapter: vector_results(vector_index.search(query, limit, chapter)))
        return hybrid.search(question, top_k)
    except ProviderUnavailable as exc:
        raise ChatRetrievalError("retrieval_unavailable", str(exc)) from exc
    except ValueError as exc:
        raise ChatRetrievalError("embedding_identity_mismatch", str(exc)) from exc


def _raw_valid(root: Path, chunk: dict[str, Any]) -> bool:
    """Validate the selected chunk against both the raw file and its manifest.

    Chunk hashes cover the normalized chunk text, while manifest chapter hashes
    cover the raw file bytes. Overlap chunks may contain text from the previous
    range, so the chunk text is checked against the complete file rather than
    only the non-overlap line range.
    """
    source_path = str(chunk.get("source_path", ""))
    raw_root = (root / "raw" / "chapters").resolve()
    source = (root / source_path).resolve()
    if not source.is_relative_to(raw_root) or not source.is_file():
        return False
    try:
        raw_bytes = source.read_bytes()
        lines = raw_bytes.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False

    manifest = LocalIndex(root).manifest
    chapter_record = manifest.chapters.get(source_path)
    if not isinstance(chapter_record, dict):
        return False
    expected_file_hash = str(chapter_record.get("content_hash", ""))
    if not expected_file_hash or hashlib.sha256(raw_bytes).hexdigest() != expected_file_hash:
        return False

    start = int(chunk.get("start_line", 0) or 0)
    end = int(chunk.get("end_line", 0) or 0)
    if start < 1 or end < start or end > len(lines):
        return False
    expected = str(chunk.get("text", ""))
    content_hash = str(chunk.get("content_hash", ""))
    if not expected or not content_hash:
        return False
    if hashlib.sha256(expected.encode("utf-8")).hexdigest() != content_hash:
        return False
    normalized_source = " ".join("\n".join(lines).split())
    if " ".join(expected.split()) not in normalized_source:
        return False
    return True


def _evidence_records(root: Path, chunks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve selected raw chunks through the rendered public evidence index."""
    evidence_path = root / "wiki" / "evidence.json"
    try:
        rendered = json.loads(evidence_path.read_text(encoding="utf-8")).get("evidence", {})
    except (OSError, ValueError, AttributeError):
        return []
    valid: list[dict[str, Any]] = []
    for chunk in chunks:
        if not _raw_valid(root, chunk):
            continue
        chapter = int(chunk.get("chapter", 0) or 0)
        chunk_id = str(chunk.get("chunk_id", ""))
        matches = [
            value for value in rendered.values()
            if isinstance(value, dict) and str(value.get("chunk_id", "")) == chunk_id
            and int(value.get("chapter", 0) or 0) == chapter
        ]
        if not matches:
            continue
        value = matches[0]
        ref = EvidenceRef(
            evidence_id=str(value.get("evidence_id", "")), chunk_id=chunk_id,
            source_path=str(value.get("source_path", "")), chapter=chapter,
            start_line=int(value.get("start_line", 0) or 0), end_line=int(value.get("end_line", 0) or 0),
            quote=str(value.get("quote", "")), locator=dict(value.get("locator") or {}),
        )
        if not ref.evidence_id or validate_evidence(root, [ref]):
            continue
        valid.append({
            "evidence_id": ref.evidence_id,
            "chunk_id": ref.chunk_id,
            "source_path": ref.source_path,
            "chapter": ref.chapter,
            "start_line": ref.start_line,
            "end_line": ref.end_line,
            "quote": ref.quote,
            "text": str(chunk.get("text", "")),
        })
    return valid


def extract_citation_ids(text: str) -> set[str]:
    return {f"C{chapter}E{index}" for chapter, index in _EVIDENCE_ID_RE.findall(text or "")}


def retrieve_chat_context(
    root: Path,
    question: str,
    history: Sequence[dict[str, str]] = (),
    *,
    backend: str = "bge-m3",
    retrieval: str | None = None,
    max_tokens: int = 8000,
) -> dict[str, Any]:
    """Build a strict wiki-first prompt; raise before generation if raw evidence is invalid."""
    wiki = search_wiki(root, question, 3)
    artifacts = search_artifacts(root, question, 2)
    raw = rerank_results(question, _strict_raw_results(root, question, backend=backend, retrieval=retrieval), "local")
    context, selected = assemble_wiki_first_context(wiki, artifacts, raw, max_tokens, 3, 2, 3)
    raw_selected = _evidence_records(root, [item for item in selected if item.get("source_kind") == "raw"])
    if not raw_selected:
        raise ChatRetrievalError("raw_evidence_missing", "没有找到可验证的原始证据，无法回答该问题。")
    allowed = {item["evidence_id"] for item in raw_selected}
    history_text = "\n".join(
        f"{item.get('role', 'user').upper()}: {str(item.get('content', '')).strip()}"
        for item in history[-20:]
        if str(item.get("content", "")).strip()
    )
    prompt = build_wiki_first_prompt(question, context)
    if history_text:
        prompt = f"CONVERSATION HISTORY (untrusted context, not evidence):\n{history_text}\n\n{prompt}"
    return {
        "prompt": prompt,
        "context": context,
        "selected": selected,
        "raw_evidence": raw_selected,
        "allowed_evidence_ids": sorted(allowed),
    }


def validate_answer(answer: str, allowed_evidence_ids: set[str]) -> list[str]:
    citations = sorted(extract_citation_ids(answer))
    if not citations:
        raise ChatRetrievalError("answer_missing_citation", "模型回答没有包含可验证 evidence 引用。")
    unknown = [item for item in citations if item not in allowed_evidence_ids]
    if unknown:
        raise ChatRetrievalError("answer_unknown_citation", f"模型引用了本轮不可用的 evidence：{', '.join(unknown)}")
    return citations
