import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onebookwiki.chunking import chunk_text
from onebookwiki.generation import GenerationError, GenerationOptions, _parse_json_response, generate_chapters, synthesize_book
from onebookwiki.index import LocalIndex
from onebookwiki.providers import GenerationResponse


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.valid = {
            "purpose": "Explain attention.",
            "executive_summary": "Attention organizes inquiry.",
            "core_thesis": "Questions direct attention.",
            "claims": [{"text": "Questions direct attention.", "evidence_ids": ["C1E1"]}],
        }

    def make_index(self, root: Path, chapters: int = 1) -> None:
        index = LocalIndex(root)
        for number in range(1, chapters + 1):
            chapter = root / "raw" / "chapters" / f"{number:02d}-attention.md"
            chapter.parent.mkdir(parents=True, exist_ok=True)
            chapter.write_text(f"# Chapter {number}: Attention\n\n> Chapter: {number}\n\nQuestions direct attention.\n", encoding="utf-8")
            index.update(chapter.relative_to(root), number, chunk_text(chapter.read_text(encoding="utf-8"), chapter.relative_to(root).as_posix(), number))

    def test_parser_accepts_strict_json(self):
        value, repaired, error = _parse_json_response(json.dumps(self.valid))
        self.assertEqual(value, self.valid)
        self.assertFalse(repaired)
        self.assertIsNone(error)

    def test_parser_repairs_missing_comma(self):
        text = '{"purpose":"Explain attention." "executive_summary":"Attention organizes inquiry."}'
        value, repaired, error = _parse_json_response(text)
        self.assertEqual(value["executive_summary"], "Attention organizes inquiry.")
        self.assertTrue(repaired)
        self.assertIn("invalid JSON", error or "")

    def test_parser_repairs_unescaped_quotes_inside_chinese_value(self):
        text = '{"purpose":"旨在阐明否认作为一种社会与政治现象，并将其与单纯的"否定"区分开来。","executive_summary":"燃烧的孩子"之梦建立结构性类比。"}'
        value, repaired, _ = _parse_json_response(text)
        self.assertEqual(value["purpose"], "旨在阐明否认作为一种社会与政治现象，并将其与单纯的\"否定\"区分开来。")
        self.assertTrue(repaired)

    def test_parser_rejects_unrecoverable_text(self):
        with self.assertRaisesRegex(GenerationError, "repair unsuccessful"):
            _parse_json_response("not a JSON response")

    def test_repaired_chapter_response_completes_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root)
            response = json.dumps(self.valid).replace(', "executive_summary"', ' "executive_summary"')
            with patch("onebookwiki.generation.generate_response", return_value=GenerationResponse(text=response, model="test")):
                store = generate_chapters(root, GenerationOptions(provider="test"))
            artifact = root / ".onebookwiki" / "artifacts" / "chapters" / "0001.json"
            self.assertTrue(artifact.is_file())
            node = store.data["nodes"]["chapter:1"]
            self.assertEqual(node["status"], "completed")
            self.assertTrue(node["repaired_json"])

    def test_generation_persists_source_unit_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root)
            metadata = {
                "title": "Source Book",
                "source_structure": {
                    "units": [{
                        "chapter": 1,
                        "source_unit_id": "unit-01",
                        "title": "第一章 开始",
                        "source_title": "第一章 开始",
                        "kind": "chapter",
                        "breadcrumb": ["第一部", "第一章 开始"],
                        "physical_page_start": 5,
                        "physical_page_end": 8,
                        "spine": "chapter-one",
                        "spine_index": 4,
                        "href": "OEBPS/chapter-one.xhtml",
                        "fragment": "opening",
                        "confidence": 0.91,
                        "part": 1,
                        "part_count": 2,
                    }]
                },
            }
            source = root / ".onebookwiki" / "source.json"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            with patch("onebookwiki.generation.generate_response", return_value=GenerationResponse(text=json.dumps(self.valid), model="test")):
                generate_chapters(root, GenerationOptions(provider="test"))
            artifact = json.loads((root / ".onebookwiki" / "artifacts" / "chapters" / "0001.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["title"], "第一章 开始")
            self.assertEqual(artifact["source_unit_id"], "unit-01")
            self.assertEqual(artifact["breadcrumb"], ["第一部", "第一章 开始"])
            self.assertEqual(artifact["physical_page_start"], 5)
            self.assertEqual(artifact["spine"], "chapter-one")
            self.assertEqual(artifact["spine_index"], 4)
            self.assertEqual(artifact["href"], "OEBPS/chapter-one.xhtml")
            self.assertEqual(artifact["fragment"], "opening")
            self.assertEqual(artifact["part_count"], 2)

    def test_rollup_accepts_evidence_from_its_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root, chapters=4)
            chapter_responses = []
            for number in range(1, 5):
                chapter_value = dict(self.valid)
                chapter_value["claims"] = [{"text": f"Chapter {number} claim", "evidence_ids": [f"C{number}E1"]}]
                chapter_responses.append(GenerationResponse(text=json.dumps(chapter_value), model="test"))
            with patch("onebookwiki.generation.generate_response", side_effect=chapter_responses):
                store = generate_chapters(root, GenerationOptions(provider="test"))
            rollup = {"summary": "Combined", "themes": [{"text": "Fourth chapter theme", "evidence_ids": ["C4E1"]}]}
            book = {"overview": "Overview"}
            with patch("onebookwiki.generation.generate_response", side_effect=[GenerationResponse(text=json.dumps(rollup), model="test"), GenerationResponse(text=json.dumps(book), model="test")]):
                store = synthesize_book(root, GenerationOptions(provider="test", run_id=store.run_id))
            self.assertEqual(store.data["nodes"]["rollup:1-4"]["status"], "completed")
            self.assertTrue((root / ".onebookwiki" / "artifacts" / "rollups" / "0001-0004.json").is_file())

    def test_rollup_strips_evidence_outside_its_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root, chapters=4)
            chapter_responses = []
            for number in range(1, 5):
                chapter_value = dict(self.valid)
                chapter_value["claims"] = [{"text": f"Chapter {number} claim", "evidence_ids": [f"C{number}E1"]}]
                chapter_responses.append(GenerationResponse(text=json.dumps(chapter_value), model="test"))
            with patch("onebookwiki.generation.generate_response", side_effect=chapter_responses):
                store = generate_chapters(root, GenerationOptions(provider="test"))
            # Evidence IDs outside the rollup group are silently stripped rather
            # than failing the entire rollup (rollup synthesis works from summaries).
            invalid = {"summary": "Combined", "themes": [{"text": "Unknown chapter", "evidence_ids": ["C5E1"]}]}
            book = {"overview": "Overview"}
            with patch("onebookwiki.generation.generate_response", side_effect=[GenerationResponse(text=json.dumps(invalid), model="test"), GenerationResponse(text=json.dumps(book), model="test")]):
                store = synthesize_book(root, GenerationOptions(provider="test", run_id=store.run_id))
            self.assertEqual(store.data["nodes"]["rollup:1-4"]["status"], "completed")
            artifact = json.loads((root / ".onebookwiki" / "artifacts" / "rollups" / "0001-0004.json").read_text(encoding="utf-8"))
            # C5E1 should have been stripped from the persisted artifact
            self.assertEqual(artifact["themes"][0]["evidence_ids"], [])

    def test_unrecoverable_response_marks_checkpoint_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root)
            with patch("onebookwiki.generation.generate_response", return_value=GenerationResponse(text="not a JSON response", model="test")):
                with self.assertRaisesRegex(GenerationError, "response length"):
                    generate_chapters(root, GenerationOptions(provider="test"))
            checkpoint = json.loads(next((root / ".onebookwiki" / "checkpoints").glob("run-*.json")).read_text(encoding="utf-8"))
            node = checkpoint["nodes"]["chapter:1"]
            self.assertEqual(node["status"], "failed")
            self.assertIn("preview", node["error"])


if __name__ == "__main__":
    unittest.main()
