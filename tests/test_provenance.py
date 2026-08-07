import json
import tempfile
import unittest
from pathlib import Path

from onebookwiki.chunking import chunk_text, locator_for_range
from onebookwiki.citations import display_label
from onebookwiki.importers import import_pdf
from onebookwiki.models import BookSynthesis, ChapterInterpretation, Claim, EvidenceRef
from onebookwiki.rendering import render_book


class ProvenanceTest(unittest.TestCase):
    def test_pdf_locator_uses_one_based_page_headings_and_ranges(self):
        text = """# Chapter 1: Sample

> Format: PDF
> Pages: 4-6

## Page 4

First page text.

## Page 5

Second page text.

## Page 6

Third page text.
"""
        self.assertEqual(
            locator_for_range(text, 7, 7, 1),
            {"format": "PDF", "physical_page_start": 4, "physical_page_end": 4},
        )
        self.assertEqual(
            locator_for_range(text, 7, 11, 1),
            {"format": "PDF", "physical_page_start": 4, "physical_page_end": 5},
        )

    def test_pdf_import_records_chapter_page_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source = Path(tmp) / "source.pdf"
            source.write_bytes(b"fixture PDF source")
            import_pdf(source, root, title="Sample", pages_per_chapter=2, pages=["one", "two", "three"])
            source = json.loads((root / ".onebookwiki" / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(source["locator_policy"], "pdf-physical-page-1-based")
            self.assertEqual(source["chapters"], [
                {"number": 1, "raw_path": "raw/chapters/01-sample.md", "physical_page_start": 1, "physical_page_end": 2},
                {"number": 2, "raw_path": "raw/chapters/02-sample.md", "physical_page_start": 3, "physical_page_end": 3},
            ])

    def test_rendered_citations_use_readable_pdf_labels(self):
        ref = EvidenceRef(
            evidence_id="C1E1",
            chunk_id="0001-0001",
            source_path="raw/chapters/01-sample.md",
            chapter=1,
            start_line=7,
            end_line=9,
            locator={"format": "PDF", "physical_page_start": 4, "physical_page_end": 5},
        )
        chapter = ChapterInterpretation(
            chapter=1,
            title="Sample",
            executive_summary="A summary [C1E1]",
            claims=[Claim("A claim", ("C1E1",))],
            evidence=[ref],
        )
        book = BookSynthesis(title="Sample", themes=[Claim("A theme", ("C1E1",))])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / ref.source_path
            raw.parent.mkdir(parents=True)
            raw.write_text("# Chapter 1: Sample\n\n> Format: PDF\n\n## Page 4\n\nSource text.\n", encoding="utf-8")
            render_book(book, [chapter], root)
            rendered = (root / "wiki" / "chapters" / "01-sample.md").read_text(encoding="utf-8")
            evidence = json.loads((root / "wiki" / "evidence.json").read_text(encoding="utf-8"))
            self.assertIn("PDF pp. 4-5", rendered)
            self.assertNotIn("`C1E1`", rendered)
            self.assertEqual(evidence["evidence"]["C1E1"]["display_label"], "PDF pp. 4-5")

    def test_epub_label_never_uses_pdf_page(self):
        ref = EvidenceRef(
            evidence_id="C2E1",
            chunk_id="0002-0001",
            source_path="raw/chapters/02-book.md",
            chapter=2,
            start_line=1,
            end_line=2,
            locator={"format": "EPUB", "chapter": 2, "spine_index": 3, "href": "OEBPS/two.xhtml", "fragment": "start"},
        )
        self.assertEqual(display_label(ref), "EPUB Ch. 2 · Spine 3 · OEBPS/two.xhtml#start")

    def test_chunking_preserves_pdf_locator(self):
        text = """# Chapter 1: Sample

> Format: PDF
> Pages: 1-2

## Page 1

Evidence on page one.

## Page 2

Evidence on page two.
"""
        chunks = chunk_text(text, "raw/chapters/01-sample.md", 1, target_tokens=12, overlap_tokens=0, max_tokens=30)
        self.assertTrue(any(chunk.locator.get("format") == "PDF" for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
