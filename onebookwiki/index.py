"""JSON-backed chunk index; embeddings are optional."""
from __future__ import annotations

import json
from pathlib import Path
from .chunking import Chunk
from .manifest import Manifest

class LocalIndex:
    def __init__(self, root: Path):
        self.root = root
        self.manifest = Manifest.load(root)

    @property
    def path(self) -> Path:
        return self.root / ".onebookwiki" / "chunks.json"

    def save(self) -> None:
        self.manifest.save(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(list(self.manifest.chunks.values()), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def load(self) -> list[dict]:
        if self.path.is_file():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return list(self.manifest.chunks.values())

    def update(self, chapter_path: Path, chapter: int, chunks: list[Chunk]) -> tuple[set[str], set[str]]:
        serialized = [chunk.__dict__ for chunk in chunks]
        relative_path = chapter_path if not chapter_path.is_absolute() else chapter_path.relative_to(self.root)
        actual_path = self.root / relative_path
        removed, added = self.manifest.update_chapter(relative_path, chapter, serialized, self.manifest.chapter_hash(actual_path))
        self.save()
        return removed, added

    def rebuild(self, chapter_files: list[tuple[Path, int]], chunker) -> dict:
        for path, chapter in chapter_files:
            chunks = chunker(path, chapter)
            relative_path = path if not path.is_absolute() else path.relative_to(self.root)
            self.manifest.update_chapter(relative_path, chapter, [chunk.__dict__ for chunk in chunks], self.manifest.chapter_hash(self.root / relative_path))
        self.save()
        return self.manifest.chapters
