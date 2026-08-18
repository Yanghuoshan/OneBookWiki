"""Persistent derived-state manifest and content hashes."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from .markdown import sha256_text

SCHEMA_VERSION = 4

@dataclass
class Manifest:
    schema_version: int = SCHEMA_VERSION
    embedding_backend: str = "lexical"
    embedding_model: str = "none"
    chapters: dict[str, dict] | None = None
    chunks: dict[str, dict] | None = None
    source: dict | None = None
    artifacts: dict[str, dict] | None = None

    def __post_init__(self):
        self.chapters = self.chapters or {}
        self.chunks = self.chunks or {}
        self.artifacts = self.artifacts or {}
        if self.schema_version < SCHEMA_VERSION:
            self.schema_version = SCHEMA_VERSION

    @classmethod
    def load(cls, root: Path) -> "Manifest":
        path = root / ".onebookwiki" / "manifest.json"
        if not path.is_file():
            return cls()
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, root: Path) -> None:
        target = root / ".onebookwiki" / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def chapter_hash(self, path: Path) -> str:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def update_chapter(
        self,
        path: Path,
        chapter: int,
        chunks: list[dict],
        content_hash: str | None = None,
        *,
        chunking: dict | None = None,
        index_identity: dict | None = None,
    ) -> tuple[set[str], set[str]]:
        key = path.as_posix()
        old_ids = {item.get("chunk_id") for item in self.chunks.values() if item.get("source_path") == key}
        new_ids = {item["chunk_id"] for item in chunks}
        for chunk_id in old_ids - new_ids:
            self.chunks.pop(chunk_id, None)
        for item in chunks:
            self.chunks[item["chunk_id"]] = item
        digest = content_hash if content_hash is not None else self.chapter_hash(path)
        record = {"chapter": chapter, "content_hash": digest, "chunk_ids": sorted(new_ids)}
        if chunking is not None:
            record["chunking"] = chunking
        if index_identity is not None:
            record["index_identity"] = index_identity
        self.chapters[key] = record
        return old_ids - new_ids, new_ids - old_ids

    def prune_missing_chapters(self, root: Path) -> dict[str, set[str]]:
        """Remove manifest chapters and chunks whose raw files no longer exist."""
        removed_paths: set[str] = set()
        removed_chunk_ids: set[str] = set()
        for key, chapter in list(self.chapters.items()):
            if (root / key).is_file():
                continue
            removed_paths.add(key)
            self.chapters.pop(key, None)
            for chunk_id in chapter.get("chunk_ids", []):
                removed_chunk_ids.add(chunk_id)
                self.chunks.pop(chunk_id, None)
        return {"paths": removed_paths, "chunk_ids": removed_chunk_ids}
