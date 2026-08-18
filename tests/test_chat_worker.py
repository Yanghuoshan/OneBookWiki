import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.chat_agent import AgentPolicy, AgentResult, AgentTraceEvent
from onebookwiki.chat_retrieval import ChatRetrievalError
from onebookwiki.ledger import append_chat_model_call_audit
from onebookwiki.providers import GenerationConfig, GenerationResponse
from server.chat_worker import process_job
from server.config import ChatSettings, generation_snapshot
from server.database import (
    claim_chat_job,
    create_chat_conversation,
    create_placeholder,
    get_chat_agent_trace,
    init_db,
)


class SuccessfulRunner:
    last_finalizing_status = None

    def __init__(self, *args, **kwargs):
        self.on_trace = kwargs["on_trace"]
        self.on_model_call = kwargs["on_model_call"]
        self.on_finalizing = kwargs["on_finalizing"]

    def run(self, question, history):
        plan = GenerationResponse(
            text='{"type":"action"}',
            model="test-model",
            usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        )
        self.on_model_call("chat_plan", "planner prompt", plan)
        self.on_trace(AgentTraceEvent(
            step_no=1,
            phase="retrieving",
            action="book_overview",
            request={"arguments": {}},
            result={"title": "Test book"},
            usage=plan.usage,
        ))
        type(self).last_finalizing_status = self.on_finalizing()
        final = GenerationResponse(
            text="Supported answer. C1E1",
            model="test-model",
            usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            request_id="request-1",
        )
        self.on_model_call("chat_final", "final prompt", final)
        self.on_trace(AgentTraceEvent(
            step_no=2,
            phase="generating",
            action="final_answer",
            request={"evidence_ids": ["C1E1"]},
            result={"citations": ["C1E1"]},
            provider_request_id=final.request_id,
            usage=final.usage,
        ))
        evidence = [{"evidence_id": "C1E1", "source_path": "raw/chapters/01.md"}]
        return AgentResult(
            answer=final.text,
            citations=["C1E1"],
            evidence=evidence,
            trace=[],
            usage={"prompt_tokens": 6, "completion_tokens": 3, "total_tokens": 9},
            estimated_usage=False,
            model=final.model,
            provider_request_id=final.request_id,
            final_prompt="final prompt",
        )


class RetrievalFailureRunner:
    def __init__(self, *args, **kwargs):
        pass

    def run(self, question, history):
        raise ChatRetrievalError("answer_uncited_claim", "answer paragraph has no citation")


class ChatWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "books"
        self.root.mkdir()
        self.database = Path(self.tmp.name) / "onebookwiki.db"
        self.conn = init_db(str(self.database))
        self.book_id = create_placeholder(self.conn, "Worker book")
        self.book_root = self.root / str(self.book_id)
        self.book_root.mkdir()
        self.settings = ChatSettings(lease_seconds=60, lease_heartbeat_seconds=60)

        current = GenerationConfig.from_env()
        self.generation = generation_snapshot(
            current.provider,
            current.model,
            current.max_output_tokens,
            AgentPolicy().to_dict(),
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def create_job(self):
        _, turn_id = create_chat_conversation(
            self.conn,
            self.book_id,
            "What is supported?",
            self.generation,
        )
        job = claim_chat_job(self.conn, "worker-a", self.settings.lease_seconds)
        self.assertEqual(job["id"], turn_id)
        return turn_id, job

    def runner_mocks(self, runner):
        return patch("server.chat_worker.AgenticChatRunner", runner), patch(
            "server.chat_worker.BookCatalog", return_value=object()
        ), patch("server.chat_worker.build_embedder", return_value=object())

    def test_worker_records_trace_usage_and_finalizes_after_generating(self):
        turn_id, job = self.create_job()
        runner_patch, catalog_patch, embedder_patch = self.runner_mocks(SuccessfulRunner)
        with runner_patch as mocked_runner, catalog_patch, embedder_patch:
            process_job(
                self.conn,
                job,
                self.root,
                self.settings,
                "worker-a",
                self.database,
            )

        row = self.conn.execute(
            "SELECT status, answer, prompt_tokens, completion_tokens, total_tokens FROM chat_turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["answer"], "Supported answer. C1E1")
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]), (6, 3, 9))
        self.assertTrue(SuccessfulRunner.last_finalizing_status)
        trace = get_chat_agent_trace(self.conn, turn_id)
        self.assertEqual([item["action"] for item in trace], ["book_overview", "final_answer"])
        self.assertEqual([item["phase"] for item in trace], ["retrieving", "generating"])

        usage_path = self.book_root / ".onebookwiki" / "usage.jsonl"
        entries = [json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["stage"] for item in entries], ["chat_plan", "chat_final"])
        self.assertEqual([item["node_id"] for item in entries], [f"{turn_id}:chat_plan", f"{turn_id}:chat_final"])

        audit_path = self.book_root / ".onebookwiki" / "chat-audit" / f"{turn_id}.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["schema_version"], 1)
        self.assertEqual(audit["turn_id"], turn_id)
        self.assertEqual([item["stage"] for item in audit["calls"]], ["chat_plan", "chat_final"])
        self.assertEqual([item["prompt"] for item in audit["calls"]], ["planner prompt", "final prompt"])
        self.assertEqual([item["output"] for item in audit["calls"]], ['{"type":"action"}', "Supported answer. C1E1"])
        self.assertEqual([item["request_id"] for item in audit["calls"]], [None, "request-1"])
        self.assertEqual([item["usage"]["total_tokens"] for item in audit["calls"]], [3, 6])
        self.assertTrue(all(item["prompt_hash"] for item in audit["calls"]))

    def test_chat_audit_appends_calls_across_attempts(self):
        turn_id, _ = self.create_job()
        first = GenerationResponse(text="first", model="test-model", request_id="request-1")
        second = GenerationResponse(text="second", model="test-model", request_id="request-2")
        append_chat_model_call_audit(
            self.book_root, turn_id=turn_id, attempt=1, stage="chat_final", prompt="prompt 1", response=first
        )
        append_chat_model_call_audit(
            self.book_root, turn_id=turn_id, attempt=2, stage="chat_final", prompt="prompt 2", response=second
        )

        audit_path = self.book_root / ".onebookwiki" / "chat-audit" / f"{turn_id}.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual([item["sequence"] for item in audit["calls"]], [1, 2])
        self.assertEqual([item["attempt"] for item in audit["calls"]], [1, 2])
        self.assertEqual([item["output"] for item in audit["calls"]], ["first", "second"])

    def test_controlled_retrieval_failure_becomes_refused_turn(self):
        turn_id, job = self.create_job()
        runner_patch, catalog_patch, embedder_patch = self.runner_mocks(RetrievalFailureRunner)
        with runner_patch, catalog_patch, embedder_patch:
            process_job(
                self.conn,
                job,
                self.root,
                self.settings,
                "worker-a",
                self.database,
            )

        row = self.conn.execute(
            "SELECT status, refusal_code, answer FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "refused")
        self.assertEqual(row["refusal_code"], "answer_uncited_claim")
        self.assertIn("证据不足", row["answer"])

    def test_worker_does_not_write_terminal_result_after_ownership_loss(self):
        turn_id, job = self.create_job()
        runner_patch, catalog_patch, embedder_patch = self.runner_mocks(SuccessfulRunner)
        with runner_patch, catalog_patch, embedder_patch, patch(
            "server.chat_worker.chat_job_owned", return_value=False
        ), patch("server.chat_worker.mark_chat_turn_generating", return_value=False):
            process_job(
                self.conn,
                job,
                self.root,
                self.settings,
                "worker-a",
                self.database,
            )

        row = self.conn.execute(
            "SELECT status, answer FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "retrieving")
        self.assertIsNone(row["answer"])


if __name__ == "__main__":
    unittest.main()
