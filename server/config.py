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


def agent_policy_snapshot(embedding_backend: str | None = None) -> dict[str, object]:
    """Return the bounded, non-secret policy captured for a chat conversation."""
    from onebookwiki.chat_agent import AgentPolicy

    values: dict[str, object] = {
        "max_actions": os.getenv("ONEBOOKWIKI_CHAT_AGENT_MAX_ACTIONS", AgentPolicy.max_actions),
        "max_seconds": os.getenv("ONEBOOKWIKI_CHAT_AGENT_MAX_SECONDS", AgentPolicy.max_seconds),
        "max_observation_tokens": os.getenv("ONEBOOKWIKI_CHAT_AGENT_OBSERVATION_TOKENS", AgentPolicy.max_observation_tokens),
        "max_planner_output_tokens": os.getenv("ONEBOOKWIKI_CHAT_AGENT_PLANNER_TOKENS", AgentPolicy.max_planner_output_tokens),
        "max_page_results": os.getenv("ONEBOOKWIKI_CHAT_AGENT_PAGE_RESULTS", AgentPolicy.max_page_results),
        "max_raw_candidates": os.getenv("ONEBOOKWIKI_CHAT_AGENT_RAW_CANDIDATES", AgentPolicy.max_raw_candidates),
        "max_evidence": os.getenv("ONEBOOKWIKI_CHAT_AGENT_MAX_EVIDENCE", AgentPolicy.max_evidence),
        "final_evidence_tokens": os.getenv("ONEBOOKWIKI_CHAT_AGENT_EVIDENCE_TOKENS", AgentPolicy.final_evidence_tokens),
        "retrieval": os.getenv("ONEBOOKWIKI_CHAT_AGENT_RETRIEVAL", AgentPolicy.retrieval),
        "embedding_backend": embedding_backend or os.getenv(
            "ONEBOOKWIKI_CHAT_EMBEDDING_BACKEND", AgentPolicy.embedding_backend
        ),
    }
    return AgentPolicy.from_dict(values).to_dict()


def generation_snapshot(
    provider: str,
    model: str,
    max_output_tokens: int,
    agent_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a non-secret, stable generation configuration snapshot."""
    value: dict[str, object] = {
        "provider": provider,
        "model": model,
        "max_output_tokens": int(max_output_tokens),
        "agent_policy": agent_policy or agent_policy_snapshot(),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    value["config_hash"] = hashlib.sha256(encoded).hexdigest()
    return value
