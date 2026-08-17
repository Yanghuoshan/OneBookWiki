import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.book_navigation import BookCatalog
from onebookwiki.wiki_vector_index import (
    CHAPTER_INTERPRETATION,
    WikiVectorIndex,
    WikiVectorIndexError,
)


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
    def make_book(self, root: Path) -> None:
        raw_dir = root / "raw" / "chapters"
        raw_dir.mkdir(parents=True)
        (raw_dir / "01.md").write_text("Raw chapter one", encoding="utf-8")
        (raw_dir / "02.md").write_text("Raw chapter two", encoding="utf-8")
        metadata = {
            "title": "Test Book",
            "format": "TXT",
            "source_structure": {
                "outline": [{"id": "unit-a", "title": "Unit A", "kind": "chapter", "breadcrumb": ["Unit A"], "pageIds": ["chapter-01", "chapter-02"], "children": []}],
                "units": [
                    {"chapter": 1, "source_unit_id": "unit-a", "title": "Unit A part one", "source_title": "Unit A", "kind": "chapter", "breadcrumb": ["Unit A"], "part": 1, "part_count": 2, "raw_path": "raw/chapters/01.md"},
                    {"chapter": 2, "source_unit_id": "unit-a", "title": "Unit A part two", "source_title": "Unit A", "kind": "chapter", "breadcrumb": ["Unit A"], "part": 2, "part_count": 2, "raw_path": "raw/chapters/02.md"},
                ],
            },
        }
        wiki = root / "wiki"
        (wiki / "chapters").mkdir(parents=True)
        (wiki / "themes").mkdir()
        (wiki / "book.md").write_text("# Test Book\n\nBook navigation text.", encoding="utf-8")
        (wiki / "index.md").write_text("# Reading map\n\nUnit A.", encoding="utf-8")
        (wiki / "chapters" / "one.md").write_text("# Unit A part one\n\nSemantic chapter interpretation one.", encoding="utf-8")
        (wiki / "chapters" / "two.md").write_text("# Unit A part two\n\nSemantic chapter interpretation two.", encoding="utf-8")
        (wiki / "themes" / "attention.md").write_text("# Attention\n\nCross chapter theme.", encoding="utf-8")
        (root / ".onebookwiki").mkdir(exist_ok=True)
        (root / ".onebookwiki" / "source.json").write_text(json.dumps(metadata), encoding="utf-8")
        structure = {
            "title": "Test Book",
            "description": "A bounded test book.",
            "sourceOutline": metadata["source_structure"]["outline"],
            "pages": [
                {"id": "book", "title": "Test Book", "path": "book.md", "kind": "book", "relatedPages": ["index"]},
                {"id": "index", "title": "Reading map", "path": "index.md", "kind": "index", "relatedPages": ["book"]},
                {"id": "chapter-01", "title": "Unit A part one", "path": "chapters/one.md", "kind": "reading_unit", "chapter": 1, "sourceUnitId": "unit-a", "part": 1, "partCount": 2, "breadcrumb": ["Unit A"], "rawSources": ["raw/chapters/01.md"], "relatedPages": []},
                {"id": "chapter-02", "title": "Unit A part two", "path": "chapters/two.md", "kind": "reading_unit", "chapter": 2, "sourceUnitId": "unit-a", "part": 2, "partCount": 2, "breadcrumb": ["Unit A"], "rawSources": ["raw/chapters/02.md"], "relatedPages": []},
                {"id": "theme-attention", "title": "Attention", "path": "themes/attention.md", "kind": "theme", "evidenceIds": ["C1E1", "C2E1"], "relatedPages": []},
            ],
        }
        evidence = {
            "evidence": {
                "C1E1": {"evidence_id": "C1E1", "chapter": 1},
                "C2E1": {"evidence_id": "C2E1", "chapter": 2},
            }
        }
        (wiki / "structure.json").write_text(json.dumps(structure), encoding="utf-8")
        (wiki / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")

    def test_chapter_files_are_independently_indexed_and_source_unit_is_aggregated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_book(root)
            index = WikiVectorIndex(root, FakeEmbedder())
            index.build()
            health = index.health()
            self.assertTrue(health["healthy"])
            self.assertEqual(health["chapter_files_expected"], 2)
            self.assertEqual(health["chapter_files_indexed"], 2)
            results = index.search(CHAPTER_INTERPRETATION, "interpretation", source_unit_ids=["unit-a"])
            self.assertEqual({item["page_id"] for item in results}, {"chapter-01", "chapter-02"})
            self.assertTrue(all(item["page_kind"] == "reading_unit" for item in results))
            catalog = BookCatalog(root, index)
            unit = catalog.open_source_unit("unit-a")
            self.assertEqual(unit["chapters"], [1, 2])
            self.assertEqual(unit["part_count"], 2)

    def test_metadata_change_rebuilds_the_affected_chapter_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_book(root)
            index = WikiVectorIndex(root, FakeEmbedder())
            first = {item.layer: item for item in index.build()}
            self.assertEqual(first[CHAPTER_INTERPRETATION].changed_documents, 2)
            second = {item.layer: item for item in index.build()}
            self.assertEqual(second[CHAPTER_INTERPRETATION].changed_documents, 0)
            structure_path = root / "wiki" / "structure.json"
            structure = json.loads(structure_path.read_text(encoding="utf-8"))
            for page in structure["pages"]:
                if page.get("id") == "chapter-01":
                    page["breadcrumb"] = ["Changed route"]
            structure_path.write_text(json.dumps(structure), encoding="utf-8")
            changed = {item.layer: item for item in index.build()}
            self.assertEqual(changed[CHAPTER_INTERPRETATION].changed_documents, 1)

    def test_unregistered_chapter_file_fails_coverage_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_book(root)
            (root / "wiki" / "chapters" / "unregistered.md").write_text("# Missing metadata", encoding="utf-8")
            with self.assertRaisesRegex(WikiVectorIndexError, "does not exactly cover"):
                WikiVectorIndex(root, FakeEmbedder()).build()


if __name__ == "__main__":
    unittest.main()
