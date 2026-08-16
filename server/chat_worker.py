"""Standalone worker for durable, non-streaming chat jobs."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import time
from pathlib import Path

from server.config import ChatSettings, books_root, db_path
from server.database import claim_chat_job, complete_chat_turn, init_db, mark_chat_turn_generating
from onebookwiki.chat_retrieval import ChatRetrievalError, retrieve_chat_context, validate_answer
from onebookwiki.ledger import append_usage
from onebookwiki.providers import GenerationConfig, ProviderUnavailable, generate_response


def _history(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT question, answer FROM chat_turns WHERE conversation_id = ? AND status = 'succeeded' ORDER BY turn_no",
        (conversation_id,),
    ).fetchall()
    history: list[dict[str, str]] = []
    for row in rows:
        history.extend(({"role": "user", "content": row["question"]}, {"role": "assistant", "content": row["answer"] or ""}))
    return history


def process_job(conn: sqlite3.Connection, job: dict, root: Path, settings: ChatSettings, worker_id: str) -> None:
    turn_id = str(job["id"])
    book_root = root / str(job["book_id"])
    try:
        snapshot = json.loads(str(job["generation_config_json"]))
        current = GenerationConfig.from_env()
        if (
            snapshot.get("provider") != current.provider
            or snapshot.get("model") != current.model
            or int(snapshot.get("max_output_tokens", 0)) != current.max_output_tokens
        ):
            raise ChatRetrievalError("generation_config_mismatch", "当前服务配置与生成该书时的配置不一致。")
        history = _history(conn, str(job["conversation_id"]))
        retrieved = retrieve_chat_context(book_root, str(job["question"]), history)
        if not mark_chat_turn_generating(conn, turn_id, worker_id):
            return
        response = generate_response(
            retrieved["prompt"],
            provider=str(snapshot["provider"]),
            model=str(snapshot["model"]),
            max_output_tokens=int(snapshot["max_output_tokens"]),
        )
        citations = validate_answer(response.text, set(retrieved["allowed_evidence_ids"]))
        citation_records = [item for item in retrieved["raw_evidence"] if item["evidence_id"] in citations]
        completed = complete_chat_turn(
            conn, turn_id, status="succeeded", answer=response.text, citations=citation_records,
            retrieval={"allowed_evidence_ids": retrieved["allowed_evidence_ids"], "selected": retrieved["selected"]},
            prompt_hash=__import__("hashlib").sha256(retrieved["prompt"].encode("utf-8")).hexdigest(),
            provider_request_id=response.request_id, usage=response.usage, claimed_by=worker_id,
        )
        if completed:
            append_usage(
                book_root, run_id=f"chat:{job['conversation_id']}", node_id=turn_id, stage="chat", attempt=int(job.get("attempt", 1)),
                provider=str(snapshot["provider"]), model=response.model or str(snapshot["model"]), usage=response.usage,
                estimated=response.estimated_usage, prompt=retrieved["prompt"],
            )
    except ChatRetrievalError as exc:
        complete_chat_turn(conn, turn_id, status="refused", answer=f"证据不足，无法回答：{exc}", refusal_code=exc.code, claimed_by=worker_id)
    except ProviderUnavailable as exc:
        complete_chat_turn(conn, turn_id, status="failed", error_code="provider_unavailable", error_message=str(exc), claimed_by=worker_id)
    except Exception as exc:  # noqa: BLE001 - durable job boundary
        complete_chat_turn(conn, turn_id, status="failed", error_code=type(exc).__name__, error_message=str(exc)[:1000], claimed_by=worker_id)


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
                process_job(conn, job, root, settings, worker_id)
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
