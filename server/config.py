"""Shared server configuration for API and chat worker processes."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
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
    lease_heartbeat_seconds: float = 60.0
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

        lease_seconds = integer("ONEBOOKWIKI_CHAT_LEASE_SECONDS", cls.lease_seconds)
        heartbeat_default = min(cls.lease_heartbeat_seconds, max(1.0, lease_seconds / 3))
        heartbeat = number("ONEBOOKWIKI_CHAT_LEASE_HEARTBEAT", heartbeat_default)
        return cls(
            max_question_chars=integer("ONEBOOKWIKI_CHAT_MAX_QUESTION_CHARS", cls.max_question_chars),
            max_turns=integer("ONEBOOKWIKI_CHAT_MAX_TURNS", cls.max_turns),
            poll_interval_seconds=number("ONEBOOKWIKI_CHAT_POLL_INTERVAL", cls.poll_interval_seconds),
            lease_seconds=lease_seconds,
            lease_heartbeat_seconds=min(heartbeat, max(1.0, lease_seconds / 3)),
            worker_idle_seconds=number("ONEBOOKWIKI_CHAT_WORKER_IDLE", cls.worker_idle_seconds),
            admin_token=os.getenv("ONEBOOKWIKI_ADMIN_TOKEN") or None,
        )


def generation_config_hash(snapshot: Mapping[str, object]) -> str:
    """Hash the exact persisted payload, excluding only its hash field."""
    payload = dict(snapshot)
    payload.pop("config_hash", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_generation_snapshot(
    snapshot: Mapping[str, object],
    *,
    expected_hash: str | None = None,
) -> dict[str, object]:
    """Validate a snapshot before applying any compatibility defaults."""
    value = dict(snapshot)
    if not all(key in value for key in ("provider", "model", "max_output_tokens", "config_hash")):
        raise ValueError("generation snapshot is missing required fields")
    if not isinstance(value["provider"], str) or not isinstance(value["model"], str):
        raise ValueError("generation snapshot provider/model must be strings")
    try:
        max_tokens = int(value["max_output_tokens"])
    except (TypeError, ValueError) as exc:
        raise ValueError("generation snapshot max_output_tokens must be an integer") from exc
    if max_tokens < 1:
        raise ValueError("generation snapshot max_output_tokens must be positive")
    actual = generation_config_hash(value)
    stored = str(value["config_hash"])
    if not hmac.compare_digest(actual, stored):
        raise ValueError("generation snapshot hash is invalid")
    if expected_hash is not None and not hmac.compare_digest(stored, str(expected_hash)):
        raise ValueError("generation snapshot does not match conversation hash")
    return value


def generation_snapshot(
    provider: str,
    model: str,
    max_output_tokens: int,
) -> dict[str, object]:
    """Return a non-secret, stable generation configuration snapshot."""
    value: dict[str, object] = {
        "provider": provider,
        "model": model,
        "max_output_tokens": int(max_output_tokens),
    }
    value["config_hash"] = generation_config_hash(value)
    return value
