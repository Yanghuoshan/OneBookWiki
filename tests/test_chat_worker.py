import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.evidence_registry import register_project
from onebookwiki.knowledge_store import GroundedKnowledgeStore
from onebookwiki.providers import GenerationConfig, GenerationResponse
from server.chat_worker import process_job
from server.config import ChatSettings, generation_snapshot
from server.database import (
    claim_chat_job,
    create_chat_conversation,
    create_placeholder,
    get_chat_agent_trace,
    init_db,
    publish_registry_snapshot,
)


def _fake_generate_structured_response(prompt, provider=None, model=None, max_output_tokens=None):
    """Return a minimal valid segments payload citing exactly the prompt's allowed IDs."""
    lines = prompt.splitlines()
    statement_line = next(line for line in lines if line.startswith("ALLOWED statement_revision_ids:"))
    evidence_line = next(line for line in lines if line.startswith("ALLOWED evidence_revision_ids:"))
    raw_statements = statement_line.split(":", 1)[1].strip()
    statement_ids = [] if raw_statements == "(none)" else [item.strip() for item in raw_statements.split(",") if item.strip()]
    evidence_ids = [item.strip() for item in evidence_line.split(":", 1)[1].split(",") if item.strip()]
    payload = {
        "segments": [{
            "segment_id": "seg-1",
            "text": "Grounded claims require original evidence, per the pinned source text.",
            "statement_revision_ids": statement_ids,
            "evidence_revision_ids": evidence_ids[:1],
        }],
    }
    return GenerationResponse(
        text=json.dumps(payload),
        model="test-model",
        usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        request_id="request-1",
    )


class ChatWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        chapter = self.root / "raw" / "chapters" / "01-example.md"
        chapter.parent.mkdir(parents=True)
        chapter.write_text(
            "# Example\n\n> Chapter: 1\n\n"
            "Grounded claims require original evidence.\n\n"
            "Readers must verify each claim against its source text.\n",
            encoding="utf-8",
        )
        self.snapshot = register_project(self.root)
        self.database = self.root / "onebookwiki.db"
        self.conn = init_db(str(self.database))
        self.book_id = create_placeholder(self.conn, "Worker book")
        self.book_root = self.root / str(self.book_id)
        self.book_root.mkdir(parents=True, exist_ok=True)
        publish_registry_snapshot(self.conn, self.book_id, self.snapshot)

        store = GroundedKnowledgeStore(self.conn, self.root, self.book_id, self.snapshot.book_revision_id)
        by_line = {
            evidence["source_line_start"]: (evidence_id, evidence)
            for evidence_id, evidence in store.evidence.items()
        }
        first_id, first = by_line[min(by_line)]
        second_id, second = by_line[max(by_line)]
        self.evr1, self.evr2 = first_id, second_id

        def statement(draft_id, canonical_key, truth_condition, evidence_id, evidence):
            return {
                "draft_id": draft_id,
                "canonical_key": canonical_key,
                "kind": "factual",
                "subject": "Example",
                "truth_condition": truth_condition,
                "scope": {"documentary_scope": "reading_unit", "target_type": "chapter", "chapter": 1},
                "abstraction": "concrete_detail",
                "qualification": None,
                "confidence": "high",
                "supports": [{
                    "evidence_revision_id": evidence_id,
                    "support_type": "positive",
                    "quote": evidence["payload"]["quote"],
                    "span_map": {
                        "source_line_start": evidence["source_line_start"],
                        "source_line_end": evidence["source_line_end"],
                    },
                }],
            }

        store.publish(
            [
                statement("stmt-one", "example.claim1", "Grounded claims require original evidence.", self.evr1, first),
                statement("stmt-two", "example.claim2", "Readers must verify each claim against its source text.", self.evr2, second),
            ],
            [{
                "draft_id": "chapter-summary",
                "canonical_key": "chapter.1.summary",
                "kind": "chapter_summary",
                "members": [
                    {"member_type": "statement", "draft_id": "stmt-one", "role": "support"},
                    {"member_type": "statement", "draft_id": "stmt-two", "role": "support"},
                ],
                "rendering": {
                    "text": "This chapter's thesis is that trustworthy analysis depends on verifiable sourcing.",
                    "span_map": {},
                },
            }],
        )
        self.book_revision_id = self.snapshot.book_revision_id
        self.evidence_quotes = {first_id: first["payload"]["quote"], second_id: second["payload"]["quote"]}
        self.evidence_spans = {
            first_id: (first["source_line_start"], first["source_line_end"]),
            second_id: (second["source_line_start"], second["source_line_end"]),
        }
        self.settings = ChatSettings(lease_seconds=60, lease_heartbeat_seconds=60)
        current = GenerationConfig.from_env()
        self.generation = generation_snapshot(current.provider, current.model, current.max_output_tokens)

    def _publish_unrelated_book(self) -> tuple[int, str, str]:
        """Publish a second, unrelated book/revision in the same database."""
        other_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(other_root, ignore_errors=True))
        chapter = other_root / "raw" / "chapters" / "01-other.md"
        chapter.parent.mkdir(parents=True)
        chapter.write_text(
            "# Other\n\n> Chapter: 1\n\nAn unrelated book must never leak into another book's citations.\n",
            encoding="utf-8",
        )
        snapshot = register_project(other_root)
        other_book_id = create_placeholder(self.conn, "Unrelated book")
        publish_registry_snapshot(self.conn, other_book_id, snapshot)
        store = GroundedKnowledgeStore(self.conn, other_root, other_book_id, snapshot.book_revision_id)
        evidence_id, evidence = next(iter(store.evidence.items()))
        store.publish([{
            "draft_id": "other-claim", "canonical_key": "other.claim", "kind": "factual",
            "subject": "Other", "truth_condition": "An unrelated book must never leak into another book's citations.",
            "scope": {"documentary_scope": "reading_unit", "target_type": "chapter", "chapter": 1},
            "abstraction": "concrete_detail", "qualification": None, "confidence": "high",
            "supports": [{
                "evidence_revision_id": evidence_id, "support_type": "positive",
                "quote": evidence["payload"]["quote"],
                "span_map": {
                    "source_line_start": evidence["source_line_start"],
                    "source_line_end": evidence["source_line_end"],
                },
            }],
        }], [{
            "draft_id": "other-summary", "canonical_key": "other.summary", "kind": "chapter_summary",
            "members": [{"member_type": "statement", "draft_id": "other-claim", "role": "support"}],
            "rendering": {"text": "Unrelated book summary.", "span_map": {}},
        }])
        return other_book_id, snapshot.book_revision_id, evidence_id

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def create_job(self, question: str):
        _, turn_id = create_chat_conversation(
            self.conn, self.book_id, question, self.generation, self.book_revision_id
        )
        job = claim_chat_job(self.conn, "worker-a", self.settings.lease_seconds)
        self.assertEqual(job["id"], turn_id)
        return turn_id, job

    def test_canonical_summary_question_answers_without_a_model_call(self):
        turn_id, job = self.create_job("请总结这一章的核心论点")
        with patch("server.chat_worker.generate_structured_response") as mocked:
            process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)
        mocked.assert_not_called()

        row = self.conn.execute(
            "SELECT status, answer, answer_mode, answer_plan_id, citation_snapshot_id, "
            "pinned_revision_status FROM chat_turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["answer"], "This chapter's thesis is that trustworthy analysis depends on verifiable sourcing.")
        self.assertEqual(row["answer_mode"], "canonical_passthrough")
        self.assertTrue(row["answer_plan_id"].startswith("aplan-"))
        self.assertTrue(row["citation_snapshot_id"].startswith("csnap-"))
        self.assertEqual(row["pinned_revision_status"], "healthy")

        trace = get_chat_agent_trace(self.conn, turn_id)
        self.assertEqual([item["action"] for item in trace], ["coverage_evaluation", "canonical_passthrough"])

    def test_knowledge_synthesis_question_calls_model_with_pinned_allowlists(self):
        turn_id, job = self.create_job("Explain why grounded claims require original evidence in this chapter")
        with patch("server.chat_worker.generate_structured_response", side_effect=_fake_generate_structured_response):
            process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)

        row = self.conn.execute(
            "SELECT status, answer, answer_mode, citations_json FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["answer_mode"], "knowledge_synthesis")
        citations = json.loads(row["citations_json"])
        self.assertTrue(citations)
        self.assertIn(citations[0]["evidence_id"], {self.evr1, self.evr2})

        usage_path = self.book_root / ".onebookwiki" / "usage.jsonl"
        entries = [json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["stage"] for item in entries], ["answer_plan"])

    def test_raw_synthesis_question_falls_back_to_evidence_quotes(self):
        turn_id, job = self.create_job('原文怎么说"Grounded claims require original evidence."')
        with patch("server.chat_worker.generate_structured_response", side_effect=_fake_generate_structured_response):
            process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)

        row = self.conn.execute("SELECT status, answer_mode FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["answer_mode"], "raw_synthesis")

    def test_insufficient_evidence_becomes_a_refused_turn(self):
        turn_id, job = self.create_job("量子物理中的黑洞蒸发速率是多少")
        process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)

        row = self.conn.execute(
            "SELECT status, refusal_code, answer FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "refused")
        self.assertEqual(row["refusal_code"], "insufficient_pinned_evidence")
        self.assertIn("证据不足", row["answer"])

    def test_tampered_generation_snapshot_fails_closed(self):
        turn_id, job = self.create_job("请总结这一章的核心论点")
        tampered = dict(self.generation)
        tampered["model"] = "tampered-model"
        job["generation_config_json"] = json.dumps(tampered)
        process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)

        row = self.conn.execute("SELECT status, error_code FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        self.assertEqual((row["status"], row["error_code"]), ("failed", "generation_config_invalid"))

    def test_superseded_book_revision_fails_closed(self):
        turn_id, job = self.create_job("请总结这一章的核心论点")
        self.conn.execute(
            "UPDATE book_revisions SET status = 'superseded' WHERE id = ?", (self.book_revision_id,)
        )
        self.conn.commit()
        process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)

        row = self.conn.execute(
            "SELECT status, error_code, pinned_revision_status FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "book_revision_unhealthy")

    def test_worker_does_not_write_terminal_result_after_ownership_loss(self):
        turn_id, job = self.create_job("请总结这一章的核心论点")
        with patch("server.chat_worker.chat_job_owned", return_value=False):
            process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)

        row = self.conn.execute("SELECT status, answer FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        self.assertEqual(row["status"], "retrieving")
        self.assertIsNone(row["answer"])

    def test_completed_citations_include_only_immutable_same_revision_fields(self):
        # An unrelated book/revision sharing the same database must never leak
        # into this turn's citations even if evidence ids or line numbers collide.
        self._publish_unrelated_book()

        turn_id, job = self.create_job("Explain why grounded claims require original evidence in this chapter")
        with patch("server.chat_worker.generate_structured_response", side_effect=_fake_generate_structured_response):
            process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)

        row = self.conn.execute(
            "SELECT status, citations_json FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "succeeded")
        citations = json.loads(row["citations_json"])
        self.assertTrue(citations)
        expected_keys = {
            "evidence_id", "evidence_revision_id", "book_revision_id",
            "source_path", "chapter", "start_line", "end_line", "quote",
        }
        for citation in citations:
            self.assertEqual(set(citation), expected_keys)
            self.assertEqual(citation["evidence_id"], citation["evidence_revision_id"])
            self.assertEqual(citation["book_revision_id"], self.book_revision_id)
            evidence_id = citation["evidence_revision_id"]
            self.assertIn(evidence_id, self.evidence_quotes)
            self.assertEqual(citation["quote"], self.evidence_quotes[evidence_id])
            start_line, end_line = self.evidence_spans[evidence_id]
            self.assertEqual(citation["start_line"], start_line)
            self.assertEqual(citation["end_line"], end_line)
            self.assertEqual(citation["source_path"], "raw/chapters/01-example.md")
            self.assertEqual(citation["chapter"], 1)

    def test_canonical_passthrough_citations_cover_the_full_evidence_bundle(self):
        turn_id, job = self.create_job("请总结这一章的核心论点")
        with patch("server.chat_worker.generate_structured_response") as mocked:
            process_job(self.conn, job, self.root, self.settings, "worker-a", self.database)
        mocked.assert_not_called()

        row = self.conn.execute(
            "SELECT status, answer_mode, citations_json FROM chat_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["answer_mode"], "canonical_passthrough")
        citations = json.loads(row["citations_json"])
        cited_ids = {item["evidence_revision_id"] for item in citations}
        self.assertEqual(cited_ids, {self.evr1, self.evr2})
        for citation in citations:
            self.assertEqual(citation["book_revision_id"], self.book_revision_id)


if __name__ == "__main__":
    unittest.main()
