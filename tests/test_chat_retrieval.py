import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.chat_retrieval import (
    _evidence_records,
    _raw_valid,
    _wiki_evidence_records,
    retrieve_chat_context,
    validate_answer,
    ChatRetrievalError,
)
from onebookwiki.chunking import chunk_text
from onebookwiki.index import LocalIndex


class ChatRetrievalTest(unittest.TestCase):
    def make_book(self, root: Path) -> dict:
        raw = root / "raw" / "chapters" / "01-evidence.md"
        raw.parent.mkdir(parents=True)
        raw.write_text("# Evidence\n\n> Chapter: 1\n\nGrounded claims require original evidence.\n", encoding="utf-8")
        chunks = chunk_text(raw.read_text(encoding="utf-8"), "raw/chapters/01-evidence.md", 1)
        index = LocalIndex(root)
        index.update(raw.relative_to(root), 1, chunks)
        chunk = index.load()[0]
        evidence = {
            "C1E1": {
                "evidence_id": "C1E1",
                "chunk_id": chunk["chunk_id"],
                "source_path": chunk["source_path"],
                "chapter": 1,
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "quote": "Grounded claims require original evidence.",
                "locator": chunk["locator"],
            }
        }
        wiki = root / "wiki"
        wiki.mkdir()
        (wiki / "evidence.json").write_text(json.dumps({"evidence": evidence}), encoding="utf-8")
        return chunk

    def test_valid_chunk_maps_to_rendered_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk = self.make_book(root)
            self.assertTrue(_raw_valid(root, chunk))
            records = _evidence_records(root, [chunk])
            self.assertEqual([item["evidence_id"] for item in records], ["C1E1"])

    def test_tampered_chunk_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk = self.make_book(root)
            chunk["content_hash"] = "0" * 64
            self.assertFalse(_raw_valid(root, chunk))
            self.assertEqual(_evidence_records(root, [chunk]), [])

    def test_tampered_raw_file_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk = self.make_book(root)
            raw = root / chunk["source_path"]
            raw.write_text(raw.read_text(encoding="utf-8") + "Changed after indexing.\n", encoding="utf-8")
            self.assertFalse(_raw_valid(root, chunk))

    def test_chunk_must_be_present_in_raw_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk = self.make_book(root)
            chunk["text"] = "Invented material"
            chunk["content_hash"] = hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
            self.assertFalse(_raw_valid(root, chunk))

    def test_answer_requires_only_selected_evidence_ids(self):
        self.assertEqual(validate_answer("Supported answer C1E1", {"C1E1"}), ["C1E1"])
        with self.assertRaisesRegex(ChatRetrievalError, "没有包含"):
            validate_answer("Unsupported answer", {"C1E1"})
        with self.assertRaisesRegex(ChatRetrievalError, "不可用"):
            validate_answer("Wrong citation C2E3", {"C1E1"})

    def test_epub_locator_is_display_metadata_not_a_citation_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk = self.make_book(root)
            evidence_path = root / "wiki" / "evidence.json"
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            payload["evidence"]["C1E1"]["display_label"] = "EPUB Ch. 1 · Spine 2 · index_split_000.html"
            evidence_path.write_text(json.dumps(payload), encoding="utf-8")
            records = _evidence_records(root, [chunk])
            self.assertEqual([item["evidence_id"] for item in records], ["C1E1"])
            with self.assertRaises(ChatRetrievalError) as raised:
                validate_answer("Answer EPUB Ch. 1 · Spine 2 · index_split_000.html", {"C1E1"})
            self.assertEqual(raised.exception.code, "answer_missing_citation")

    def test_evidence_key_and_embedded_id_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk = self.make_book(root)
            evidence_path = root / "wiki" / "evidence.json"
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            value = payload["evidence"]["C1E1"]
            value["evidence_id"] = "C1E2"
            evidence_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(_evidence_records(root, [chunk]), [])

    def test_wiki_evidence_records_resolve_inline_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_book(root)
            chunk = {"text": "全书主旨见 [C1E1] 的证据。", "source_kind": "wiki"}
            records = _wiki_evidence_records(root, [chunk])
            self.assertEqual([item["evidence_id"] for item in records], ["C1E1"])

    def test_wiki_evidence_records_reject_unknown_or_tampered_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_book(root)
            unknown = _wiki_evidence_records(root, [{"text": "见 C9E9。", "source_kind": "wiki"}])
            self.assertEqual(unknown, [])
            raw = root / "raw" / "chapters" / "01-evidence.md"
            raw.write_text(raw.read_text(encoding="utf-8").replace("Grounded claims require original evidence.", "This line no longer matches the recorded quote."), encoding="utf-8")
            tampered = _wiki_evidence_records(root, [{"text": "见 C1E1。", "source_kind": "wiki"}])
            self.assertEqual(tampered, [])

    def test_chat_context_answers_from_wiki_evidence_without_raw_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_book(root)
            wiki = root / "wiki"
            (wiki / "book.md").write_text(
                "# Book\n\n## Overview\n\n这本书讨论证据基础的写作方法。 [C1E1]\n",
                encoding="utf-8",
            )
            result = retrieve_chat_context(root, "这本书的主要内容是什么", backend="lexical", retrieval="lexical")
            self.assertIn("C1E1", result["allowed_evidence_ids"])
            self.assertIn("ALLOWED EVIDENCE IDS", result["prompt"])
            self.assertIn("C1E1", result["prompt"])

    def test_raw_only_context_exposes_its_own_evidence_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_book(root)
            result = retrieve_chat_context(root, "Grounded claims require original evidence", backend="lexical", retrieval="lexical")
            self.assertEqual(result["allowed_evidence_ids"], ["C1E1"])
            self.assertIn("[C1E1]", result["context"])
            self.assertEqual(validate_answer("Original evidence is required. C1E1", set(result["allowed_evidence_ids"])), ["C1E1"])


if __name__ == "__main__":
    unittest.main()
