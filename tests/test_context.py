import unittest

from onebookwiki.context import assemble_context
from onebookwiki.retrieval import Result


class ContextTest(unittest.TestCase):
    def test_zero_budget_returns_no_context(self):
        result = Result(1, {"chunk_id": "a", "chapter": 1, "text": "evidence"})
        context, selected = assemble_context([result], max_tokens=0)
        self.assertEqual((context, selected), ("", []))

    def test_deduplicates_and_limits_per_chapter(self):
        chunks = [
            {"chunk_id": "a", "content_hash": "a", "chapter": 1, "source_path": "one.md", "start_line": 1, "end_line": 2, "token_count": 5, "text": "alpha " * 5},
            {"chunk_id": "duplicate", "content_hash": "a", "chapter": 1, "source_path": "one.md", "start_line": 1, "end_line": 2, "token_count": 5, "text": "alpha " * 5},
            {"chunk_id": "b", "content_hash": "b", "chapter": 1, "source_path": "one.md", "start_line": 3, "end_line": 4, "token_count": 5, "text": "beta " * 5},
            {"chunk_id": "c", "content_hash": "c", "chapter": 2, "source_path": "two.md", "start_line": 1, "end_line": 2, "token_count": 5, "text": "gamma " * 5},
        ]
        context, selected = assemble_context([Result(4 - i, chunk) for i, chunk in enumerate(chunks)], max_tokens=10, max_per_chapter=1)
        self.assertEqual(len(selected), 2)
        self.assertEqual({item["chapter"] for item in selected}, {1, 2})
        self.assertIn("lines 1-2", context)


if __name__ == "__main__":
    unittest.main()
