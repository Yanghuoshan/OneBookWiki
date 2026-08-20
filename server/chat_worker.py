"""Standalone worker for durable, non-streaming Grounded v2 chat jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onebookwiki.answer_planning import (
    AnswerPlanError,
    build_answer_plan,
    load_citation_snapshot,
    persist_answer_plan,
    persist_citation_snapshot,
    validate_answer_payload,
)
from onebookwiki.coverage import CoverageError, KnowledgeRetriever
from onebookwiki.ledger import append_chat_audit_event, append_chat_model_call_audit, append_usage
from onebookwiki.providers import (
    GenerationConfig,
    ProviderUnavailable,
    build_answer_plan_prompt,
    generate_structured_response,
)
from onebookwiki.query_analyzer import QueryAnalysisError, analyze_query
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


class ChatWorkerError(RuntimeError):
    """A durable chat job cannot be answered; fail the turn closed."""

    def __init__(self, code: str, message: str, *, refusal: bool = False):
        super().__init__(message)
        self.code = code
        self.refusal = refusal


@dataclass(frozen=True)
class _TraceStep:
    step_no: int
    phase: str
    action: str
    status: str = "completed"
    request: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    prompt_hash: str | None = None
    provider_request_id: str | None = None
    usage: dict[str, int] | None = None


class _LeaseHeartbeat:
    """Keep a chat job alive through blocking retrieval and LLM operations."""

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


def _terminal_error(exc: BaseException) -> tuple[str, str, str | None]:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ProviderUnavailable):
            return "failed", "provider_unavailable", str(cause)
        cause = cause.__cause__
    if isinstance(exc, ChatWorkerError):
        if exc.refusal:
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


def _evidence_citations(
    conn: sqlite3.Connection, book_revision_id: str, plan, evidence_ids: set[str]
) -> list[dict[str, Any]]:
    """Build citation records for exactly the pinned evidence a segment cited."""
    citations: list[dict[str, Any]] = []
    for evidence_id in sorted(evidence_ids):
        quote = plan.evidence_bundle.quotes.get(evidence_id)
        if quote is None:
            continue
        span = plan.evidence_bundle.spans.get(evidence_id, {})
        row = conn.execute(
            "SELECT source_path, chapter FROM evidence_revisions WHERE id = ? AND book_revision_id = ?",
            (evidence_id, book_revision_id),
        ).fetchone()
        citations.append({
            "evidence_id": evidence_id,
            "evidence_revision_id": evidence_id,
            "book_revision_id": book_revision_id,
            "source_path": str(row["source_path"]) if row else None,
            "chapter": int(row["chapter"]) if row else None,
            "start_line": span.get("source_line_start"),
            "end_line": span.get("source_line_end"),
            "quote": quote,
        })
    return citations


def process_job(
    conn: sqlite3.Connection,
    job: dict,
    root: Path,
    settings: ChatSettings,
    worker_id: str,
    database: Path | None = None,
) -> None:
    turn_id = str(job["id"])
    book_id = int(job["book_id"])
    book_root = root / str(book_id)
    attempt = int(job.get("attempt", 1))
    heartbeat = _LeaseHeartbeat(database or db_path(), turn_id, worker_id, settings)
    heartbeat.start()
    pinned_revision_status = "pending"
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
            raise ChatWorkerError("generation_config_invalid", "生成配置快照无效。") from exc

        current = GenerationConfig.from_env()
        try:
            snapshot_max_tokens = int(snapshot["max_output_tokens"])
        except (TypeError, ValueError) as exc:
            raise ChatWorkerError("generation_config_invalid", "生成配置快照无效。") from exc
        if (
            snapshot["provider"] != current.provider
            or snapshot["model"] != current.model
            or snapshot_max_tokens != current.max_output_tokens
            or str(job.get("generation_provider")) != str(snapshot["provider"])
            or str(job.get("generation_model")) != str(snapshot["model"])
            or int(job.get("generation_max_tokens", 0)) != snapshot_max_tokens
        ):
            raise ChatWorkerError("generation_config_mismatch", "当前服务配置与生成该书时的配置不一致。")

        book_revision_id = str(job.get("book_revision_id") or "")
        if not book_revision_id:
            raise ChatWorkerError("book_revision_missing", "对话未绑定书籍知识版本。")

        try:
            analysis = analyze_query(str(job["question"]))
        except QueryAnalysisError as exc:
            raise ChatWorkerError("question_invalid", "问题无法解析。") from exc

        try:
            retriever = KnowledgeRetriever(conn, book_id, book_revision_id)
        except CoverageError as exc:
            raise ChatWorkerError(
                "book_revision_unhealthy", "绑定的书籍知识版本已失效，无法继续回答。"
            ) from exc
        pinned_revision_status = "healthy"

        retrieval = retriever.retrieve(analysis)

        policy_snapshot = {
            "contract_version": "grounded-v2",
            "worker": "chat_worker",
            "generation": {
                "provider": str(snapshot["provider"]),
                "model": str(snapshot["model"]),
                "max_output_tokens": snapshot_max_tokens,
            },
        }
        try:
            plan, coverage = build_answer_plan(
                conn, root, book_id, book_revision_id, analysis,
                policy_snapshot=policy_snapshot, retriever=retriever,
            )
        except AnswerPlanError as exc:
            raise ChatWorkerError(
                "insufficient_pinned_evidence", f"证据不足，无法回答：{exc}", refusal=True
            ) from exc

        persist_answer_plan(conn, plan)
        citation_snapshot_id = persist_citation_snapshot(conn, plan)
        load_citation_snapshot(conn, plan.answer_plan_id)

        if not append_chat_agent_step(
            conn, turn_id, attempt,
            _TraceStep(
                step_no=1, phase="retrieving", action="coverage_evaluation",
                request={"query_hash": analysis.query_hash}, result=coverage.to_dict(),
            ),
            claimed_by=worker_id,
        ):
            heartbeat.mark_lost()
            return

        if not heartbeat.active() or not chat_job_owned(conn, turn_id, worker_id):
            return
        if not mark_chat_turn_generating(conn, turn_id, worker_id):
            heartbeat.mark_lost()
            return

        prompt_hash: str | None = None
        provider_request_id: str | None = None
        usage: dict[str, int] | None = None

        if plan.answer_mode == "canonical_passthrough":
            composition_ids = plan.composition_revision_ids
            if len(composition_ids) != 1:
                raise ChatWorkerError("plan_invalid", "canonical 回答缺少唯一的定案组合。")
            rendering = conn.execute(
                "SELECT rendering_text FROM composition_renderings WHERE composition_revision_id = ?",
                (composition_ids[0],),
            ).fetchone()
            if rendering is None:
                raise ChatWorkerError("plan_invalid", "canonical 回答缺少渲染文本。")
            answer_text = str(rendering["rendering_text"])
            cited_evidence_ids = set(plan.evidence_bundle.evidence_revision_ids)
        else:
            statement_lookup = {item.statement_revision_id: item for item in retrieval.statements}
            prompt_statements = [
                {
                    "statement_revision_id": statement_id,
                    "semantic_text": statement_lookup[statement_id].semantic_text,
                    "evidence_revision_ids": list(statement_lookup[statement_id].evidence_revision_ids),
                }
                for statement_id in plan.statement_revision_ids
                if statement_id in statement_lookup
            ]
            prompt = build_answer_plan_prompt(
                str(job["question"]), plan.answer_mode, prompt_statements, dict(plan.evidence_bundle.quotes),
            )
            response = generate_structured_response(
                prompt,
                provider=str(snapshot["provider"]),
                model=str(snapshot["model"]),
                max_output_tokens=snapshot_max_tokens,
            )
            append_chat_model_call_audit(
                book_root, turn_id=turn_id, attempt=attempt, stage="answer_plan", prompt=prompt, response=response,
            )
            append_usage(
                book_root,
                run_id=f"chat:{job['conversation_id']}",
                node_id=f"{turn_id}:answer_plan",
                stage="answer_plan",
                attempt=attempt,
                provider=str(snapshot["provider"]),
                model=response.model or str(snapshot["model"]),
                usage=response.usage,
                estimated=response.estimated_usage,
                prompt=prompt,
            )
            try:
                payload = json.loads(response.text)
            except (TypeError, ValueError) as exc:
                raise ChatWorkerError("model_payload_invalid", "模型返回的答案格式无效。") from exc
            try:
                segments = validate_answer_payload(plan, payload)
            except AnswerPlanError as exc:
                raise ChatWorkerError("model_payload_invalid", f"模型答案未通过验证：{exc}") from exc
            answer_text = "\n\n".join(segment["text"] for segment in segments)
            cited_evidence_ids = set()
            for segment in segments:
                cited_evidence_ids.update(segment["evidence_revision_ids"])
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            provider_request_id = response.request_id
            usage = response.usage

        citations = _evidence_citations(conn, book_revision_id, plan, cited_evidence_ids)

        if not append_chat_agent_step(
            conn, turn_id, attempt,
            _TraceStep(
                step_no=2, phase="generating", action=plan.answer_mode,
                request={"answer_plan_id": plan.answer_plan_id},
                result={"citation_count": len(citations)},
                prompt_hash=prompt_hash, provider_request_id=provider_request_id, usage=usage,
            ),
            claimed_by=worker_id,
        ):
            heartbeat.mark_lost()
            return

        if not heartbeat.active() or not chat_job_owned(conn, turn_id, worker_id):
            return
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
                "answer_mode": plan.answer_mode,
                "answer_chars": len(answer_text),
                "provider_request_id": provider_request_id,
                "usage": usage or {},
            },
            answer=answer_text,
            citations=citations,
            retrieval={"answer_mode": plan.answer_mode, "answer_plan_id": plan.answer_plan_id},
            prompt_hash=prompt_hash,
            provider_request_id=provider_request_id,
            usage=usage,
            answer_plan_id=plan.answer_plan_id,
            citation_snapshot_id=citation_snapshot_id,
            answer_mode=plan.answer_mode,
            pinned_revision_status=pinned_revision_status,
            plan_summary=coverage.to_dict(),
        )
    except BaseException as exc:  # noqa: BLE001 - durable job boundary
        status, code, detail = _terminal_error(exc)
        if not heartbeat.active():
            return
        if status == "refused":
            terminal_kwargs: dict[str, object] = {"answer": str(exc), "refusal_code": code}
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
            pinned_revision_status=pinned_revision_status,
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
