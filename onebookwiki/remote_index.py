"""Persistent per-book cloud-vector index with optional FAISS acceleration."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .chunking import Chunk
from .manifest import Manifest
from typing import Protocol


class Embedder(Protocol):
    provider: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...

    def identity(self) -> dict: ...


class CloudVectorIndex:
    def __init__(self, root: Path, embedder: Embedder):
        self.root = root
        self.embedder = embedder
        self.manifest = Manifest.load(root)
        self.vector_path = root / ".onebookwiki" / "vectors.json"

    def _load_vectors(self) -> dict[str, list[float]]:
        if not self.vector_path.is_file():
            return {}
        try:
            value = json.loads(self.vector_path.read_text(encoding="utf-8"))
            return {str(k): list(v) for k, v in value.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_vectors(self, vectors: dict[str, list[float]]) -> None:
        self.vector_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_path.write_text(json.dumps(vectors, ensure_ascii=False), encoding="utf-8")

    def update_chapter(
        self,
        path: Path,
        chapter: int,
        chunks: list[Chunk],
        *,
        chunking: dict | None = None,
        index_identity: dict | None = None,
    ) -> tuple[int, int]:
        relative = path if not path.is_absolute() else path.relative_to(self.root)
        source_key = relative.as_posix()
        old = self.manifest.chapters.get(source_key, {})
        old_ids = set(old.get("chunk_ids", []))
        vectors = self._load_vectors()
        for chunk_id in old_ids:
            vectors.pop(chunk_id, None)
        new_vectors = self.embedder.embed([chunk.text for chunk in chunks])
        serialized = []
        for chunk, vector in zip(chunks, new_vectors):
            item = chunk.__dict__.copy()
            item["embedding_dimension"] = len(vector)
            serialized.append(item)
            vectors[chunk.chunk_id] = vector
        identity = self.embedder.identity()
        resolved_identity = index_identity or {
            "backend": str(identity.get("provider", getattr(self.embedder, "provider", "vector"))),
            "model": str(identity.get("model", "configured")),
        }
        self.manifest.update_chapter(
            relative,
            chapter,
            serialized,
            self.manifest.chapter_hash(self.root / relative),
            chunking=chunking,
            index_identity=resolved_identity,
        )
        self.manifest.embedding_backend = str(resolved_identity["backend"])
        self.manifest.embedding_model = str(resolved_identity["model"])
        self._save_vectors(vectors)
        self.manifest.save(self.root)
        return len(old_ids), len(chunks)

    def search(self, query: str, top_k: int = 8, chapter: int | None = None) -> list[tuple[float, dict]]:
        vectors = self._load_vectors()
        query_vector = self.embedder.embed_one(query)
        query_norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
        results = []
        for item in self.manifest.chunks.values():
            if chapter is not None and item.get("chapter") != chapter:
                continue
            vector = vectors.get(item["chunk_id"])
            if not vector or len(vector) != len(query_vector):
                continue
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            score = sum(a * b for a, b in zip(query_vector, vector)) / (query_norm * norm)
            results.append((score, item))
        results.sort(key=lambda pair: (-pair[0], pair[1].get("chapter", 0), pair[1].get("start_line", 0)))
        return results[:top_k]
