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
    def test_book3_shared_epub_locators_do_not_collapse_canonical_identity(self):
        project = Path(__file__).resolve().parent.parent
        book = project / "books" / "3"
        source = json.loads((book / ".onebookwiki" / "source.json").read_text(encoding="utf-8"))
        evidence = json.loads((book / "wiki" / "evidence.json").read_text(encoding="utf-8"))["evidence"]
        outline = {item["id"]: item for item in source["source_structure"]["outline"]}
        units = {int(item["chapter"]): item for item in source["source_structure"]["units"]}

        fool_ship = outline["epub-outline-002"]
        self.assertEqual(fool_ship["pageIds"], [f"chapter-{number:02d}" for number in range(5, 9)])
        self.assertEqual(len(set(fool_ship["unitIds"])), 4)
        self.assertEqual(
            [units[number]["source_unit_id"] for number in range(5, 9)],
            fool_ship["unitIds"],
        )
        self.assertEqual(
            {(units[number]["locator"]["spine_index"], units[number]["locator"]["href"]) for number in range(5, 9)},
            {(6, "index_split_004.html")},
        )
        self.assertEqual({units[number]["part"] for number in range(5, 9)}, {1, 2, 3, 4})
        self.assertTrue(all(units[number]["part_count"] == 4 for number in range(5, 9)))
        self.assertEqual({evidence[f"C{number}E1"]["chapter"] for number in range(5, 9)}, {5, 6, 7, 8})
        self.assertEqual(len({evidence[f"C{number}E1"]["evidence_id"] for number in range(5, 9)}), 4)

        asylum = outline["epub-outline-010"]
        self.assertEqual(asylum["pageIds"], [f"chapter-{number:02d}" for number in range(29, 33)])
        self.assertEqual(len(set(asylum["unitIds"])), 4)
        self.assertEqual(
            {(units[number]["locator"]["spine_index"], units[number]["locator"]["href"]) for number in range(29, 33)},
            {(14, "index_split_012.html")},
        )
        canonical = {"C29E36", "C29E37", "C30E6", "C31E39", "C31E56", "C32E17"}
        self.assertTrue(canonical.issubset(evidence))
        self.assertEqual({evidence[item]["evidence_id"] for item in canonical}, canonical)

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
            self.assertEqual(source["schema_version"], 3)
            chapters = source["source_structure"]["units"]
            self.assertEqual([(item["chapter"], item["raw_path"], item["physical_page_start"], item["physical_page_end"]) for item in chapters], [
                (1, "raw/chapters/01-pdf-pp-1-2.md", 1, 2),
                (2, "raw/chapters/02-pdf-pp-3-3.md", 3, 3),
            ])
            self.assertTrue(all(item["source_unit_id"] for item in chapters))

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

    def test_evidence_index_keeps_complete_line_range_and_falls_back_for_invalid_source(self):
        valid_ref = EvidenceRef(
            evidence_id="C1E1",
            chunk_id="0001-0001",
            source_path="raw/chapters/01-sample.md",
            chapter=1,
            start_line=1,
            end_line=3,
        )
        invalid_ref = EvidenceRef(
            evidence_id="C1E2",
            chunk_id="0001-0002",
            source_path="raw/chapters/missing.md",
            chapter=1,
            start_line=1,
            end_line=2,
        )
        chapter = ChapterInterpretation(chapter=1, title="Sample", evidence=[valid_ref, invalid_ref])
        long_line = "x" * 900
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / valid_ref.source_path
            raw.parent.mkdir(parents=True)
            raw.write_text(f"first line\n{long_line}\nlast line\n", encoding="utf-8")
            render_book(BookSynthesis(title="Sample"), [chapter], root)
            published = json.loads((root / "wiki" / "evidence.json").read_text(encoding="utf-8"))
            artifact = json.loads((root / ".onebookwiki" / "artifacts" / "evidence.json").read_text(encoding="utf-8"))

        self.assertEqual(published, artifact)
        self.assertEqual(published["schema_version"], 2)
        valid = published["evidence"]["C1E1"]
        self.assertEqual(valid["excerpt"], f"first line\n{long_line}\nlast line")
        self.assertEqual(valid["excerpt_start_line"], 1)
        self.assertEqual(valid["excerpt_end_line"], 3)
        self.assertFalse(valid["excerpt_truncated"])
        invalid = published["evidence"]["C1E2"]
        self.assertEqual(invalid["excerpt"], "")
        self.assertFalse(invalid["excerpt_truncated"])
        self.assertNotIn("excerpt_start_line", invalid)
        self.assertNotIn("excerpt_end_line", invalid)

    def test_rendered_structure_keeps_source_outline_page_ids_valid(self):
        chapter = ChapterInterpretation(
            chapter=1,
            title="第一章 开始",
            source_unit_id="unit-01",
            source_title="第一章 开始",
            source_type="chapter",
            breadcrumb=["第一部", "第一章 开始"],
            physical_page_start=5,
            physical_page_end=8,
            executive_summary="摘要",
        )
        book = BookSynthesis(title="示例书")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / ".onebookwiki" / "source.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({"source_structure": {"outline": [{
                "id": "unit-01", "title": "第一章 开始", "kind": "chapter",
                "pageIds": ["chapter-01", "chapter-99"], "children": [],
            }]}}, ensure_ascii=False), encoding="utf-8")
            render_book(book, [chapter], root)
            structure = json.loads((root / "wiki" / "structure.json").read_text(encoding="utf-8"))
            self.assertEqual(structure["sourceOutline"][0]["pageIds"], ["chapter-01"])
            self.assertEqual(structure["pages"][2]["sourceTitle"], "第一章 开始")

    def test_rendered_epub_structure_and_evidence_keep_spine_location(self):
        ref = EvidenceRef(
            evidence_id="C1E1",
            chunk_id="0001-0001",
            source_path="raw/chapters/01-introduction.md",
            chapter=1,
            start_line=1,
            end_line=2,
            locator={"format": "EPUB", "chapter": 1, "spine_index": 3, "href": "OEBPS/one.xhtml", "fragment": "start"},
        )
        chapter = ChapterInterpretation(
            chapter=1,
            title="Introduction",
            source_unit_id="epub-unit-01",
            source_title="Introduction",
            source_type="chapter",
            breadcrumb=["Part One", "Introduction"],
            spine="chapter-one",
            spine_index=3,
            href="OEBPS/one.xhtml",
            fragment="start",
            executive_summary="Summary",
            evidence=[ref],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / ref.source_path
            raw.parent.mkdir(parents=True)
            raw.write_text("# Introduction\n\n> Format: EPUB\n> Spine: chapter-one\n> Spine Index: 3\n> Href: OEBPS/one.xhtml\n> Fragment: start\n", encoding="utf-8")
            render_book(BookSynthesis(title="EPUB Book"), [chapter], root)
            structure = json.loads((root / "wiki" / "structure.json").read_text(encoding="utf-8"))
            page = next(item for item in structure["pages"] if item["id"] == "chapter-01")
            self.assertEqual(page["spineIndex"], 3)
            self.assertEqual(page["href"], "OEBPS/one.xhtml")
            evidence = json.loads((root / "wiki" / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["evidence"]["C1E1"]["spine_index"], 3)
            self.assertEqual(evidence["evidence"]["C1E1"]["href"], "OEBPS/one.xhtml")

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
