"""Public opaque-URL chat conversation endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.chat_service import append_turn, conversation_payload, create_conversation
from server.config import ChatSettings

router = APIRouter(prefix="/api", tags=["chat"])


def _db(request: Request):
    return request.app.state.db


def _settings() -> ChatSettings:
    return ChatSettings.from_env()


@router.post("/books/{book_id}/chat-conversations", status_code=202)
def create_chat_conversation(book_id: int, body: dict, request: Request):
    """Start a new conversation; every submission from the wiki creates a new one."""
    conversation_id, turn_id = create_conversation(
        _db(request), request.app.state.books_root, book_id, body.get("question", ""), _settings()
    )
    return JSONResponse(
        {
            "conversationId": conversation_id,
            "turnId": turn_id,
            "status": "queued",
            "answerUrl": f"/book/{book_id}/ask/{conversation_id}",
        },
        status_code=202,
    )


@router.post("/chat-conversations/{conversation_id}/turns", status_code=202)
def append_chat_turn_endpoint(conversation_id: str, body: dict, request: Request):
    """Append one question to a known conversation URL."""
    turn_id = append_turn(_db(request), conversation_id, body.get("question", ""), _settings())
    return JSONResponse({"conversationId": conversation_id, "turnId": turn_id, "status": "queued"}, status_code=202)


@router.get("/chat-conversations/{conversation_id}")
def get_chat_conversation_endpoint(conversation_id: str, request: Request):
    """Return one conversation and its complete permanent turn history."""
    return JSONResponse(conversation_payload(_db(request), conversation_id))
