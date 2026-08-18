import unittest

from onebookwiki.prompts import book_prompt, chapter_prompt, rollup_prompt


class PromptTest(unittest.TestCase):
    def test_book_prompt_requires_chapter_summaries_mapping(self):
        prompt = book_prompt([], [], "Test book")
        self.assertIn('"chapter_summaries": {}', prompt)
        self.assertIn("MUST be a JSON object/map", prompt)
        self.assertIn("never an array", prompt)
        self.assertIn("ALLOWED EVIDENCE IDS", prompt)

    def test_chapter_prompt_uses_canonical_ids_and_rejects_locator_identity(self):
        prompt = chapter_prompt(5, "第一章", [{"chapter": 5, "chunk_id": "x", "text": "source"}])
        self.assertIn('"evidence_ids": [\n        "C5E1"', prompt)
        self.assertIn("source_path, locator, spine, href", prompt)
        self.assertNotIn('"evidence_ids": ["E1"]', prompt)

    def test_rollup_prompt_has_exact_schema_and_allowlist(self):
        prompt = rollup_prompt([{
            "chapter": 5,
            "evidence": [{"evidence_ids": ["C5E1"]}],
            "evidence_level": "claims",
        }])
        self.assertIn('"summary": "string"', prompt)
        self.assertIn('"C5E1"', prompt)
        self.assertIn("no extra keys", prompt)


if __name__ == "__main__":
    unittest.main()
