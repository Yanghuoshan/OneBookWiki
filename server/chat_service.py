"""Business operations for public opaque-id chat conversations."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from server.config import ChatSettings, generation_snapshot, verify_generation_snapshot
from server.database import (
    append_chat_turn,
    create_chat_conversation,
    get_active_healthy_book_revision,
    get_book,
    get_chat_conversation,
)
from onebookwiki.providers import GenerationConfig

CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{32,48}$")


def _validate_question(question: str, settings: ChatSettings) -> str:
    value = str(question or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="Question is required")
    if len(value) > settings.max_question_chars:
        raise HTTPException(status_code=413, detail=f"Question exceeds {settings.max_question_chars} characters")
    return value


def validate_conversation_id(value: str) -> str:
    if not CONVERSATION_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return value


def configured_generation() -> dict[str, Any]:
    config = GenerationConfig.from_env()
    if config.provider in {"", "none"}:
        raise HTTPException(status_code=503, detail="Generation provider is not configured")
    return generation_snapshot(config.provider, config.model, config.max_output_tokens)


def create_conversation(conn, books_root: Path, book_id: int, question: str, settings: ChatSettings) -> tuple[str, str]:
    book = get_book(conn, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    if book.get("phase") != "complete":
        raise HTTPException(status_code=409, detail="Book is not ready for chat")
    book_revision_id = get_active_healthy_book_revision(conn, book_id)
    if book_revision_id is None:
        raise HTTPException(status_code=409, detail="Book has no healthy Grounded knowledge revision; regenerate the wiki first")
    snapshot_path = books_root / str(book_id) / ".onebookwiki" / "generation-config.json"
    if snapshot_path.is_file():
        try:
            import json
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict):
                raise ValueError("invalid generation snapshot")
            generation = verify_generation_snapshot(snapshot)
        except (OSError, ValueError, TypeError):
            raise HTTPException(status_code=409, detail="Book generation configuration is invalid")
    else:
        raise HTTPException(status_code=409, detail="Book has no generation configuration snapshot; regenerate the wiki first")
    generation = generation_snapshot(
        str(generation["provider"]), str(generation["model"]), int(generation["max_output_tokens"]),
    )
    return create_chat_conversation(
        conn, book_id, _validate_question(question, settings), generation, book_revision_id
    )


def append_turn(conn, conversation_id: str, question: str, settings: ChatSettings) -> str:
    validate_conversation_id(conversation_id)
    if get_chat_conversation(conn, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        return append_chat_turn(conn, conversation_id, _validate_question(question, settings), settings.max_turns)
    except RuntimeError as exc:
        if str(exc) == "conversation_busy":
            raise HTTPException(status_code=409, detail="Conversation already has a pending turn") from exc
        raise
    except OverflowError as exc:
        raise HTTPException(status_code=409, detail="Conversation turn limit reached") from exc


def conversation_payload(conn, conversation_id: str) -> dict[str, Any]:
    validate_conversation_id(conversation_id)
    result = get_chat_conversation(conn, conversation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return result
