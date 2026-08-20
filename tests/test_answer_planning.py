import tempfile
import unittest
from pathlib import Path

from onebookwiki.answer_planning import (
    AnswerPlanError,
    build_answer_plan,
    build_citation_snapshot,
    load_answer_plan,
    load_citation_snapshot,
    persist_answer_plan,
    persist_citation_snapshot,
    validate_answer_payload,
    verify_exact_quote,
)
from onebookwiki.evidence_registry import register_project
from onebookwiki.knowledge_store import GroundedKnowledgeStore
from onebookwiki.query_analyzer import analyze_query
from server.database import create_placeholder, init_db, publish_registry_snapshot


class AnswerPlanningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        chapter = self.root / "raw" / "chapters" / "01-example.md"
        chapter.parent.mkdir(parents=True)
        chapter.write_text(
            "# Example\n\n> Chapter: 1\n\nGrounded claims require original evidence.\n",
            encoding="utf-8",
        )
        self.snapshot = register_project(self.root)
        self.conn = init_db(str(self.root / "onebookwiki.db"))
        self.book_id = create_placeholder(self.conn, "Example")
        publish_registry_snapshot(self.conn, self.book_id, self.snapshot)
        store = GroundedKnowledgeStore(self.conn, self.root, self.book_id, self.snapshot.book_revision_id)
        self.evidence_id, self.evidence = next(iter(store.evidence.items()))
        store.publish([{
            "draft_id": "stmt-one",
            "canonical_key": "example.claim1",
            "kind": "factual",
            "subject": "Example",
            "truth_condition": "Grounded claims require original evidence.",
            "scope": {"documentary_scope": "reading_unit", "target_type": "chapter", "chapter": 1},
            "abstraction": "concrete_detail",
            "qualification": None,
            "confidence": "high",
            "supports": [{
                "evidence_revision_id": self.evidence_id,
                "support_type": "positive",
                "quote": self.evidence["payload"]["quote"],
                "span_map": {
                    "source_line_start": self.evidence["source_line_start"],
                    "source_line_end": self.evidence["source_line_end"],
                },
            }],
        }])
        self.book_revision_id = self.snapshot.book_revision_id
        self.policy = {"version": "answer-plan-v1"}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_build_plan_pins_evidence_bundle_and_snapshot(self):
        analysis = analyze_query("Explain why grounded claims require original evidence")
        plan, coverage = build_answer_plan(
            self.conn, self.root, self.book_id, self.book_revision_id, analysis,
            policy_snapshot=self.policy,
        )
        self.assertEqual(coverage.answer_mode, "knowledge_synthesis")
        self.assertTrue(plan.answer_plan_id.startswith("aplan-"))
        self.assertIn(self.evidence_id, plan.evidence_bundle.evidence_revision_ids)
        self.assertEqual(plan.evidence_bundle.quotes[self.evidence_id], self.evidence["payload"]["quote"])

        persist_answer_plan(self.conn, plan)
        reloaded = load_answer_plan(self.conn, self.root, self.book_id, plan.answer_plan_id)
        self.assertEqual(reloaded, plan)

        snapshot_id = persist_citation_snapshot(self.conn, plan)
        snapshot = load_citation_snapshot(self.conn, plan.answer_plan_id)
        self.assertEqual(snapshot, build_citation_snapshot(plan))
        self.assertTrue(snapshot_id.startswith("csnap-"))

    def test_insufficient_evidence_raises(self):
        analysis = analyze_query("这本书对黑洞热力学有什么看法")
        with self.assertRaises(AnswerPlanError):
            build_answer_plan(
                self.conn, self.root, self.book_id, self.book_revision_id, analysis,
                policy_snapshot=self.policy,
            )

    def test_segment_must_cite_only_pinned_nodes(self):
        analysis = analyze_query("Explain why grounded claims require original evidence")
        plan, _ = build_answer_plan(
            self.conn, self.root, self.book_id, self.book_revision_id, analysis,
            policy_snapshot=self.policy,
        )
        good = {
            "segments": [{
                "segment_id": "seg-1",
                "text": "Claims must be traceable to source text.",
                "statement_revision_ids": list(plan.statement_revision_ids),
                "evidence_revision_ids": [self.evidence_id],
            }],
        }
        normalized = validate_answer_payload(plan, good)
        self.assertEqual(normalized[0]["segment_id"], "seg-1")
        self.assertTrue(verify_exact_quote(plan, self.evidence_id, self.evidence["payload"]["quote"]))
        self.assertFalse(verify_exact_quote(plan, self.evidence_id, "tampered quote"))

        bad = {
            "segments": [{
                "segment_id": "seg-1",
                "text": "Unsupported claim.",
                "statement_revision_ids": [],
                "evidence_revision_ids": ["evr-" + "0" * 32],
            }],
        }
        with self.assertRaises(AnswerPlanError):
            validate_answer_payload(plan, bad)

    def test_out_of_order_segment_ids_are_rejected(self):
        analysis = analyze_query("Explain why grounded claims require original evidence")
        plan, _ = build_answer_plan(
            self.conn, self.root, self.book_id, self.book_revision_id, analysis,
            policy_snapshot=self.policy,
        )
        payload = {
            "segments": [{
                "segment_id": "seg-2",
                "text": "Out of order.",
                "statement_revision_ids": [],
                "evidence_revision_ids": [self.evidence_id],
            }],
        }
        with self.assertRaises(AnswerPlanError):
            validate_answer_payload(plan, payload)


if __name__ == "__main__":
    unittest.main()
