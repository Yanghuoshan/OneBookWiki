import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onebookwiki.checkpoints import CheckpointStore
from onebookwiki.chunking import chunk_text
from onebookwiki.evidence_registry import register_project
from onebookwiki.generation import (
    GenerationError,
    GenerationOptions,
    _parse_json_response,
    _response,
    generate_chapters,
    synthesize_book,
)
from onebookwiki.index import LocalIndex
from onebookwiki.manifest import Manifest
from onebookwiki.providers import GenerationResponse


class GenerationTest(unittest.TestCase):
    def setUp(self):
        self.valid = self.payload_for_evidence("evr-" + "1" * 32, "Questions direct attention.", 1)

    @staticmethod
    def payload_for_evidence(evidence_id: str, quote: str, chapter: int | None, *, draft_prefix: str | None = None) -> dict:
        prefix = draft_prefix or (f"chapter-{chapter}" if chapter is not None else "book")
        scope = {
            "documentary_scope": "reading_unit" if chapter is not None else "book",
            "target_type": "chapter" if chapter is not None else "book",
            "chapter": chapter,
        }
        return {
            "statements": [{
                "draft_id": f"{prefix}-claim",
                "canonical_key": f"{prefix}.claim",
                "kind": "factual",
                "subject": "Attention",
                "truth_condition": "Questions direct attention.",
                "scope": scope,
                "abstraction": "concrete_detail",
                "qualification": None,
                "confidence": "high",
                "supports": [{
                    "evidence_revision_id": evidence_id,
                    "support_type": "positive",
                    "quote": quote,
                    "span_map": {"source_line_start": 5, "source_line_end": 5},
                }],
            }],
            "compositions": [{
                "draft_id": f"{prefix}-summary",
                "canonical_key": f"{prefix}.summary",
                "kind": "chapter_summary" if chapter is not None else "overview",
                "members": [{"member_type": "statement", "draft_id": f"{prefix}-claim", "role": "support"}],
                "rendering": {"text": "Attention organizes inquiry.", "span_map": {}},
            }],
        }

    @staticmethod
    def evidence_for(root: Path, chapter: int) -> dict:
        manifest = Manifest.load(root)
        values = [
            dict(item) for item in manifest.evidence_revisions.values()
            if int(item["chapter"]) == chapter
        ]
        values.sort(key=lambda item: int(item["source_line_start"]))
        return values[0]

    def make_index(self, root: Path, chapters: int = 1) -> None:
        index = LocalIndex(root)
        for number in range(1, chapters + 1):
            chapter = root / "raw" / "chapters" / f"{number:02d}-attention.md"
            chapter.parent.mkdir(parents=True, exist_ok=True)
            chapter.write_text(
                f"# Chapter {number}: Attention\n\n> Chapter: {number}\n\nQuestions direct attention.\n",
                encoding="utf-8",
            )
            index.update(
                chapter.relative_to(root), number,
                chunk_text(chapter.read_text(encoding="utf-8"), chapter.relative_to(root).as_posix(), number),
            )
        snapshot = register_project(root)
        manifest = Manifest.load(root)
        manifest.pin_registry(snapshot)
        manifest.save(root)

    def response_for(self, root: Path, chapter: int, *, prefix: str | None = None) -> GenerationResponse:
        evidence = self.evidence_for(root, chapter)
        return GenerationResponse(
            text=json.dumps(self.payload_for_evidence(
                evidence["evidence_revision_id"], evidence["quote"], chapter, draft_prefix=prefix,
            )),
            model="test",
        )

    def test_parser_accepts_strict_json(self):
        value, repaired, error = _parse_json_response(json.dumps(self.valid))
        self.assertEqual(value, self.valid)
        self.assertFalse(repaired)
        self.assertIsNone(error)

    def test_parser_repairs_missing_comma(self):
        text = '{"statements":[] "compositions":[]}'
        value, repaired, error = _parse_json_response(text)
        self.assertEqual(value, {"statements": [], "compositions": []})
        self.assertTrue(repaired)
        self.assertIn("invalid JSON", error or "")

    def test_parser_repairs_unescaped_quotes_inside_value(self):
        text = '{"text":"A phrase with an "embedded" quote."}'
        value, repaired, _ = _parse_json_response(text)
        self.assertEqual(value["text"], 'A phrase with an "embedded" quote.')
        self.assertTrue(repaired)

    def test_parser_rejects_unrecoverable_text(self):
        with self.assertRaisesRegex(GenerationError, "repair unsuccessful"):
            _parse_json_response("not a JSON response")

    def test_repaired_chapter_response_completes_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root)
            response = json.dumps(self.response_for(root, 1).text).replace("\\\"compositions\\\"", "\\\"compositions\\\"")
            # Remove the delimiter between the two strict stage fields.
            response = self.response_for(root, 1).text.replace(', "compositions"', ' "compositions"')
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
                "source_structure": {"units": [{
                    "chapter": 1, "source_unit_id": "unit-01", "title": "第一章 开始",
                    "source_title": "第一章 开始", "kind": "chapter", "breadcrumb": ["第一部", "第一章 开始"],
                    "physical_page_start": 5, "physical_page_end": 8, "spine": "chapter-one",
                    "spine_index": 4, "href": "OEBPS/chapter-one.xhtml", "fragment": "opening",
                    "confidence": 0.91, "part": 1, "part_count": 2,
                }]},
            }
            source = root / ".onebookwiki" / "source.json"
            source.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            with patch("onebookwiki.generation.generate_response", return_value=self.response_for(root, 1)):
                generate_chapters(root, GenerationOptions(provider="test"))
            artifact = json.loads((root / ".onebookwiki" / "artifacts" / "chapters" / "0001.json").read_text(encoding="utf-8"))
            context = artifact["provenance"]["context"]
            self.assertEqual(context["title"], "第一章 开始")
            self.assertEqual(context["source_unit_id"], "unit-01")
            self.assertEqual(context["breadcrumb"], ["第一部", "第一章 开始"])
            self.assertEqual(context["physical_page_start"], 5)
            self.assertEqual(context["spine"], "chapter-one")
            self.assertEqual(context["spine_index"], 4)
            self.assertEqual(context["href"], "OEBPS/chapter-one.xhtml")
            self.assertEqual(context["fragment"], "opening")
            self.assertEqual(context["part_count"], 2)

    def test_rollup_accepts_evidence_from_its_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root, chapters=4)
            chapter_responses = [self.response_for(root, number) for number in range(1, 5)]
            with patch("onebookwiki.generation.generate_response", side_effect=chapter_responses):
                store = generate_chapters(root, GenerationOptions(provider="test"))
            fourth = self.evidence_for(root, 4)
            rollup = self.payload_for_evidence(fourth["evidence_revision_id"], fourth["quote"], None, draft_prefix="rollup")
            rollup["statements"][0]["scope"] = {"documentary_scope": "rollup", "target_type": "book", "chapter": None}
            with patch("onebookwiki.generation.generate_response", side_effect=[GenerationResponse(text=json.dumps(rollup), model="test"), GenerationResponse(text=json.dumps(self.payload_for_evidence(fourth["evidence_revision_id"], fourth["quote"], None)), model="test")]):
                store = synthesize_book(root, GenerationOptions(provider="test", run_id=store.run_id))
            self.assertEqual(store.data["nodes"]["rollup:1-4"]["status"], "completed")
            self.assertTrue((root / ".onebookwiki" / "artifacts" / "rollups" / "0001-0004.json").is_file())

    def test_rollup_rejects_evidence_outside_its_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_index(root, chapters=4)
            with patch("onebookwiki.generation.generate_response", side_effect=[self.response_for(root, number) for number in range(1, 5)]):
                store = generate_chapters(root, GenerationOptions(provider="test"))
            fifth_id = "evr-" + "f" * 32
            invalid = self.payload_for_evidence(fifth_id, "Unknown evidence.", None, draft_prefix="rollup")
            invalid["statements"][0]["scope"] = {"documentary_scope": "rollup", "target_type": "book", "chapter": None}
            with patch("onebookwiki.generation.generate_response", return_value=GenerationResponse(text=json.dumps(invalid), model="test")):
                with self.assertRaisesRegex(GenerationError, "unavailable evidence revision"):
                    synthesize_book(root, GenerationOptions(provider="test", run_id=store.run_id))
            checkpoint = json.loads((root / ".onebookwiki" / "checkpoints" / f"run-{store.run_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["nodes"]["rollup:1-4"]["status"], "failed")
            self.assertFalse((root / ".onebookwiki" / "artifacts" / "rollups" / "0001-0004.json").is_file())

    def test_usage_accounting_failure_does_not_replay_successful_provider_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CheckpointStore(root, run_id="accounting")
            response = GenerationResponse(text=json.dumps(self.valid), model="test")
            with patch("onebookwiki.generation.generate_response", return_value=response) as provider, patch("onebookwiki.generation.append_usage", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(GenerationError, "usage accounting failed"):
                    _response(root, "prompt", GenerationOptions(provider="test", retries=2), store, "chapter:1", "chapter")
            provider.assert_called_once()
            node = store.data["nodes"]["chapter:1"]
            self.assertEqual(node["status"], "failed")
            self.assertEqual(node["failure_kind"], "accounting")
            self.assertTrue(node["provider_response_received"])

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
