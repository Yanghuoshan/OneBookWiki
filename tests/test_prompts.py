import unittest

from onebookwiki.prompts import book_prompt


class PromptTest(unittest.TestCase):
    def test_book_prompt_requires_chapter_summaries_mapping(self):
        prompt = book_prompt([], [], "Test book")
        self.assertIn('"chapter_summaries": {', prompt)
        self.assertIn("MUST be a JSON object/map", prompt)
        self.assertIn("Do not return chapter_summaries as an array or list", prompt)


if __name__ == "__main__":
    unittest.main()
