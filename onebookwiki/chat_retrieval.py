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
from .retrieval import HybridRetriever, LexicalRetriever, Result, rerank_results, search_project, vector_results
from .index import LocalIndex
from .wiki_retrieval import assemble_wiki_first_context, build_wiki_first_prompt, render_context_blocks, search_artifacts, search_wiki


class ChatRetrievalError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_EVIDENCE_ID_RE = re.compile(r"\bC(\d+)E(\d+)\b")


def strict_raw_results(root: Path, question: str, *, top_k: int = 8, backend: str = "bge-m3", retrieval: str | None = None, chapters: Sequence[int] = ()) -> list[Result]:
    """Search indexed raw evidence without a lexical-only fallback.

    This is the Agent-only raw evidence entry point. Callers must use ``vector``
    or ``hybrid``. The optional chapter filter keeps the selected source-unit
    route bounded without weakening the raw provenance boundary.
    """
    mode = retrieval or "vector"
    if mode not in {"vector", "hybrid"}:
        raise ChatRetrievalError("retrieval_unavailable", "Agent raw evidence retrieval requires vector or hybrid mode.")
    chapter_filter = sorted({int(item) for item in chapters})
    try:
        embedder = build_embedder(backend)
        vector_index = CloudVectorIndex(root, embedder)
        if mode == "vector":
            if chapter_filter:
                found = []
                for chapter in chapter_filter:
                    found.extend(vector_results(vector_index.search(question, top_k, chapter)))
                return rerank_results(question, found, "local")[:top_k]
            return vector_results(vector_index.search(question, top_k))
        lexical = LexicalRetriever(LocalIndex(root).load())
        hybrid = HybridRetriever(lexical, lambda query, limit, chapter: vector_results(vector_index.search(query, limit, chapter)))
        if chapter_filter:
            found = []
            per_chapter = max(1, (top_k + len(chapter_filter) - 1) // len(chapter_filter))
            for chapter in chapter_filter:
                found.extend(hybrid.search(question, per_chapter, chapter))
            return rerank_results(question, found, "local")[:top_k]
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


def _wiki_evidence_records(root: Path, chunks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve CnEn citations embedded in selected wiki/artifact blocks through the rendered evidence index.

    Wiki pages and generation artifacts already carry evidence IDs that were
    validated once at generation time (see rendering.py / generation.py).
    Re-validating them here against the manifest and raw source lets chat
    answers grounded in curated Overview/Thesis prose (which rarely has a
    matching raw-chunk hit) still cite real, checkable evidence instead of
    being refused outright.
    """
    evidence_path = root / "wiki" / "evidence.json"
    try:
        rendered = json.loads(evidence_path.read_text(encoding="utf-8")).get("evidence", {})
    except (OSError, ValueError, AttributeError):
        return []
    ids: set[str] = set()
    for chunk in chunks:
        ids.update(extract_citation_ids(str(chunk.get("text", ""))))
    valid: list[dict[str, Any]] = []
    for evidence_id in sorted(ids):
        value = rendered.get(evidence_id)
        if not isinstance(value, dict):
            continue
        ref = EvidenceRef(
            evidence_id=str(value.get("evidence_id", evidence_id)),
            chunk_id=str(value.get("chunk_id", "")),
            source_path=str(value.get("source_path", "")),
            chapter=int(value.get("chapter", 0) or 0),
            start_line=int(value.get("start_line", 0) or 0),
            end_line=int(value.get("end_line", 0) or 0),
            quote=str(value.get("quote", "")),
            locator=dict(value.get("locator") or {}),
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
            "text": "",
        })
    return valid


def _legacy_raw_results(
    root: Path,
    question: str,
    *,
    backend: str,
    retrieval: str | None,
) -> list[Result]:
    """Preserve fixed-RAG CLI/test behavior while Agent retrieval stays strict.

    The legacy path still supports explicitly selected lexical retrieval. It is
    intentionally private so new web chat code cannot accidentally choose it.
    """
    mode = retrieval or ("vector" if backend in {"bge-m3", "modelscope"} else "lexical")
    if mode == "lexical":
        return search_project(root, question, top_k=8)
    return strict_raw_results(root, question, backend=backend, retrieval=mode)


def retrieve_chat_context(
    root: Path,
    question: str,
    history: Sequence[dict[str, str]] = (),
    *,
    backend: str = "bge-m3",
    retrieval: str | None = None,
    max_tokens: int = 8000,
) -> dict[str, Any]:
    """Build a legacy fixed-RAG prompt; new web chat uses ``AgenticChatRunner``."""
    wiki = search_wiki(root, question, 3)
    artifacts = search_artifacts(root, question, 2)
    raw = rerank_results(
        question,
        _legacy_raw_results(root, question, backend=backend, retrieval=retrieval),
        "local",
    )
    context, selected = assemble_wiki_first_context(wiki, artifacts, raw, max_tokens, 3, 2, 3)
    raw_items = [item for item in selected if item.get("source_kind") == "raw"]
    raw_selected = _evidence_records(root, raw_items)
    # Stamp each raw block with its resolved, validated evidence_id so the
    # rendered context (and therefore the model) can see and cite the exact
    # CnEn tag for that block, instead of being told to cite an ID it was
    # never shown.
    by_chunk_id = {item["chunk_id"]: item["evidence_id"] for item in raw_selected}
    for item in raw_items:
        evidence_id = by_chunk_id.get(str(item.get("chunk_id", "")))
        if evidence_id:
            item["evidence_id"] = evidence_id
    wiki_selected = _wiki_evidence_records(root, [item for item in selected if item.get("source_kind") in {"wiki", "artifact"}])
    combined_evidence = raw_selected + wiki_selected
    if not combined_evidence:
        raise ChatRetrievalError("raw_evidence_missing", "没有找到可验证的证据，无法回答该问题。")
    allowed = {item["evidence_id"] for item in combined_evidence}
    context = render_context_blocks(selected)
    history_text = "\n".join(
        f"{item.get('role', 'user').upper()}: {str(item.get('content', '')).strip()}"
        for item in history[-20:]
        if str(item.get("content", "")).strip()
    )
    prompt = build_wiki_first_prompt(question, context, sorted(allowed))
    if history_text:
        prompt = f"CONVERSATION HISTORY (untrusted context, not evidence):\n{history_text}\n\n{prompt}"
    return {
        "prompt": prompt,
        "context": context,
        "selected": selected,
        "raw_evidence": combined_evidence,
        "allowed_evidence_ids": sorted(allowed),
    }


def resolve_validated_evidence(root: Path, results: Sequence[Result | dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    """Resolve strict raw results to deduplicated, source-validated CnEn records."""
    chunks = [dict(item.chunk) if isinstance(item, Result) else dict(item) for item in results]
    records = _evidence_records(root, chunks)
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        unique.setdefault(str(record["evidence_id"]), record)
    return list(unique.values())[: max(1, min(limit, 6))]


def validate_final_answer(answer: str, allowed_evidence_ids: set[str]) -> list[str]:
    """Require a valid allowed citation in each substantive answer paragraph."""
    citations = validate_answer(answer, allowed_evidence_ids)
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", answer) if item.strip()]
    for paragraph in paragraphs:
        prose = re.sub(_EVIDENCE_ID_RE, "", paragraph).strip(" []()，,。;；")
        if len(prose) < 20:
            continue
        paragraph_ids = extract_citation_ids(paragraph)
        if not paragraph_ids:
            raise ChatRetrievalError("answer_uncited_claim", "模型回答包含没有 evidence 引用的实质段落。")
        unknown = paragraph_ids - allowed_evidence_ids
        if unknown:
            raise ChatRetrievalError("answer_unknown_citation", f"模型引用了本轮不可用的 evidence：{', '.join(sorted(unknown))}")
    return citations


def validate_answer(answer: str, allowed_evidence_ids: set[str]) -> list[str]:
    citations = sorted(extract_citation_ids(answer))
    if not citations:
        raise ChatRetrievalError("answer_missing_citation", "模型回答没有包含可验证 evidence 引用。")
    unknown = [item for item in citations if item not in allowed_evidence_ids]
    if unknown:
        raise ChatRetrievalError("answer_unknown_citation", f"模型引用了本轮不可用的 evidence：{', '.join(unknown)}")
    return citations
