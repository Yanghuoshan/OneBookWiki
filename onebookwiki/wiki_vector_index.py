"""Persistent semantic indexes for the rendered wiki navigation layers.

Raw source evidence remains in ``CloudVectorIndex``.  This module deliberately
keeps book navigation, chapter interpretation, and semantic entity vectors in a
separate manifest so their coverage and embedding identity can be audited
without weakening raw-evidence provenance checks.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from .chunking import chunk_text, chunking_profile, count_tokens
from .markdown import sha256_text


INDEX_VERSION = 1
BOOK_NAVIGATION = "book_navigation"
CHAPTER_INTERPRETATION = "chapter_interpretation"
SEMANTIC_ENTITY = "semantic_entity"
LAYER_NAMES = (BOOK_NAVIGATION, CHAPTER_INTERPRETATION, SEMANTIC_ENTITY)


class VectorEmbedder(Protocol):
    provider: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...

    def identity(self) -> dict[str, Any]: ...


class WikiVectorIndexError(RuntimeError):
    """Raised when a required rendered-wiki vector layer is unavailable."""


def _identity(embedder: VectorEmbedder) -> dict[str, str]:
    value = embedder.identity()
    return {
        "backend": str(value.get("provider", getattr(embedder, "provider", "vector"))),
        "model": str(value.get("model", "configured")),
        "base_url": str(value.get("base_url", "")),
    }


def _safe_file(root: Path, relative: str, parent: Path) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(parent.resolve()) or not path.is_file():
        raise WikiVectorIndexError(f"invalid rendered wiki path: {relative}")
    return path


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WikiVectorIndexError(f"{label} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise WikiVectorIndexError(f"{label} must be a JSON object")
    return value


def _citation_ids(text: str) -> set[str]:
    import re

    return set(re.findall(r"\bC\d+E\d+\b", text))


def _flatten_outline(nodes: Iterable[Any], ancestors: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        node = dict(raw)
        node_id = str(node.get("id", ""))
        if not node_id:
            continue
        node["outline_ancestors"] = list(ancestors)
        result.append(node)
        result.extend(_flatten_outline(node.get("children") or [], ancestors + (node_id,)))
    return result


def _shorten(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if count_tokens(text) <= limit:
        return text.strip()
    low, high, best = 1, len(text), 0
    while low <= high:
        middle = (low + high) // 2
        if count_tokens(text[:middle]) <= limit:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return text[:best].rstrip()


@dataclass(frozen=True)
class LayerBuildResult:
    layer: str
    documents: int
    chunks: int
    changed_documents: int
    reused_documents: int


class WikiVectorIndex:
    """Read and incrementally build typed semantic indexes for rendered wiki files."""

    def __init__(self, root: Path, embedder: VectorEmbedder):
        self.root = root.resolve()
        self.embedder = embedder
        self.directory = self.root / ".onebookwiki" / "retrieval"
        self.manifest_path = self.directory / "wiki-vectors.json"
        self.vectors_path = self.directory / "wiki-vectors-data.json"

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {"version": INDEX_VERSION, "identity": {}, "layers": {}}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WikiVectorIndexError("wiki vector manifest is invalid") from exc
        if not isinstance(value, dict) or value.get("version") != INDEX_VERSION:
            raise WikiVectorIndexError("wiki vector manifest version is unsupported")
        value.setdefault("layers", {})
        return value

    def _load_vectors(self) -> dict[str, list[float]]:
        if not self.vectors_path.is_file():
            return {}
        try:
            raw = json.loads(self.vectors_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            return {str(key): [float(item) for item in value] for key, value in raw.items()}
        except (OSError, ValueError, TypeError) as exc:
            raise WikiVectorIndexError("wiki vector data is invalid") from exc

    def _save(self, manifest: dict[str, Any], vectors: dict[str, list[float]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.vectors_path.write_text(json.dumps(vectors, ensure_ascii=False) + "\n", encoding="utf-8")

    def _page_records(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
        wiki = self.root / "wiki"
        structure = _load_json(wiki / "structure.json", "wiki/structure.json")
        evidence = _load_json(wiki / "evidence.json", "wiki/evidence.json")
        pages = {
            str(item.get("id")): dict(item)
            for item in structure.get("pages", [])
            if isinstance(item, dict) and str(item.get("id", ""))
        }
        if not pages:
            raise WikiVectorIndexError("wiki structure contains no pages")
        return structure, pages, dict(evidence.get("evidence") or {})

    def _document_chunks(
        self,
        *,
        layer: str,
        document_id: str,
        path: str,
        text: str,
        metadata: dict[str, Any],
        target_tokens: int,
        overlap_tokens: int,
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        profile = chunking_profile(
            text,
            target_tokens=target_tokens,
            overlap_tokens=overlap_tokens,
            max_tokens=max_tokens,
        )
        chapter = int(metadata.get("chapter", 0) or 0)
        chunks = chunk_text(
            text,
            path,
            chapter,
            target_tokens=target_tokens,
            overlap_tokens=overlap_tokens,
            max_tokens=max_tokens,
        )
        output: list[dict[str, Any]] = []
        for ordinal, chunk in enumerate(chunks):
            raw = dict(chunk.__dict__)
            chunk_id = f"{layer}:{document_id}:{ordinal}:{raw['content_hash'][:12]}"
            raw.update(metadata)
            raw.update({
                "chunk_id": chunk_id,
                "layer": layer,
                "document_id": document_id,
                "source_path": path,
                "chunking": profile,
            })
            output.append(raw)
        return output

    def _documents(self) -> dict[str, list[dict[str, Any]]]:
        """Load all indexable rendered files into independently typed documents."""
        structure, pages, evidence = self._page_records()
        wiki_root = self.root / "wiki"
        outline = _flatten_outline(structure.get("sourceOutline") or [])
        outline_by_page: dict[str, list[str]] = {}
        for node in outline:
            for page_id in node.get("pageIds") or []:
                if isinstance(page_id, str):
                    outline_by_page.setdefault(page_id, []).append(str(node["id"]))

        source = _load_json(self.root / ".onebookwiki" / "source.json", ".onebookwiki/source.json")
        unit_by_chapter = {
            int(item.get("chapter", 0) or 0): str(item.get("source_unit_id", ""))
            for item in list(dict(source.get("source_structure") or {}).get("units") or [])
            if isinstance(item, dict) and int(item.get("chapter", 0) or 0)
        }
        evidence_to_units: dict[str, str] = {}
        evidence_to_chapters: dict[str, int] = {}
        for evidence_id, record in evidence.items():
            if not isinstance(record, dict):
                continue
            chapter = int(record.get("chapter", 0) or 0)
            evidence_to_units[str(evidence_id)] = str(
                record.get("source_unit_id") or record.get("sourceUnitId") or unit_by_chapter.get(chapter, "")
            )
            evidence_to_chapters[str(evidence_id)] = chapter

        layers: dict[str, list[dict[str, Any]]] = {name: [] for name in LAYER_NAMES}
        for page_id, page in sorted(pages.items()):
            kind = str(page.get("kind", ""))
            relative = str(page.get("path", ""))
            if kind not in {"book", "index", "reading_unit", "theme", "concept", "argument"}:
                continue
            path = _safe_file(self.root, f"wiki/{relative}", wiki_root)
            text = path.read_text(encoding="utf-8")
            metadata = {
                "page_id": page_id,
                "page_kind": kind,
                "title": str(page.get("title", "")),
                "chapter": int(page.get("chapter", 0) or 0),
                "source_unit_id": str(page.get("sourceUnitId", "")),
                "part": int(page.get("part", 0) or 0),
                "part_count": int(page.get("partCount", 0) or 0),
                "breadcrumb": [str(item) for item in page.get("breadcrumb", [])],
                "raw_sources": [str(item) for item in page.get("rawSources", [])],
                "related_pages": [str(item) for item in page.get("relatedPages", [])],
                "outline_node_ids": outline_by_page.get(page_id, []),
            }
            document_id = f"page:{page_id}"
            relative_path = f"wiki/{relative}"
            if kind in {"book", "index"}:
                layers[BOOK_NAVIGATION].append({
                    "document_id": document_id,
                    "path": relative_path,
                    "text": text,
                    "metadata": metadata,
                    "profile": (180, 20, 240),
                })
            elif kind == "reading_unit":
                layers[CHAPTER_INTERPRETATION].append({
                    "document_id": document_id,
                    "path": relative_path,
                    "text": text,
                    "metadata": metadata,
                    "profile": (180, 20, 240),
                })
            else:
                # Rendered prose uses display labels, so page metadata is the
                # canonical citation-route source. The text scan remains for
                # compatibility with older rendered books that retained CnEn.
                citation_ids = {
                    *[str(item) for item in page.get("evidenceIds", []) if str(item)],
                    *_citation_ids(text),
                }
                unit_ids = sorted({evidence_to_units[item] for item in citation_ids if evidence_to_units.get(item)})
                chapters = sorted({evidence_to_chapters[item] for item in citation_ids if evidence_to_chapters.get(item)})
                metadata["evidence_ids"] = sorted(citation_ids)
                metadata["source_unit_ids"] = unit_ids
                metadata["chapters"] = chapters
                layers[SEMANTIC_ENTITY].append({
                    "document_id": document_id,
                    "path": relative_path,
                    "text": text,
                    "metadata": metadata,
                    "profile": (180, 20, 240),
                })

        # Chapter interpretation coverage is a hard build invariant: every
        # rendered wiki/chapters Markdown file must have exactly one typed
        # document, and no reading-unit metadata may point somewhere else.
        chapter_root = wiki_root / "chapters"
        rendered_chapter_paths = {
            path.relative_to(wiki_root).as_posix()
            for path in chapter_root.glob("*.md")
            if path.is_file()
        }
        indexed_chapter_paths = {
            str(item["path"])[len("wiki/"):]
            for item in layers[CHAPTER_INTERPRETATION]
        }
        if rendered_chapter_paths != indexed_chapter_paths:
            missing = sorted(rendered_chapter_paths - indexed_chapter_paths)
            extra = sorted(indexed_chapter_paths - rendered_chapter_paths)
            raise WikiVectorIndexError(
                "chapter interpretation metadata does not exactly cover wiki/chapters/*.md "
                f"(missing={missing}, extra={extra})"
            )

        # Outline cards are navigation-only synthetic documents; no raw excerpts are included.
        for node in outline:
            card = "\n".join([
                str(node.get("title", "")),
                " > ".join(str(item) for item in node.get("breadcrumb", [])),
                f"kind: {node.get('kind', '')}",
                f"pages: {', '.join(str(item) for item in node.get('pageIds', []))}",
            ]).strip()
            if not card:
                continue
            layers[BOOK_NAVIGATION].append({
                "document_id": f"outline:{node['id']}",
                "path": "wiki/structure.json",
                "text": card,
                "metadata": {
                    "outline_node_id": str(node["id"]),
                    "page_kind": "outline",
                    "title": str(node.get("title", "")),
                    "breadcrumb": [str(item) for item in node.get("breadcrumb", [])],
                    "related_pages": [str(item) for item in node.get("pageIds", [])],
                    "outline_ancestors": [str(item) for item in node.get("outline_ancestors", [])],
                },
                "profile": (160, 0, 200),
            })
        return layers

    def build(self) -> list[LayerBuildResult]:
        """Incrementally build all rendered-wiki vector layers."""
        identity = _identity(self.embedder)
        manifest = self._load_manifest()
        vectors = self._load_vectors()
        same_identity = manifest.get("identity") == identity
        old_layers = manifest.get("layers", {}) if same_identity else {}
        fresh_layers: dict[str, Any] = {}
        live_vector_ids: set[str] = set()
        results: list[LayerBuildResult] = []

        for layer, documents in self._documents().items():
            old = dict(old_layers.get(layer) or {})
            old_documents = dict(old.get("documents") or {})
            old_chunks = {str(item.get("chunk_id")): item for item in old.get("chunks", []) if isinstance(item, dict)}
            layer_chunks: list[dict[str, Any]] = []
            document_records: dict[str, Any] = {}
            changed = reused = 0
            for document in documents:
                # Routing metadata is part of a semantic document's identity. A
                # page can keep the same prose while its chapter/source-unit
                # relationship changes after a render, and stale route metadata
                # would otherwise be reused with the old vectors.
                digest_input = json.dumps(
                    {
                        "text": document["text"],
                        "path": document["path"],
                        "metadata": document["metadata"],
                        "profile": document["profile"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                digest = sha256_text(digest_input)
                document_id = str(document["document_id"])
                previous = old_documents.get(document_id)
                if previous and previous.get("content_hash") == digest:
                    ids = [str(item) for item in previous.get("chunk_ids", [])]
                    reused_chunks = [old_chunks[item] for item in ids if item in old_chunks and item in vectors]
                    if len(reused_chunks) == len(ids) and reused_chunks:
                        layer_chunks.extend(reused_chunks)
                        document_records[document_id] = previous
                        live_vector_ids.update(ids)
                        reused += 1
                        continue
                target, overlap, maximum = document["profile"]
                chunks = self._document_chunks(
                    layer=layer,
                    document_id=document_id,
                    path=document["path"],
                    text=document["text"],
                    metadata=document["metadata"],
                    target_tokens=target,
                    overlap_tokens=overlap,
                    max_tokens=maximum,
                )
                embeddings = self.embedder.embed([str(item["text"]) for item in chunks])
                if len(embeddings) != len(chunks):
                    raise WikiVectorIndexError(f"embedding provider returned incomplete {layer} vectors")
                for chunk, vector in zip(chunks, embeddings):
                    if not vector:
                        raise WikiVectorIndexError(f"embedding provider returned empty {layer} vector")
                    vectors[str(chunk["chunk_id"])] = [float(value) for value in vector]
                    live_vector_ids.add(str(chunk["chunk_id"]))
                layer_chunks.extend(chunks)
                document_records[document_id] = {
                    "path": document["path"],
                    "content_hash": digest,
                    "chunk_ids": [str(item["chunk_id"]) for item in chunks],
                    "chunking": chunks[0]["chunking"] if chunks else {},
                }
                changed += 1
            if layer == CHAPTER_INTERPRETATION and not documents:
                raise WikiVectorIndexError("no wiki/chapters/*.md documents were available for the chapter layer")
            fresh_layers[layer] = {"documents": document_records, "chunks": layer_chunks}
            results.append(LayerBuildResult(layer, len(document_records), len(layer_chunks), changed, reused))

        vectors = {key: value for key, value in vectors.items() if key in live_vector_ids}
        manifest = {"version": INDEX_VERSION, "identity": identity, "layers": fresh_layers}
        self._save(manifest, vectors)
        return results

    def _validate(self, layer: str) -> tuple[dict[str, Any], dict[str, list[float]]]:
        if layer not in LAYER_NAMES:
            raise WikiVectorIndexError(f"unknown wiki vector layer: {layer}")
        manifest = self._load_manifest()
        identity = _identity(self.embedder)
        if manifest.get("identity") != identity:
            raise WikiVectorIndexError("wiki vector index identity does not match the configured embedding model")
        value = manifest.get("layers", {}).get(layer)
        if not isinstance(value, dict) or not value.get("chunks"):
            raise WikiVectorIndexError(f"wiki vector layer is missing or empty: {layer}")
        vectors = self._load_vectors()
        for chunk in value["chunks"]:
            if str(chunk.get("chunk_id", "")) not in vectors:
                raise WikiVectorIndexError(f"wiki vector data is incomplete for layer: {layer}")
        return value, vectors

    def search(
        self,
        layer: str,
        query: str,
        *,
        limit: int = 6,
        source_unit_ids: Sequence[str] = (),
        chapters: Sequence[int] = (),
        kinds: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        if not query.strip() or limit <= 0:
            return []
        value, vectors = self._validate(layer)
        query_vector = self.embedder.embed_one(query)
        if not query_vector:
            raise WikiVectorIndexError("embedding provider returned an empty query vector")
        query_norm = math.sqrt(sum(item * item for item in query_vector)) or 1.0
        allowed_units = {str(item) for item in source_unit_ids if str(item)}
        allowed_chapters = {int(item) for item in chapters}
        allowed_kinds = {str(item) for item in kinds if str(item)}
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in value["chunks"]:
            if allowed_units:
                chunk_units = {str(chunk.get("source_unit_id", "")), *[str(item) for item in chunk.get("source_unit_ids", [])]}
                if not chunk_units & allowed_units:
                    continue
            if allowed_chapters:
                chunk_chapters = {int(chunk.get("chapter", 0) or 0), *[int(item) for item in chunk.get("chapters", [])]}
                if not chunk_chapters & allowed_chapters:
                    continue
            if allowed_kinds and str(chunk.get("page_kind", "")) not in allowed_kinds:
                continue
            vector = vectors.get(str(chunk["chunk_id"]))
            if not vector or len(vector) != len(query_vector):
                continue
            norm = math.sqrt(sum(item * item for item in vector)) or 1.0
            score = sum(left * right for left, right in zip(query_vector, vector)) / (query_norm * norm)
            item = dict(chunk)
            item["score"] = score
            item["text"] = _shorten(str(item.get("text", "")), 180)
            scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("document_id", "")), str(pair[1].get("chunk_id", ""))))
        return [item for _, item in scored[: min(limit, 12)]]

    def health(self) -> dict[str, Any]:
        """Return index coverage, including strict chapter-page coverage diagnostics."""
        manifest = self._load_manifest()
        layers = manifest.get("layers", {})
        chapter_layer = dict(layers.get(CHAPTER_INTERPRETATION) or {})
        expected = set()
        rendered_files: set[str] = set()
        try:
            _, pages, _ = self._page_records()
            expected = {f"page:{page_id}" for page_id, page in pages.items() if page.get("kind") == "reading_unit"}
            rendered_files = {
                f"wiki/{path.relative_to(self.root / 'wiki').as_posix()}"
                for path in (self.root / "wiki" / "chapters").glob("*.md")
                if path.is_file()
            }
        except WikiVectorIndexError:
            pass
        indexed_documents = dict(chapter_layer.get("documents") or {})
        indexed = set(indexed_documents)
        indexed_files = {str(item.get("path", "")) for item in indexed_documents.values() if isinstance(item, dict)}
        return {
            "identity": manifest.get("identity", {}),
            "layers": {
                name: {
                    "documents": len(dict((layers.get(name) or {}).get("documents") or {})),
                    "chunks": len(list((layers.get(name) or {}).get("chunks") or [])),
                }
                for name in LAYER_NAMES
            },
            "chapter_documents_expected": len(expected),
            "chapter_documents_indexed": len(indexed),
            "chapter_documents_missing": sorted(expected - indexed),
            "chapter_documents_extra": sorted(indexed - expected),
            "chapter_files_expected": len(rendered_files),
            "chapter_files_indexed": len(indexed_files),
            "chapter_files_missing": sorted(rendered_files - indexed_files),
            "chapter_files_extra": sorted(indexed_files - rendered_files),
            "healthy": bool(expected) and expected == indexed and rendered_files == indexed_files,
        }


def build_wiki_vector_indexes(root: Path, embedder: VectorEmbedder) -> list[LayerBuildResult]:
    """Build all rendered wiki semantic layers after a successful render."""
    return WikiVectorIndex(root, embedder).build()
