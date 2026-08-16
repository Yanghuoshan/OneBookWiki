"""Shared server configuration for API and chat worker processes."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def books_root() -> Path:
    configured = os.getenv("ONEBOOKWIKI_BOOKS_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else PROJECT_ROOT / "books"


def db_path() -> Path:
    return books_root() / "onebookwiki.db"


@dataclass(frozen=True)
class ChatSettings:
    max_question_chars: int = 4000
    max_turns: int = 100
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 300
    worker_idle_seconds: float = 1.0
    admin_token: str | None = None

    @classmethod
    def from_env(cls) -> "ChatSettings":
        def integer(name: str, default: int, minimum: int = 1) -> int:
            try:
                return max(minimum, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        def number(name: str, default: float, minimum: float = 0.1) -> float:
            try:
                return max(minimum, float(os.getenv(name, str(default))))
            except ValueError:
                return default

        return cls(
            max_question_chars=integer("ONEBOOKWIKI_CHAT_MAX_QUESTION_CHARS", cls.max_question_chars),
            max_turns=integer("ONEBOOKWIKI_CHAT_MAX_TURNS", cls.max_turns),
            poll_interval_seconds=number("ONEBOOKWIKI_CHAT_POLL_INTERVAL", cls.poll_interval_seconds),
            lease_seconds=integer("ONEBOOKWIKI_CHAT_LEASE_SECONDS", cls.lease_seconds),
            worker_idle_seconds=number("ONEBOOKWIKI_CHAT_WORKER_IDLE", cls.worker_idle_seconds),
            admin_token=os.getenv("ONEBOOKWIKI_ADMIN_TOKEN") or None,
        )


def generation_snapshot(provider: str, model: str, max_output_tokens: int) -> dict[str, object]:
    """Return a non-secret, stable generation configuration snapshot."""
    value = {
        "provider": provider,
        "model": model,
        "max_output_tokens": int(max_output_tokens),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    value["config_hash"] = hashlib.sha256(encoded).hexdigest()
    return value
