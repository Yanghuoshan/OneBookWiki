"""Persistent derived-state manifest and content hashes."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from .markdown import sha256_text

SCHEMA_VERSION = 5
CONTRACT_VERSION = "grounded-v2"

_REQUIRED_V2_FIELDS = (
    "contract_version",
    "schema_integrity",
    "book_revision_id",
    "book_revision_hash",
    "evidence_revisions",
    "publication_health",
)

@dataclass
class Manifest:
    schema_version: int = SCHEMA_VERSION
    contract_version: str = CONTRACT_VERSION
    schema_integrity: str = "grounded-v2"
    book_revision_id: str = ""
    book_revision_hash: str = ""
    evidence_revisions: dict[str, dict] | None = None
    publication_health: dict[str, object] | None = None
    embedding_backend: str = "lexical"
    embedding_model: str = "none"
    chapters: dict[str, dict] | None = None
    chunks: dict[str, dict] | None = None
    source: dict | None = None
    artifacts: dict[str, dict] | None = None

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema version: {self.schema_version}")
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported manifest contract: {self.contract_version}")
        if self.schema_integrity != CONTRACT_VERSION:
            raise ValueError(f"manifest schema integrity mismatch: {self.schema_integrity}")
        self.evidence_revisions = self.evidence_revisions or {}
        self.publication_health = self.publication_health or {"status": "unpublished", "reason": "not_generated"}
        self.chapters = self.chapters or {}
        self.chunks = self.chunks or {}
        self.artifacts = self.artifacts or {}

    @classmethod
    def load(cls, root: Path) -> "Manifest":
        path = root / ".onebookwiki" / "manifest.json"
        if not path.is_file():
            return cls()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid Grounded v2 manifest: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Grounded v2 manifest must be an object: {path}")
        if value.get("schema_version") != SCHEMA_VERSION or any(field not in value for field in _REQUIRED_V2_FIELDS):
            raise ValueError(f"unsupported manifest contract; expected Grounded v2 schema {SCHEMA_VERSION}")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError(f"invalid Grounded v2 manifest fields: {path}") from exc

    def save(self, root: Path) -> None:
        target = root / ".onebookwiki" / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def pin_registry(self, snapshot) -> None:
        """Pin the manifest to one immutable Evidence Registry snapshot."""
        self.book_revision_id = str(snapshot.book_revision_id)
        self.book_revision_hash = str(snapshot.book_revision_hash)
        self.evidence_revisions = {
            revision_id: revision.to_dict()
            for revision_id, revision in sorted(snapshot.evidence.items())
        }
        for source_path, registered in snapshot.chapters.items():
            record = self.chapters.get(source_path)
            if record is None:
                raise ValueError(f"indexed chapter is missing from Grounded v2 manifest: {source_path}")
            record["source_file_hash"] = registered.source_file_hash
            record["body_hash"] = registered.body_hash
            record["evidence_revision_ids"] = list(registered.evidence_revision_ids)
            for chunk_id in record.get("chunk_ids", []):
                chunk = self.chunks.get(chunk_id)
                if chunk is not None:
                    chunk["book_revision_id"] = self.book_revision_id
        self.publication_health = {"status": "staging", "reason": "knowledge_not_published"}

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
