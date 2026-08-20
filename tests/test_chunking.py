import unittest

from onebookwiki.chunking import chunk_text, chunking_profile
from onebookwiki.markdown import extract_body


class ChunkingTest(unittest.TestCase):
    def test_chunks_track_source_lines_and_preserve_text(self):
        text = "# Source\n\n## First\n\nAlpha evidence appears here.\n\n## Second\n\nBeta evidence appears here.\n"
        chunks = chunk_text(text, "raw/chapters/01-source.md", 1, target_words=4, overlap_words=0)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0].source_path, "raw/chapters/01-source.md")
        self.assertEqual(chunks[0].chapter, 1)
        self.assertGreaterEqual(chunks[0].start_line, 1)
        self.assertGreaterEqual(chunks[-1].end_line, chunks[-1].start_line)
        self.assertIn("Alpha evidence", "\n".join(chunk.text for chunk in chunks))
        self.assertIn("Beta evidence", "\n".join(chunk.text for chunk in chunks))

    def test_body_extraction_excludes_title_and_metadata_with_physical_lines(self):
        text = (
            "# Example\r\n"
            "\r\n"
            "> Book: Example\r\n"
            "> Date: 2024-01-01\r\n"
            "> Chapter: 1\r\n"
            "\r\n"
            "## Evidence\r\n"
            "\r\n"
            "The body contains 2024-02-02 and 12,345.\r\n"
        )
        extraction = extract_body(text)
        self.assertEqual(extraction.title, "# Example")
        self.assertEqual(extraction.header, ("> Book: Example", "> Date: 2024-01-01", "> Chapter: 1"))
        self.assertEqual(extraction.body, "## Evidence\n\nThe body contains 2024-02-02 and 12,345.")
        self.assertEqual(extraction.source_line_map, (7, 8, 9))
        self.assertEqual(extraction.body_start_line, 7)
        self.assertEqual(extraction.body_end_line, 9)

        chunks = chunk_text(text, "raw/chapters/01-example.md", 1, target_words=20, overlap_words=0)
        combined = "\n".join(chunk.text for chunk in chunks)
        self.assertNotIn("# Example", combined)
        self.assertNotIn("Book: Example", combined)
        self.assertNotIn("2024-01-01", combined)
        self.assertIn("The body contains 2024-02-02 and 12,345.", combined)
        self.assertEqual((chunks[0].start_line, chunks[0].end_line), (7, 9))
        self.assertEqual(chunks[0].locator["source_line_start"], 7)
        self.assertEqual(chunks[0].locator["source_line_end"], 9)

    def test_body_canonicalization_is_stable_for_line_endings_and_nfc_only(self):
        composed = "# T\n\n正文：全角１２，café。\n"
        crlf = composed.replace("\n", "\r\n")
        self.assertEqual(extract_body(composed).body, extract_body(crlf).body)
        self.assertEqual(extract_body(composed).body, "正文：全角１２，café。")
        self.assertNotIn("正文:全角12", extract_body(composed).body)

        lf_chunks = chunk_text(composed, "raw/chapters/01.md", 1, target_words=20, overlap_words=0)
        crlf_chunks = chunk_text(crlf, "raw/chapters/01.md", 1, target_words=20, overlap_words=0)
        self.assertEqual([chunk.content_hash for chunk in lf_chunks], [chunk.content_hash for chunk in crlf_chunks])
        self.assertEqual([chunk.text for chunk in lf_chunks], [chunk.text for chunk in crlf_chunks])

    def test_default_profiles_use_smaller_budgets_for_both_languages(self):
        self.assertEqual(
            chunking_profile("中文内容。"),
            {
                "revision": "smaller-v1",
                "language": "cjk",
                "target_tokens": 150,
                "overlap_tokens": 25,
                "max_tokens": 200,
            },
        )
        self.assertEqual(
            chunking_profile("English content."),
            {
                "revision": "smaller-v1",
                "language": "latin",
                "target_tokens": 200,
                "overlap_tokens": 30,
                "max_tokens": 250,
            },
        )

    def test_explicit_token_budget_overrides_default_profile(self):
        self.assertEqual(
            chunking_profile("English content.", target_tokens=120, overlap_tokens=30, max_tokens=160),
            {
                "revision": "smaller-v1",
                "language": "latin",
                "target_tokens": 120,
                "overlap_tokens": 30,
                "max_tokens": 160,
            },
        )

    def test_chinese_uses_token_budget_without_spaces(self):
        sentences = "。".join(f"这是第{i}个句子，包含关于证据和阅读的内容" for i in range(80)) + "。"
        chunks = chunk_text(sentences, "raw/chapters/01-source.md", 1, target_tokens=60, overlap_tokens=10, max_tokens=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.token_count <= 80 for chunk in chunks))
        self.assertEqual("".join(chunk.text.replace(" ", "") for chunk in chunks).count("这是第"), 80)

    def test_long_chinese_sentence_has_hard_limit(self):
        text = "这是一个没有句号的长段落" * 500
        chunks = chunk_text(text, "raw/chapters/01-source.md", 1, target_tokens=40, overlap_tokens=0, max_tokens=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.token_count <= 50 for chunk in chunks))

    def test_chinese_overlap_repeats_tail_evidence(self):
        text = "第一句关于主题。第二句关于证据。第三句关于论点。第四句关于结论。"
        chunks = chunk_text(text, "raw/chapters/01-source.md", 1, target_tokens=14, overlap_tokens=10, max_tokens=30)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(any("证据" in chunk.text for chunk in chunks[1:]))


if __name__ == "__main__":
    unittest.main()
