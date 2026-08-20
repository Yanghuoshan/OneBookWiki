import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.evidence_registry import register_project
from onebookwiki.knowledge_store import GroundedKnowledgeStore
from onebookwiki.wiki_projection import WikiProjectionError, load_grounded_wiki_projection, render_grounded_projection
from server.database import create_placeholder, init_db, publish_registry_snapshot


class GroundedWikiProjectionTest(unittest.TestCase):
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
        (self.root / ".onebookwiki").mkdir(exist_ok=True)
        (self.root / ".onebookwiki" / "source.json").write_text(json.dumps({
            "title": "Projection Book",
            "format": "TXT",
            "source_structure": {
                "outline": [{"id": "chapter-one", "title": "Example", "kind": "chapter", "breadcrumb": ["Example"], "children": []}],
                "units": [{
                    "chapter": 1, "source_unit_id": "chapter-one", "source_title": "Example",
                    "kind": "chapter", "breadcrumb": ["Example"], "raw_path": "raw/chapters/01-example.md",
                }],
            },
        }), encoding="utf-8")
        self.snapshot = register_project(self.root)
        self.conn = init_db(str(self.root / "onebookwiki.db"))
        self.book_id = create_placeholder(self.conn, "Projection Book")
        publish_registry_snapshot(self.conn, self.book_id, self.snapshot)
        store = GroundedKnowledgeStore(self.conn, self.root, self.book_id, self.snapshot.book_revision_id)
        evidence_id, evidence = sorted(store.evidence.items())[0]
        statement = {
            "draft_id": "claim-one", "canonical_key": "chapter.1.claim", "kind": "factual",
            "subject": "Example", "truth_condition": "Grounded claims require original evidence.",
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
        }
        compositions = [{
            "draft_id": "chapter-summary", "canonical_key": "chapter.1.summary", "kind": "chapter_summary",
            "members": [{"member_type": "statement", "draft_id": "claim-one", "role": "support"}],
            "rendering": {"text": "Trustworthy analysis depends on verifiable sourcing.", "span_map": {}},
        }, {
            "draft_id": "book-overview", "canonical_key": "book.overview", "kind": "overview",
            "members": [{"member_type": "composition", "draft_id": "chapter-summary", "role": "support"}],
            "rendering": {"text": "This book presents evidence-grounded reading.", "span_map": {}},
        }]
        store.publish([statement], compositions)
        self.evidence_id = evidence_id

        self._foreign_tmpdirs = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        for tmp in self._foreign_tmpdirs:
            tmp.cleanup()

    def _statement_revision_id(self, canonical_key: str) -> str:
        row = self.conn.execute(
            """SELECT sr.id FROM statement_revisions AS sr
               JOIN statement_identities AS si ON si.id = sr.statement_identity_id
               WHERE si.book_id = ? AND si.canonical_key = ? AND sr.status = 'healthy'""",
            (self.book_id, canonical_key),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row["id"])

    def _composition_revision_id(self, canonical_key: str) -> str:
        row = self.conn.execute(
            """SELECT cr.id FROM composition_revisions AS cr
               JOIN composition_identities AS ci ON ci.id = cr.composition_identity_id
               WHERE ci.book_id = ? AND ci.canonical_key = ? AND cr.status = 'healthy'""",
            (self.book_id, canonical_key),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row["id"])

    def _publish_foreign_revision(self) -> tuple[str, str]:
        """Publish an unrelated book/revision in the same database for cross-revision tests."""
        tmp2 = tempfile.TemporaryDirectory()
        self._foreign_tmpdirs.append(tmp2)
        root2 = Path(tmp2.name)
        chapter2 = root2 / "raw" / "chapters" / "01-other.md"
        chapter2.parent.mkdir(parents=True)
        chapter2.write_text(
            "# Other\n\n> Chapter: 1\n\n"
            "Independent evidence belongs to a different book revision.\n",
            encoding="utf-8",
        )
        snapshot2 = register_project(root2)
        book_id2 = create_placeholder(self.conn, "Foreign Book")
        publish_registry_snapshot(self.conn, book_id2, snapshot2)
        store2 = GroundedKnowledgeStore(self.conn, root2, book_id2, snapshot2.book_revision_id)
        evidence_id2, evidence2 = sorted(store2.evidence.items())[0]
        statement2 = {
            "draft_id": "foreign-claim", "canonical_key": "foreign.claim", "kind": "factual",
            "subject": "Other", "truth_condition": "Independent evidence belongs to a different book revision.",
            "scope": {"documentary_scope": "reading_unit", "target_type": "chapter", "chapter": 1},
            "abstraction": "concrete_detail", "qualification": None, "confidence": "high",
            "supports": [{
                "evidence_revision_id": evidence_id2, "support_type": "positive",
                "quote": evidence2["payload"]["quote"],
                "span_map": {
                    "source_line_start": evidence2["source_line_start"],
                    "source_line_end": evidence2["source_line_end"],
                },
            }],
        }
        composition2 = {
            "draft_id": "foreign-summary", "canonical_key": "foreign.summary", "kind": "chapter_summary",
            "members": [{"member_type": "statement", "draft_id": "foreign-claim", "role": "support"}],
            "rendering": {"text": "Foreign composition text.", "span_map": {}},
        }
        statements, compositions = store2.publish([statement2], [composition2])
        return statements[0].statement_revision_id, compositions[0].composition_revision_id

    def _assert_render_fails_closed(self, pattern: str) -> None:
        structure_path = self.root / "wiki" / "structure.json"
        baseline = structure_path.read_bytes() if structure_path.is_file() else None
        with self.assertRaisesRegex(WikiProjectionError, pattern):
            render_grounded_projection(self.conn, self.root, self.book_id)
        if baseline is None:
            self.assertFalse(structure_path.exists())
        else:
            self.assertEqual(structure_path.read_bytes(), baseline)

    def test_projection_and_static_render_use_immutable_revision_ids(self):
        projection = load_grounded_wiki_projection(self.conn, self.root, self.book_id)
        self.assertEqual(projection.book_revision_id, self.snapshot.book_revision_id)
        self.assertIn(self.evidence_id, projection.evidence)
        files = render_grounded_projection(self.conn, self.root, self.book_id)
        self.assertTrue(files)
        structure = json.loads((self.root / "wiki" / "structure.json").read_text(encoding="utf-8"))
        evidence = json.loads((self.root / "wiki" / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(structure["bookRevisionId"], self.snapshot.book_revision_id)
        self.assertEqual(evidence["bookRevisionId"], self.snapshot.book_revision_id)
        self.assertIn(self.evidence_id, evidence["evidence"])
        chapter_page = next(page for page in structure["pages"] if page["kind"] == "reading_unit")
        rendered = (self.root / "wiki" / chapter_page["path"]).read_text(encoding="utf-8")
        self.assertIn(f"onebookwiki://evidence/{self.evidence_id}", rendered)
        self.assertNotIn("C1E", rendered)

    def test_projection_fails_closed_when_active_revision_is_superseded(self):
        self.conn.execute("UPDATE book_revisions SET status = 'superseded' WHERE id = ?", (self.snapshot.book_revision_id,))
        self.conn.commit()
        with self.assertRaisesRegex(WikiProjectionError, "healthy active"):
            load_grounded_wiki_projection(self.conn, self.root, self.book_id)

    def test_projection_fails_closed_for_stale_expected_revision(self):
        with self.assertRaisesRegex(WikiProjectionError, "expected revision"):
            load_grounded_wiki_projection(
                self.conn, self.root, self.book_id, expected_revision_id="br-not-current"
            )

    def test_render_fails_closed_when_evidence_body_is_tampered_on_disk(self):
        render_grounded_projection(self.conn, self.root, self.book_id)
        body_path = self.root / ".onebookwiki" / "knowledge" / "evidence-bodies" / f"{self.evidence_id}.txt"
        body_path.write_text("Tampered evidence body text.", encoding="utf-8")
        self._assert_render_fails_closed(f"evidence revision is not healthy: {self.evidence_id}")

    def test_render_fails_closed_when_evidence_body_hash_is_tampered_in_database(self):
        render_grounded_projection(self.conn, self.root, self.book_id)
        self.conn.execute(
            "UPDATE evidence_revisions SET body_hash = ? WHERE id = ?",
            ("0" * 64, self.evidence_id),
        )
        self.conn.commit()
        self._assert_render_fails_closed(f"database evidence metadata mismatch: {self.evidence_id}")

    def test_render_fails_closed_when_evidence_locator_is_tampered_in_database(self):
        render_grounded_projection(self.conn, self.root, self.book_id)
        self.conn.execute(
            "UPDATE evidence_revisions SET locator_json = ? WHERE id = ?",
            (json.dumps({"format": "tampered"}), self.evidence_id),
        )
        self.conn.commit()
        self._assert_render_fails_closed(f"database evidence locator/body mismatch: {self.evidence_id}")

    def test_render_fails_closed_when_a_statement_loses_positive_evidence_closure(self):
        render_grounded_projection(self.conn, self.root, self.book_id)
        statement_id = self._statement_revision_id("chapter.1.claim")
        self.conn.execute(
            "UPDATE evidence_closure SET positive = 0 WHERE statement_revision_id = ?",
            (statement_id,),
        )
        self.conn.commit()
        self._assert_render_fails_closed(f"statement has invalid positive evidence closure: {statement_id}")

    def test_render_fails_closed_when_a_composition_references_a_cross_revision_statement(self):
        render_grounded_projection(self.conn, self.root, self.book_id)
        foreign_statement_id, _foreign_composition_id = self._publish_foreign_revision()
        composition_id = self._composition_revision_id("chapter.1.summary")
        self.conn.execute(
            "UPDATE composition_members SET statement_revision_id = ? "
            "WHERE composition_revision_id = ? AND member_type = 'statement'",
            (foreign_statement_id, composition_id),
        )
        self.conn.commit()
        self._assert_render_fails_closed(f"composition references a non-active statement: {composition_id}")

    def test_render_fails_closed_when_a_composition_references_a_dangling_child(self):
        render_grounded_projection(self.conn, self.root, self.book_id)
        _foreign_statement_id, foreign_composition_id = self._publish_foreign_revision()
        composition_id = self._composition_revision_id("book.overview")
        self.conn.execute(
            "UPDATE composition_members SET child_composition_revision_id = ? "
            "WHERE composition_revision_id = ? AND member_type = 'composition'",
            (foreign_composition_id, composition_id),
        )
        self.conn.commit()
        self._assert_render_fails_closed(f"composition references a non-active child: {composition_id}")


if __name__ == "__main__":
    unittest.main()
