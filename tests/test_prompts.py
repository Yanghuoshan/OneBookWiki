import unittest

from onebookwiki.prompts import book_prompt, chapter_prompt, rollup_prompt


class PromptTest(unittest.TestCase):
    evidence_id = "evr-" + "1" * 32
    other_evidence_id = "evr-" + "2" * 32

    def test_chapter_prompt_uses_canonical_ids_and_rejects_locator_identity(self):
        prompt = chapter_prompt(
            5,
            "第一章",
            [],
            evidence_records=[{
                "evidence_revision_id": self.evidence_id,
                "chapter": 5,
                "source_path": "raw/chapters/05-example.md",
                "start_line": 10,
                "end_line": 10,
                "locator": {"format": "markdown"},
                "quote": "source",
            }],
        )
        self.assertIn(f'"{self.evidence_id}"', prompt)
        self.assertIn("source_path, locator, spine, href", prompt)
        self.assertIn("evidence_revision_id is the canonical identity, not a display locator", prompt)
        self.assertNotIn("C5E1", prompt)

    def test_rollup_prompt_has_exact_schema_and_allowlist(self):
        prompt = rollup_prompt(
            [{"chapter": 5, "evidence": [{"evidence_revision_id": self.evidence_id}]}],
            evidence_records=[{
                "evidence_revision_id": self.evidence_id,
                "chapter": 5,
                "quote": "source",
            }],
        )
        self.assertIn('"statements"', prompt)
        self.assertIn('"compositions"', prompt)
        self.assertIn(f'"{self.evidence_id}"', prompt)
        self.assertIn("no extra keys", prompt)
        self.assertNotIn("C5E1", prompt)

    def test_book_prompt_requires_exact_schema_and_allowlist(self):
        prompt = book_prompt(
            [],
            [],
            "Test book",
            evidence_records=[{
                "evidence_revision_id": self.evidence_id,
                "chapter": 1,
                "quote": "source",
            }],
        )
        self.assertIn('"statements"', prompt)
        self.assertIn('"compositions"', prompt)
        self.assertIn(f'"{self.evidence_id}"', prompt)
        self.assertIn("ALLOWED EVIDENCE IDS", prompt)
        self.assertIn("BOOK TITLE: Test book", prompt)


if __name__ == "__main__":
    unittest.main()
