"""Bounded, read-only navigation over a rendered OneBookWiki book."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .chunking import count_tokens
from .wiki_vector_index import (
    BOOK_NAVIGATION,
    CHAPTER_INTERPRETATION,
    SEMANTIC_ENTITY,
    WikiVectorIndex,
    WikiVectorIndexError,
)


class BookCatalogError(RuntimeError):
    """Raised for malformed book navigation metadata or unavailable indexes."""


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BookCatalogError(f"{label} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise BookCatalogError(f"{label} must be a JSON object")
    return value


def _trim(text: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    if count_tokens(text) <= maximum:
        return text.strip()
    low, high, best = 1, len(text), 0
    while low <= high:
        middle = (low + high) // 2
        if count_tokens(text[:middle]) <= maximum:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return text[:best].rstrip()


def _outline(nodes: list[Any], parents: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("id", ""))
        if not node_id or node_id in result:
            continue
        value = dict(raw)
        value["parent_ids"] = list(parents)
        children = value.get("children") or []
        result[node_id] = value
        nested = _outline(children, parents + (node_id,))
        if result.keys() & nested.keys():
            raise BookCatalogError("source outline contains duplicate node IDs")
        result.update(nested)
    return result


class BookCatalog:
    """Validated catalog that returns compact navigation material, never evidence."""

    def __init__(self, root: Path, vector_index: WikiVectorIndex):
        self.root = root.resolve()
        self.vector_index = vector_index
        self.wiki_root = self.root / "wiki"
        source = _load(self.root / ".onebookwiki" / "source.json", ".onebookwiki/source.json")
        structure = _load(self.wiki_root / "structure.json", "wiki/structure.json")
        evidence = _load(self.wiki_root / "evidence.json", "wiki/evidence.json")
        self.title = str(structure.get("title") or source.get("title") or "")
        self.format = str(source.get("format", ""))
        self.description = str(structure.get("description", ""))
        self.pages = {
            str(item.get("id")): dict(item)
            for item in structure.get("pages", [])
            if isinstance(item, dict) and str(item.get("id", ""))
        }
        if not self.pages:
            raise BookCatalogError("wiki structure contains no pages")
        self.outline = _outline(list(structure.get("sourceOutline") or []))
        source_structure = dict(source.get("source_structure") or {})
        units = list(source_structure.get("units") or [])
        self.units: dict[str, list[dict[str, Any]]] = {}
        for item in units:
            if not isinstance(item, dict):
                continue
            source_unit_id = str(item.get("source_unit_id", ""))
            if source_unit_id:
                self.units.setdefault(source_unit_id, []).append(dict(item))
        for source_unit_id in self.units:
            self.units[source_unit_id].sort(key=lambda item: int(item.get("chapter", 0) or 0))
        self.evidence = {
            str(key): dict(value)
            for key, value in dict(evidence.get("evidence") or {}).items()
            if isinstance(value, dict)
        }
        self.pages_by_unit: dict[str, list[str]] = {}
        self.pages_by_chapter: dict[int, list[str]] = {}
        for page_id, page in self.pages.items():
            if page.get("kind") != "reading_unit":
                continue
            source_unit_id = str(page.get("sourceUnitId", ""))
            chapter = int(page.get("chapter", 0) or 0)
            if source_unit_id:
                self.pages_by_unit.setdefault(source_unit_id, []).append(page_id)
            if chapter:
                self.pages_by_chapter.setdefault(chapter, []).append(page_id)

    def _page(self, page_id: str) -> dict[str, Any]:
        page = self.pages.get(page_id)
        if page is None:
            raise BookCatalogError(f"unknown page ID: {page_id}")
        relative = str(page.get("path", ""))
        path = (self.wiki_root / relative).resolve()
        if not path.is_relative_to(self.wiki_root.resolve()) or not path.is_file():
            raise BookCatalogError(f"invalid rendered page path: {relative}")
        return page

    @staticmethod
    def _page_card(page_id: str, page: dict[str, Any]) -> dict[str, Any]:
        return {
            "page_id": page_id,
            "kind": str(page.get("kind", "")),
            "title": str(page.get("title", "")),
            "chapter": int(page.get("chapter", 0) or 0),
            "source_unit_id": str(page.get("sourceUnitId", "")),
            "part": int(page.get("part", 0) or 0),
            "part_count": int(page.get("partCount", 0) or 0),
            "breadcrumb": [str(item) for item in page.get("breadcrumb", [])],
            "related_pages": [str(item) for item in page.get("relatedPages", [])],
        }

    def book_overview(self) -> dict[str, Any]:
        roots = [item for item in self.outline.values() if not item.get("parent_ids")]
        return {
            "title": self.title,
            "format": self.format,
            "page_count": len(self.pages),
            "source_unit_count": len(self.units),
            "overview": _trim(self.description, 160),
            "root_outline": [
                {
                    "outline_node_id": str(item["id"]),
                    "title": str(item.get("title", "")),
                    "kind": str(item.get("kind", "")),
                    "breadcrumb": [str(value) for value in item.get("breadcrumb", [])],
                    "page_ids": [str(value) for value in item.get("pageIds", [])][:6],
                }
                for item in roots[:8]
            ],
        }

    def _search(self, layer: str, query: str, **filters: Any) -> list[dict[str, Any]]:
        try:
            return self.vector_index.search(layer, query, limit=min(6, int(filters.pop("limit", 6))), **filters)
        except WikiVectorIndexError as exc:
            raise BookCatalogError(str(exc)) from exc

    def search_book_navigation(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        return [self._navigation_result(item) for item in self._search(BOOK_NAVIGATION, query, limit=limit)]

    def search_chapter_interpretations(
        self,
        query: str,
        *,
        source_unit_ids: Sequence[str] = (),
        chapters: Sequence[int] = (),
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        results = self._search(
            CHAPTER_INTERPRETATION,
            query,
            source_unit_ids=source_unit_ids,
            chapters=chapters,
            limit=limit,
        )
        output: list[dict[str, Any]] = []
        for item in results:
            if str(item.get("page_kind", "")) != "reading_unit":
                raise BookCatalogError("chapter interpretation index contains a non-chapter page")
            output.append(self._navigation_result(item))
        return output

    def search_entities(self, query: str, *, kinds: Sequence[str] = (), limit: int = 6) -> list[dict[str, Any]]:
        allowed = tuple(kinds) or ("theme", "concept", "argument")
        return [self._navigation_result(item) for item in self._search(SEMANTIC_ENTITY, query, kinds=allowed, limit=limit)]

    def _navigation_result(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "page_id": str(item.get("page_id", "")),
            "outline_node_id": str(item.get("outline_node_id", "")),
            "kind": str(item.get("page_kind", "")),
            "title": str(item.get("title", "")),
            "chapter": int(item.get("chapter", 0) or 0),
            "chapters": [int(value) for value in item.get("chapters", [])],
            "source_unit_id": str(item.get("source_unit_id", "")),
            "source_unit_ids": [str(value) for value in item.get("source_unit_ids", [])],
            "part": int(item.get("part", 0) or 0),
            "part_count": int(item.get("part_count", 0) or 0),
            "breadcrumb": [str(value) for value in item.get("breadcrumb", [])],
            "related_pages": [str(value) for value in item.get("related_pages", [])],
            "outline_node_ids": [str(value) for value in item.get("outline_node_ids", [])],
            "evidence_ids": [str(value) for value in item.get("evidence_ids", [])],
            "score": round(float(item.get("score", 0.0)), 6),
            "excerpt": _trim(str(item.get("text", "")), 180),
        }

    def browse_outline(self, node_id: str, limit: int = 6) -> dict[str, Any]:
        node = self.outline.get(node_id)
        if node is None:
            raise BookCatalogError(f"unknown outline node ID: {node_id}")
        children = [child for child in node.get("children", []) if isinstance(child, dict)][: min(6, max(1, limit))]
        page_ids = [str(item) for item in node.get("pageIds", [])][: min(6, max(1, limit))]
        return {
            "outline_node_id": node_id,
            "title": str(node.get("title", "")),
            "kind": str(node.get("kind", "")),
            "breadcrumb": [str(item) for item in node.get("breadcrumb", [])],
            "parent_ids": [str(item) for item in node.get("parent_ids", [])],
            "children": [
                {"outline_node_id": str(item.get("id", "")), "title": str(item.get("title", "")), "kind": str(item.get("kind", ""))}
                for item in children
            ],
            "pages": [self._page_card(page_id, self._page(page_id)) for page_id in page_ids if page_id in self.pages],
        }

    def open_page(self, page_id: str, *, max_tokens: int = 180) -> dict[str, Any]:
        page = self._page(page_id)
        path = (self.wiki_root / str(page["path"])).resolve()
        return {
            **self._page_card(page_id, page),
            "excerpt": _trim(path.read_text(encoding="utf-8"), min(240, max(1, max_tokens))),
        }

    def open_source_unit(self, source_unit_id: str) -> dict[str, Any]:
        parts = self.units.get(source_unit_id)
        if not parts:
            raise BookCatalogError(f"unknown source unit ID: {source_unit_id}")
        raw_root = (self.root / "raw" / "chapters").resolve()
        for part in parts:
            raw_path = str(part.get("raw_path", ""))
            resolved = (self.root / raw_path).resolve()
            if not resolved.is_relative_to(raw_root) or not resolved.is_file():
                raise BookCatalogError(f"invalid source unit raw path: {raw_path}")
        first = parts[0]
        chapters = [int(item.get("chapter", 0) or 0) for item in parts if int(item.get("chapter", 0) or 0)]
        return {
            "source_unit_id": source_unit_id,
            "title": str(first.get("source_title") or first.get("title", "")),
            "source_title": str(first.get("source_title") or first.get("title", "")),
            "kind": str(first.get("kind", "")),
            "chapter": chapters[0] if chapters else 0,
            "part_count": max(int(item.get("part_count", 0) or 0) for item in parts),
            "breadcrumb": [str(item) for item in first.get("breadcrumb", [])],
            "locators": [dict(item.get("locator") or {}) for item in parts],
            "page_ids": list(self.pages_by_unit.get(source_unit_id, [])),
            "chapters": chapters,
        }

    def resolve_direct_chapter(self, chapter: int) -> dict[str, Any] | None:
        pages = self.pages_by_chapter.get(int(chapter), [])
        if not pages:
            return None
        return self._page_card(pages[0], self._page(pages[0]))
