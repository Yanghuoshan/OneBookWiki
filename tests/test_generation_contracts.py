import unittest

from onebookwiki.generation_contracts import (
    ContractError,
    collect_prompt_evidence_ids,
    strict_json_object,
    validate_book_payload,
    validate_chapter_payload,
    validate_rollup_payload,
)


class GenerationContractsTest(unittest.TestCase):
    evidence_id = "evr-" + "1" * 32
    other_evidence_id = "evr-" + "2" * 32
    quote = "They were handed over to the boatman."

    @classmethod
    def evidence(cls, evidence_id=None, quote=None):
        return {
            evidence_id or cls.evidence_id: {
                "evidence_revision_id": evidence_id or cls.evidence_id,
                "quote": quote or cls.quote,
                "source_line_start": 5,
                "source_line_end": 5,
            }
        }

    @classmethod
    def statement(cls, evidence_id=None, *, support_type="positive", quote=None, chapter=5):
        evidence_id = evidence_id or cls.evidence_id
        return {
            "draft_id": "claim-one",
            "canonical_key": "chapter.5.claim",
            "kind": "factual",
            "subject": "The boat",
            "truth_condition": "The boat marks a boundary space.",
            "scope": {
                "documentary_scope": "reading_unit",
                "target_type": "chapter",
                "chapter": chapter,
            },
            "abstraction": "concrete_detail",
            "qualification": None,
            "confidence": "high",
            "supports": [{
                "evidence_revision_id": evidence_id,
                "support_type": support_type,
                "quote": quote or cls.quote,
                "span_map": {"source_line_start": 5, "source_line_end": 5},
            }],
        }

    @classmethod
    def chapter(cls, evidence_id=None, **kwargs):
        return {
            "statements": [cls.statement(evidence_id, **kwargs)],
            "compositions": [{
                "draft_id": "chapter-summary",
                "canonical_key": "chapter.5.summary",
                "kind": "chapter_summary",
                "members": [{"member_type": "statement", "draft_id": "claim-one", "role": "support"}],
                "rendering": {"text": "A grounded chapter summary.", "span_map": {}},
            }],
        }

    @classmethod
    def rollup(cls, evidence_id=None):
        return {
            "statements": [cls.statement(evidence_id)],
            "compositions": [{
                "draft_id": "rollup-summary",
                "canonical_key": "rollup.5.summary",
                "kind": "rollup",
                "members": [{"member_type": "statement", "draft_id": "claim-one", "role": "support"}],
                "rendering": {"text": "A grounded rollup.", "span_map": {}},
            }],
        }

    @classmethod
    def book(cls, evidence_id=None):
        value = cls.statement(evidence_id, chapter=None)
        value["draft_id"] = "book-claim"
        value["canonical_key"] = "book.claim"
        value["scope"] = {"documentary_scope": "book", "target_type": "book", "chapter": None}
        return {
            "statements": [value],
            "compositions": [{
                "draft_id": "book-overview",
                "canonical_key": "book.overview",
                "kind": "overview",
                "members": [{"member_type": "statement", "draft_id": "book-claim", "role": "support"}],
                "rendering": {"text": "A grounded book overview.", "span_map": {}},
            }],
        }

    def test_strict_json_rejects_duplicate_and_trailing_content(self):
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            strict_json_object('{"summary":"a","summary":"b"}')
        with self.assertRaisesRegex(ContractError, "trailing content"):
            strict_json_object('{"summary":"a"}\nextra')

    def test_chapter_accepts_canonical_id_and_exact_quotation(self):
        ids = validate_chapter_payload(
            self.chapter(), chapter=5, evidence=self.evidence()
        )
        self.assertEqual(ids, {self.evidence_id})

    def test_chapter_rejects_legacy_id_locator_and_unknown_id(self):
        for evidence_id in ("E1", "EPUB Ch. 5 · Spine 6 · index_split_004.html", "C5E999"):
            with self.subTest(evidence_id=evidence_id), self.assertRaises(ContractError):
                validate_chapter_payload(
                    self.chapter(evidence_id), chapter=5, evidence=self.evidence()
                )

    def test_chapter_rejects_quote_not_found_in_cited_revision(self):
        payload = self.chapter(quote="A quote not present in the fixed body.")
        with self.assertRaisesRegex(ContractError, "exactly match"):
            validate_chapter_payload(payload, chapter=5, evidence=self.evidence())

    def test_positive_evidence_closure_is_required(self):
        with self.assertRaisesRegex(ContractError, "no positive evidence closure"):
            validate_chapter_payload(
                self.chapter(support_type="negative"), chapter=5, evidence=self.evidence()
            )

    def test_cross_revision_reference_is_unavailable(self):
        with self.assertRaisesRegex(ContractError, "unavailable evidence revision"):
            validate_chapter_payload(
                self.chapter(self.other_evidence_id), chapter=5, evidence=self.evidence()
            )

    def test_rollup_rejects_unknown_and_empty_citations(self):
        payload = self.rollup(self.other_evidence_id)
        with self.assertRaisesRegex(ContractError, "unavailable evidence revision"):
            validate_rollup_payload(
                payload,
                allowed_ids={self.evidence_id},
                evidence=self.evidence(),
            )
        payload["statements"][0]["supports"] = []
        with self.assertRaisesRegex(ContractError, "at least one"):
            validate_rollup_payload(
                payload,
                allowed_ids={self.evidence_id},
                evidence=self.evidence(),
            )

    def test_prompt_evidence_ids_are_canonical_and_ignore_locators(self):
        ids = collect_prompt_evidence_ids([
            {"evidence": [{"evidence_revision_id": self.evidence_id}]},
            {"evidence": [{"evidence_revision_id": "C6E2", "locator": "not identity"}]},
            {"evidence": [{"evidence_revision_id": self.other_evidence_id}]},
        ])
        self.assertEqual(ids, {self.evidence_id, self.other_evidence_id})

    def test_book_requires_strict_v2_stage_shape(self):
        payload = self.book()
        self.assertEqual(
            validate_book_payload(
                payload,
                allowed_ids={self.evidence_id},
                evidence=self.evidence(),
            ),
            {self.evidence_id},
        )
        payload["chapter_summaries"] = {}
        with self.assertRaisesRegex(ContractError, "invalid keys"):
            validate_book_payload(
                payload,
                allowed_ids={self.evidence_id},
                evidence=self.evidence(),
            )


if __name__ == "__main__":
    unittest.main()
