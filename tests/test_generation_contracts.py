import json
import unittest

from onebookwiki.generation_contracts import (
    ContractError,
    strict_json_object,
    validate_book_payload,
    validate_chapter_payload,
    validate_rollup_payload,
    collect_prompt_evidence_ids,
)


class GenerationContractsTest(unittest.TestCase):
    def chapter(self, evidence_id="C5E1"):
        return {
            "purpose": "说明愚人船意象。",
            "executive_summary": "愚人船连接驱逐与边界空间。",
            "core_thesis": "航行意象表现社会排斥。",
            "argument_map": "从驱逐转向边界与文化想象。",
            "key_concepts": ["愚人船"],
            "observations": [{"text": "疯人被交给船夫。", "evidence_ids": [evidence_id]}],
            "quotations": [{"text": "他们很容易就被交给船夫。", "evidence_ids": [evidence_id]}],
            "relation_to_previous": "材料不足",
            "relation_to_following": "材料不足",
            "cross_chapter_connections": [],
            "open_questions": [],
            "review_questions": [],
            "claims": [{"text": "船构成一种边界空间。", "evidence_ids": [evidence_id], "confidence": "high"}],
        }

    def test_strict_json_rejects_duplicate_and_trailing_content(self):
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            strict_json_object('{"summary":"a","summary":"b"}')
        with self.assertRaisesRegex(ContractError, "trailing content"):
            strict_json_object('{"summary":"a"}\nextra')

    def test_chapter_accepts_canonical_id_and_exact_quotation(self):
        chunks = [{"text": "在许多城市，他们很容易就被交给船夫。"}]
        ids = validate_chapter_payload(self.chapter(), chapter=5, chunks=chunks)
        self.assertEqual(ids, {"C5E1"})

    def test_chapter_rejects_legacy_id_locator_and_unknown_id(self):
        chunks = [{"text": "他们很容易就被交给船夫。"}]
        for evidence_id in ("E1", "EPUB Ch. 5 · Spine 6 · index_split_004.html", "C5E999"):
            with self.subTest(evidence_id=evidence_id), self.assertRaises(ContractError):
                validate_chapter_payload(self.chapter(evidence_id), chapter=5, chunks=chunks)

    def test_chapter_rejects_quote_not_found_in_cited_chunk(self):
        payload = self.chapter()
        payload["quotations"][0]["text"] = "原文中不存在的引句"
        with self.assertRaisesRegex(ContractError, "not present"):
            validate_chapter_payload(payload, chapter=5, chunks=[{"text": "他们很容易就被交给船夫。"}])

    def test_rollup_rejects_unknown_and_empty_citations(self):
        payload = {
            "summary": "综合",
            "themes": [{"text": "主题", "evidence_ids": ["C8E1"]}],
            "concepts": [], "arguments": [], "tensions": [],
        }
        with self.assertRaisesRegex(ContractError, "unavailable evidence ID"):
            validate_rollup_payload(payload, allowed_ids={"C5E1"})
        payload["themes"][0]["evidence_ids"] = []
        with self.assertRaisesRegex(ContractError, "at least one"):
            validate_rollup_payload(payload, allowed_ids={"C5E1"})

    def test_prompt_evidence_ids_are_canonical_and_shape_neutral(self):
        ids = collect_prompt_evidence_ids([
            {"evidence": [{"evidence_ids": ["C5E1", "EPUB Ch. 5"]}]},
            {"evidence": [{"evidence_id": "C6E2", "locator": "not identity"}]},
        ])
        self.assertEqual(ids, {"C5E1", "C6E2"})

    def test_book_requires_exact_chapter_summary_map(self):
        payload = {
            "overview": "概览", "core_thesis": "核心", "argument_chain": "论证链",
            "themes": [], "concepts": [], "arguments": [],
            "terminology": [], "unresolved_questions": [],
            "chapter_summaries": {"5": "第一部分", "6": "第二部分"},
        }
        self.assertEqual(
            validate_book_payload(payload, allowed_ids={"C5E1", "C6E1"}, chapters={5, 6}),
            set(),
        )
        payload["chapter_summaries"] = ["第一部分", "第二部分"]
        with self.assertRaisesRegex(ContractError, "object/map"):
            validate_book_payload(payload, allowed_ids={"C5E1", "C6E1"}, chapters={5, 6})


if __name__ == "__main__":
    unittest.main()
