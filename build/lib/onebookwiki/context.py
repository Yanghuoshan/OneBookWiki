"""Bounded context assembly for low-token book queries."""
from __future__ import annotations
from .retrieval import Result
from .chunking import count_tokens


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if count_tokens(text) <= max_tokens:
        return text
    low, high, best = 0, len(text), ""
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip()
        if candidate and count_tokens(candidate) <= max_tokens:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best or text[:1]


def assemble_context(results: list[Result], max_tokens: int = 8000, max_per_chapter: int = 3) -> tuple[str, list[dict]]:
    if max_tokens <= 0:
        return "", []
    selected: list[dict] = []
    seen_hashes: set[str] = set()
    per_chapter: dict[int, int] = {}
    used = 0
    for result in results:
        chunk = result.chunk
        digest = chunk.get("content_hash") or chunk.get("chunk_id")
        chapter = int(chunk.get("chapter", 0))
        if digest in seen_hashes or per_chapter.get(chapter, 0) >= max_per_chapter:
            continue
        text = chunk.get("text", "")
        tokens = int(chunk.get("token_count") or count_tokens(text))
        if used and used + tokens > max_tokens:
            continue
        if not used and tokens > max_tokens:
            text = _truncate_to_tokens(text, max_tokens)
            tokens = count_tokens(text)
        item = dict(chunk)
        item["text"] = text
        item["score"] = result.score
        selected.append(item)
        seen_hashes.add(digest)
        per_chapter[chapter] = per_chapter.get(chapter, 0) + 1
        used += tokens
    parts = []
    for item in selected:
        location = f"lines {item.get('start_line')}-{item.get('end_line')}"
        parts.append(f"## Chapter {item.get('chapter')} ({item.get('source_path')}, {location})\n\n{item['text']}")
    return "\n\n---\n\n".join(parts), selected
