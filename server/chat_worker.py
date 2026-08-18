"""Standalone worker for durable, non-streaming hierarchical chat jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

from onebookwiki.book_navigation import BookCatalog
from onebookwiki.chat_agent import AgentPolicy, AgentRunError, AgentTraceEvent, AgenticChatRunner
from onebookwiki.chat_retrieval import ChatRetrievalError
from onebookwiki.ledger import append_chat_audit_event, append_chat_model_call_audit, append_usage
from onebookwiki.providers import GenerationConfig, ProviderUnavailable, build_embedder
from onebookwiki.wiki_vector_index import WikiVectorIndex
from server.config import ChatSettings, books_root, db_path, verify_generation_snapshot
from server.database import (
    append_chat_agent_step,
    chat_job_owned,
    claim_chat_job,
    complete_chat_turn,
    init_db,
    mark_chat_turn_generating,
    renew_chat_lease,
)


def _history(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT question, answer FROM chat_turns WHERE conversation_id = ? AND status = 'succeeded' ORDER BY turn_no",
        (conversation_id,),
    ).fetchall()
    history: list[dict[str, str]] = []
    for row in rows:
        history.extend(({"role": "user", "content": row["question"]}, {"role": "assistant", "content": row["answer"] or ""}))
    return history


class _LeaseHeartbeat:
    """Keep an agent job alive through blocking embedder and LLM operations."""

    def __init__(self, database: Path, turn_id: str, worker_id: str, settings: ChatSettings):
        self.database = database
        self.turn_id = turn_id
        self.worker_id = worker_id
        self.settings = settings
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def run() -> None:
            connection = init_db(str(self.database))
            try:
                while not self._stop.wait(self.settings.lease_heartbeat_seconds):
                    if not renew_chat_lease(connection, self.turn_id, self.worker_id, self.settings.lease_seconds):
                        self._lost.set()
                        return
            finally:
                connection.close()

        self._thread = threading.Thread(target=run, name=f"chat-lease-{self.turn_id[:8]}", daemon=True)
        self._thread.start()

    def active(self) -> bool:
        return not self._lost.is_set()

    def mark_lost(self) -> None:
        self._lost.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.settings.lease_heartbeat_seconds + 1)


_EVIDENCE_REFUSAL_CODES = {"raw_evidence_missing"}


def _snapshot_policy(snapshot: dict[str, object]) -> AgentPolicy:
    try:
        return AgentPolicy.from_dict(snapshot.get("agent_policy") if isinstance(snapshot.get("agent_policy"), dict) else None)
    except (TypeError, ValueError) as exc:
        raise AgentRunError("generation_config_invalid", "聊天 Agent 配置无效。") from exc


def _terminal_error(exc: BaseException) -> tuple[str, str, str | None]:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ProviderUnavailable):
            return "failed", "provider_unavailable", str(cause)
        cause = cause.__cause__
    if isinstance(exc, AgentRunError):
        if exc.code in _EVIDENCE_REFUSAL_CODES:
            return "refused", exc.code, None
        return "failed", exc.code, str(exc)
    if isinstance(exc, ChatRetrievalError):
        if exc.code in _EVIDENCE_REFUSAL_CODES:
            return "refused", exc.code, None
        return "failed", exc.code, str(exc)
    return "failed", "operational_error", f"{type(exc).__name__}: {str(exc)[:900]}"


def _record_terminal(
    conn: sqlite3.Connection,
    book_root: Path,
    turn_id: str,
    attempt: int,
    worker_id: str,
    heartbeat: _LeaseHeartbeat,
    *,
    status: str,
    audit_payload: dict[str, object],
    error_payload: dict[str, object] | None = None,
    **kwargs: object,
) -> bool:
    """Persist the terminal row, then audit only a successful ownership write."""
    if not heartbeat.active():
        return False
    written = complete_chat_turn(conn, turn_id, status=status, claimed_by=worker_id, **kwargs)
    if not written:
        heartbeat.mark_lost()
        return False
    if error_payload is not None:
        append_chat_audit_event(
            book_root,
            turn_id=turn_id,
            attempt=attempt,
            event_type="error",
            payload=error_payload,
        )
    append_chat_audit_event(
        book_root,
        turn_id=turn_id,
        attempt=attempt,
        event_type="terminal",
        payload={"status": status, **audit_payload},
    )
    return True


def process_job(
    conn: sqlite3.Connection,
    job: dict,
    root: Path,
    settings: ChatSettings,
    worker_id: str,
    database: Path | None = None,
) -> None:
    turn_id = str(job["id"])
    book_root = root / str(job["book_id"])
    attempt = int(job.get("attempt", 1))
    heartbeat = _LeaseHeartbeat(database or db_path(), turn_id, worker_id, settings)
    heartbeat.start()
    try:
        try:
            raw_snapshot = json.loads(str(job["generation_config_json"]))
            if not isinstance(raw_snapshot, dict):
                raise ValueError("generation snapshot must be an object")
            snapshot = verify_generation_snapshot(
                raw_snapshot,
                expected_hash=str(job.get("generation_config_hash") or ""),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentRunError("generation_config_invalid", "生成配置快照无效。") from exc

        current = GenerationConfig.from_env()
        try:
            snapshot_max_tokens = int(snapshot["max_output_tokens"])
        except (TypeError, ValueError) as exc:
            raise AgentRunError("generation_config_invalid", "生成配置快照无效。") from exc
        if (
            snapshot["provider"] != current.provider
            or snapshot["model"] != current.model
            or snapshot_max_tokens != current.max_output_tokens
            or str(job.get("generation_provider")) != str(snapshot["provider"])
            or str(job.get("generation_model")) != str(snapshot["model"])
            or int(job.get("generation_max_tokens", 0)) != snapshot_max_tokens
        ):
            raise AgentRunError("generation_config_mismatch", "当前服务配置与生成该书时的配置不一致。")
        policy = _snapshot_policy(snapshot)
        embedder = build_embedder(policy.embedding_backend)
        catalog = BookCatalog(book_root, WikiVectorIndex(book_root, embedder))
        history = _history(conn, str(job["conversation_id"]))

        def record_trace(event: AgentTraceEvent) -> None:
            if not append_chat_agent_step(conn, turn_id, attempt, event, claimed_by=worker_id):
                heartbeat.mark_lost()
                return
            append_chat_audit_event(
                book_root,
                turn_id=turn_id,
                attempt=attempt,
                event_type="action",
                payload={
                    "step_no": event.step_no,
                    "phase": event.phase,
                    "action": event.action,
                    "status": event.status,
                    "error_code": event.error_code,
                    "prompt_hash": event.prompt_hash,
                    "provider_request_id": event.provider_request_id,
                    "usage": event.usage or {},
                    "result": event.result,
                },
            )

        def record_usage(stage: str, prompt: str, response) -> None:
            append_chat_model_call_audit(
                book_root,
                turn_id=turn_id,
                attempt=attempt,
                stage=stage,
                prompt=prompt,
                response=response,
            )
            append_usage(
                book_root,
                run_id=f"chat:{job['conversation_id']}",
                node_id=f"{turn_id}:{stage}",
                stage=stage,
                attempt=attempt,
                provider=str(snapshot["provider"]),
                model=response.model or str(snapshot["model"]),
                usage=response.usage,
                estimated=response.estimated_usage,
                prompt=prompt,
            )

        runner = AgenticChatRunner(
            catalog,
            provider=str(snapshot["provider"]),
            model=str(snapshot["model"]),
            max_output_tokens=snapshot_max_tokens,
            policy=policy,
            on_trace=record_trace,
            on_model_call=record_usage,
            on_finalizing=lambda: heartbeat.active() and mark_chat_turn_generating(conn, turn_id, worker_id),
            ownership_active=lambda: heartbeat.active() and chat_job_owned(conn, turn_id, worker_id),
        )
        result = runner.run(str(job["question"]), history)
        if not heartbeat.active() or not chat_job_owned(conn, turn_id, worker_id):
            return
        citations = [item for item in result.evidence if item["evidence_id"] in result.citations]
        _record_terminal(
            conn,
            book_root,
            turn_id,
            attempt,
            worker_id,
            heartbeat,
            status="succeeded",
            audit_payload={
                "citation_count": len(citations),
                "action_count": len(result.trace),
                "answer_chars": len(result.answer),
                "prompt_hash": hashlib.sha256(result.final_prompt.encode("utf-8")).hexdigest(),
                "provider_request_id": result.provider_request_id,
                "usage": result.usage,
            },
            answer=result.answer,
            citations=citations,
            retrieval={
                "agent_policy": policy.to_dict(),
                "citations": result.citations,
                "actions": [event.action for event in result.trace],
            },
            prompt_hash=hashlib.sha256(result.final_prompt.encode("utf-8")).hexdigest(),
            provider_request_id=result.provider_request_id,
            usage=result.usage,
        )
    except BaseException as exc:  # noqa: BLE001 - durable job boundary
        status, code, detail = _terminal_error(exc)
        if not heartbeat.active():
            return
        if status == "refused":
            answer = f"证据不足，无法回答：{exc}"
            terminal_kwargs = {"answer": answer, "refusal_code": code}
        else:
            terminal_kwargs = {"error_code": code, "error_message": detail}
        _record_terminal(
            conn,
            book_root,
            turn_id,
            attempt,
            worker_id,
            heartbeat,
            status=status,
            audit_payload={"error_code": code, "exception_type": type(exc).__name__},
            error_payload={"error_code": code, "exception_type": type(exc).__name__},
            **terminal_kwargs,
        )
    finally:
        heartbeat.stop()


def run_worker(root: Path | None = None, database: Path | None = None, once: bool = False) -> None:
    root = root or books_root()
    database = database or db_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(database))
    settings = ChatSettings.from_env()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    try:
        while True:
            job = claim_chat_job(conn, worker_id, settings.lease_seconds)
            if job:
                process_job(conn, job, root, settings, worker_id, database)
            elif once:
                return
            else:
                time.sleep(settings.worker_idle_seconds)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--books-root", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args(argv)
    run_worker(args.books_root, args.database, args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
