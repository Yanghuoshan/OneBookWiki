import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from onebookwiki.evidence_registry import register_project
from onebookwiki.knowledge_store import GroundedKnowledgeStore, KnowledgeStoreError, positive_evidence_closure
from server.database import create_placeholder, init_db, publish_registry_snapshot


class KnowledgeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        chapter = self.root / "raw" / "chapters" / "01-example.md"
        chapter.parent.mkdir(parents=True)
        chapter.write_text("# Example\n\n> Chapter: 1\n\nBody evidence.\n", encoding="utf-8")
        self.snapshot = register_project(self.root)
        self.conn = init_db(str(self.root / "onebookwiki.db"))
        self.book_id = create_placeholder(self.conn, "Example")
        publish_registry_snapshot(self.conn, self.book_id, self.snapshot)
        self.store = GroundedKnowledgeStore(self.conn, self.root, self.book_id, self.snapshot.book_revision_id)
        self.evidence_id, self.evidence = next(iter(self.store.evidence.items()))
        self.quote = self.evidence["payload"]["quote"]
        self.lines = {
            "source_line_start": self.evidence["source_line_start"],
            "source_line_end": self.evidence["source_line_end"],
        }

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def statement(self, support_type="positive"):
        return {
            "draft_id": "statement-one",
            "canonical_key": "example.body",
            "kind": "factual",
            "subject": "Example",
            "truth_condition": "The example contains body evidence.",
            "scope": {"documentary_scope": "reading_unit", "target_type": "chapter", "chapter": 1},
            "abstraction": "concrete_detail",
            "qualification": None,
            "confidence": "high",
            "supports": [{
                "evidence_revision_id": self.evidence_id,
                "support_type": support_type,
                "quote": self.quote,
                "span_map": self.lines,
            }],
        }

    def test_publish_materializes_revisions_and_positive_closure(self):
        statement, composition = self.store.publish(
            [self.statement()],
            [{
                "draft_id": "chapter-summary",
                "canonical_key": "chapter.1.summary",
                "kind": "chapter_summary",
                "members": [{"member_type": "statement", "draft_id": "statement-one", "role": "support"}],
                "rendering": {"text": "The example contains body evidence.", "span_map": {}},
            }],
        )
        self.assertEqual(len(statement), 1)
        self.assertEqual(len(composition), 1)
        revision_id = statement[0].statement_revision_id
        self.assertTrue(revision_id.startswith("str-"))
        self.assertEqual(positive_evidence_closure(self.conn, revision_id), {self.evidence_id})
        member = self.conn.execute(
            "SELECT statement_revision_id FROM composition_members WHERE composition_revision_id = ?",
            (composition[0].composition_revision_id,),
        ).fetchone()
        self.assertEqual(member[0], revision_id)
        self.assertEqual(self.conn.execute("SELECT status FROM book_revisions WHERE id = ?", (self.snapshot.book_revision_id,)).fetchone()[0], "healthy")

    def test_negative_only_support_fails_closed(self):
        with self.assertRaises(KnowledgeStoreError):
            self.store.publish([self.statement("negative")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM statement_revisions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM book_revisions WHERE id = ?", (self.snapshot.book_revision_id,)).fetchone()[0], "staging")

    def test_quote_tampering_fails_before_publication(self):
        draft = self.statement()
        draft["supports"][0]["quote"] = "not the body"
        with self.assertRaises(KnowledgeStoreError):
            self.store.publish([draft])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM statement_revisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
