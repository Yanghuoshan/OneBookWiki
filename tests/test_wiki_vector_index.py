import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.book_navigation import BookCatalog
from onebookwiki.evidence_registry import register_project
from onebookwiki.knowledge_store import GroundedKnowledgeStore
from onebookwiki.wiki_projection import render_grounded_projection
from onebookwiki.wiki_vector_index import (
    CHAPTER_INTERPRETATION,
    WikiVectorIndex,
    WikiVectorIndexError,
    build_wiki_vector_indexes,
)
from server.database import create_placeholder, init_db, publish_registry_snapshot


class FakeEmbedder:
    provider = "fake"

    def identity(self):
        return {"provider": "fake", "model": "fake-v1"}

    def embed(self, texts):
        return [self.embed_one(text) for text in texts]

    def embed_one(self, text):
        value = sum(ord(char) for char in text)
        return [float((value % 97) + 1), float((len(text) % 53) + 1)]


class WikiVectorIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        chapter = self.root / "raw" / "chapters" / "01-example.md"
        chapter.parent.mkdir(parents=True)
        chapter.write_text(
            "# Example\n\n> Chapter: 1\n\n"
            "Grounded claims require original evidence.\n",
            encoding="utf-8",
        )
        (self.root / ".onebookwiki").mkdir(exist_ok=True)
        (self.root / ".onebookwiki" / "source.json").write_text(json.dumps({
            "title": "Vector Book", "format": "TXT",
            "source_structure": {
                "outline": [{"id": "chapter-one", "title": "Example", "kind": "chapter", "breadcrumb": ["Example"], "children": []}],
                "units": [{"chapter": 1, "source_unit_id": "chapter-one", "source_title": "Example", "kind": "chapter", "breadcrumb": ["Example"]}],
            },
        }), encoding="utf-8")
        snapshot = register_project(self.root)
        self.conn = init_db(str(self.root / "onebookwiki.db"))
        self.book_id = create_placeholder(self.conn, "Vector Book")
        publish_registry_snapshot(self.conn, self.book_id, snapshot)
        self.revision_id = snapshot.book_revision_id
        store = GroundedKnowledgeStore(self.conn, self.root, self.book_id, self.revision_id)
        evidence_id, evidence = next(iter(store.evidence.items()))
        store.publish([{
            "draft_id": "claim-one", "canonical_key": "chapter.1.claim", "kind": "factual",
            "subject": "Example", "truth_condition": "Grounded claims require original evidence.",
            "scope": {"documentary_scope": "reading_unit", "target_type": "chapter", "chapter": 1},
            "abstraction": "concrete_detail", "qualification": None, "confidence": "high",
            "supports": [{
                "evidence_revision_id": evidence_id, "support_type": "positive", "quote": evidence["payload"]["quote"],
                "span_map": {"source_line_start": evidence["source_line_start"], "source_line_end": evidence["source_line_end"]},
            }],
        }], [{
            "draft_id": "summary", "canonical_key": "chapter.1.summary", "kind": "chapter_summary",
            "members": [{"member_type": "statement", "draft_id": "claim-one", "role": "support"}],
            "rendering": {"text": "A chapter rendering grounded in source evidence.", "span_map": {}},
        }])
        render_grounded_projection(self.conn, self.root, self.book_id)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_structured_projection_is_indexed_with_revision_metadata(self):
        first = {item.layer: item for item in build_wiki_vector_indexes(self.conn, self.root, self.book_id, FakeEmbedder())}
        self.assertEqual(first[CHAPTER_INTERPRETATION].changed_documents, 1)
        projection = __import__("onebookwiki.wiki_projection", fromlist=["load_grounded_wiki_projection"]).load_grounded_wiki_projection(
            self.conn, self.root, self.book_id
        )
        index = WikiVectorIndex(self.root, FakeEmbedder(), projection=projection)
        health = index.health()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["book_revision_id"], self.revision_id)
        results = index.search(CHAPTER_INTERPRETATION, "grounded evidence", source_unit_ids=["chapter-one"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["book_revision_id"], self.revision_id)
        self.assertTrue(results[0]["statement_revision_ids"])
        self.assertTrue(results[0]["evidence_revision_ids"])

    def test_stale_vector_manifest_fails_closed(self):
        build_wiki_vector_indexes(self.conn, self.root, self.book_id, FakeEmbedder())
        projection = __import__("onebookwiki.wiki_projection", fromlist=["load_grounded_wiki_projection"]).load_grounded_wiki_projection(
            self.conn, self.root, self.book_id
        )
        manifest_path = self.root / ".onebookwiki" / "retrieval" / "wiki-vectors.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["bookRevisionId"] = "br-stale"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(WikiVectorIndexError, "does not match"):
            WikiVectorIndex(self.root, FakeEmbedder(), projection=projection).search(
                CHAPTER_INTERPRETATION, "grounded"
            )

    def test_catalog_requires_healthy_grounded_projection(self):
        build_wiki_vector_indexes(self.conn, self.root, self.book_id, FakeEmbedder())
        projection = __import__("onebookwiki.wiki_projection", fromlist=["load_grounded_wiki_projection"]).load_grounded_wiki_projection(
            self.conn, self.root, self.book_id
        )
        catalog = BookCatalog(self.root, WikiVectorIndex(self.root, FakeEmbedder(), projection=projection))
        self.assertEqual(catalog.book_revision_id, self.revision_id)

    def test_build_fails_closed_for_a_stale_expected_revision(self):
        with self.assertRaisesRegex(WikiVectorIndexError, "expected revision"):
            build_wiki_vector_indexes(
                self.conn, self.root, self.book_id, FakeEmbedder(), expected_revision_id="br-not-current"
            )
        self.assertFalse((self.root / ".onebookwiki" / "retrieval" / "wiki-vectors.json").exists())

    def test_search_fails_closed_when_embedding_identity_changes(self):
        build_wiki_vector_indexes(self.conn, self.root, self.book_id, FakeEmbedder())
        projection = __import__("onebookwiki.wiki_projection", fromlist=["load_grounded_wiki_projection"]).load_grounded_wiki_projection(
            self.conn, self.root, self.book_id
        )

        class OtherEmbedder(FakeEmbedder):
            def identity(self):
                return {"provider": "fake", "model": "fake-v2"}

        with self.assertRaisesRegex(WikiVectorIndexError, "does not match the configured embedding model"):
            WikiVectorIndex(self.root, OtherEmbedder(), projection=projection).search(
                CHAPTER_INTERPRETATION, "grounded"
            )

    def test_indexed_text_contains_only_structured_statement_and_rendering_text(self):
        build_wiki_vector_indexes(self.conn, self.root, self.book_id, FakeEmbedder())
        manifest_path = self.root / ".onebookwiki" / "retrieval" / "wiki-vectors.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        chapter_chunks = manifest["layers"][CHAPTER_INTERPRETATION]["chunks"]
        self.assertTrue(chapter_chunks)
        combined_text = "\n".join(str(chunk["text"]) for chunk in chapter_chunks)
        self.assertIn("A chapter rendering grounded in source evidence.", combined_text)
        self.assertIn("Grounded claims require original evidence.", combined_text)
        self.assertNotIn("onebookwiki://evidence/", combined_text)
        self.assertNotIn("## Evidence", combined_text)
        self.assertNotIn("Grounded revision:", combined_text)


if __name__ == "__main__":
    unittest.main()
