import tempfile
import unittest
from pathlib import Path

from onebookwiki.coverage import CoverageError, KnowledgeRetriever, evaluate_coverage
from onebookwiki.evidence_registry import register_project
from onebookwiki.knowledge_store import GroundedKnowledgeStore
from onebookwiki.query_analyzer import analyze_query
from server.database import create_placeholder, init_db, publish_registry_snapshot


class CoverageTest(unittest.TestCase):
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
        self.conn = init_db(str(self.root / "onebookwiki.db"))
        self.book_id = create_placeholder(self.conn, "Example")
        publish_registry_snapshot(self.conn, self.book_id, self.snapshot)
        store = GroundedKnowledgeStore(self.conn, self.root, self.book_id, self.snapshot.book_revision_id)
        by_line: dict[int, tuple[str, dict]] = {
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

        statements, compositions = store.publish(
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
        self.statement_revision_ids = {item.draft_id: item.statement_revision_id for item in statements}
        self.composition_revision_id = compositions[0].composition_revision_id
        self.retriever = KnowledgeRetriever(self.conn, self.book_id, self.snapshot.book_revision_id)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_unhealthy_or_unknown_revision_is_rejected(self):
        with self.assertRaises(CoverageError):
            KnowledgeRetriever(self.conn, self.book_id, "book-" + "0" * 32)

    def test_summary_question_selects_the_chapter_composition(self):
        analysis = analyze_query("请总结这一章的核心论点")
        retrieval = self.retriever.retrieve(analysis)
        self.assertTrue(retrieval.compositions)
        self.assertEqual(retrieval.compositions[0].composition_revision_id, self.composition_revision_id)
        coverage = evaluate_coverage(analysis, retrieval)
        self.assertEqual(coverage.answer_mode, "canonical_passthrough")
        self.assertTrue(coverage.canonical_complete)
        self.assertIn(self.composition_revision_id, coverage.selected_composition_revision_ids)

    def test_explain_question_falls_back_to_statement_knowledge(self):
        analysis = analyze_query("Explain why grounded claims require original evidence in this chapter")
        retrieval = self.retriever.retrieve(analysis)
        coverage = evaluate_coverage(analysis, retrieval)
        self.assertEqual(coverage.answer_mode, "knowledge_synthesis")
        self.assertFalse(coverage.canonical_complete)
        self.assertIn(self.statement_revision_ids["stmt-one"], coverage.selected_statement_revision_ids)

    def test_exact_wording_question_forces_raw_synthesis(self):
        analysis = analyze_query('原文怎么说“Grounded claims require original evidence.”')
        self.assertTrue(analysis.exact_wording)
        retrieval = self.retriever.retrieve(analysis)
        coverage = evaluate_coverage(analysis, retrieval)
        self.assertEqual(coverage.answer_mode, "raw_synthesis")
        self.assertIn(self.evr1, coverage.selected_evidence_revision_ids)
        self.assertNotIn(self.evr2, coverage.selected_evidence_revision_ids)

    def test_unrelated_question_is_insufficient(self):
        analysis = analyze_query("量子物理中的黑洞蒸发速率是多少")
        retrieval = self.retriever.retrieve(analysis)
        coverage = evaluate_coverage(analysis, retrieval)
        self.assertFalse(coverage.sufficient)
        self.assertIn("insufficient_pinned_evidence", coverage.reasons)


if __name__ == "__main__":
    unittest.main()
