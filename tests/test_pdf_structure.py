import json
import tempfile
import unittest
from pathlib import Path

from onebookwiki.chunking import chunk_text
from onebookwiki.generation import GenerationError, GenerationOptions, generate_chapters
from onebookwiki.importers import import_pdf
from onebookwiki.index import LocalIndex
from onebookwiki.source_structure import PageBlock, PdfPage, SourceUnit, analyse_pdf, recognise_book_title, split_unit_into_parts


class PdfStructureTest(unittest.TestCase):
    def test_explicit_title_overrides_content_and_filename(self):
        pages = [PdfPage(1, "内容书名\n作者 甲", (PageBlock("内容书名", size=24),))]
        result = analyse_pdf(pages, Path("filename-title.pdf"), explicit_title="手动书名")
        self.assertEqual(result.title.value, "手动书名")
        self.assertEqual(result.title.method, "explicit")

    def test_filename_beats_damaged_title_page_fragments(self):
        pages = [
            PdfPage(1, "PHENOME'NOLOGY\nOF SPIRIT\nTRANSLATED BY A.V. MILLER", (
                PageBlock("PHENOME'NOLOGY", size=30),
                PageBlock("OF SPIRIT", size=30),
                PageBlock("TRANSLATED BY A.V. MILLER", size=16),
            )),
        ]
        title = recognise_book_title(pages, Path("Phenomenology of Spirit - Hegel.pdf"))
        self.assertEqual(title.value, "Phenomenology of Spirit")
        self.assertEqual(title.method, "filename")
        self.assertTrue(all("quality" in candidate or candidate["method"] == "filename" for candidate in title.candidates))

    def test_bookmarks_preserve_nested_outline_and_leaf_reading_units(self):
        pages = [PdfPage(index, f"第 {index} 页") for index in range(1, 8)]
        bookmarks = [
            {"level": 1, "title": "第一部", "page": 2},
            {"level": 2, "title": "第一章", "page": 2},
            {"level": 2, "title": "第二章", "page": 4},
            {"level": 1, "title": "第二部", "page": 6},
            {"level": 2, "title": "第三章", "page": 6},
        ]
        result = analyse_pdf(pages, Path("bookmarked.pdf"), bookmarks=bookmarks)
        self.assertEqual(result.method, "bookmarks")
        self.assertEqual([unit.title for unit in result.units], ["前置材料", "第一章", "第二章", "第三章"])
        self.assertEqual([node.title for node in result.outline], ["前置材料", "第一部", "第二部"])
        self.assertEqual([node.title for node in result.outline[1].children], ["第一章", "第二章"])
        self.assertEqual(result.outline[1].children[0].unit_ids, ["unit-02"])
        self.assertEqual(result.outline[2].children[0].breadcrumb, ("第二部", "第三章"))

    def test_toc_maps_native_headings_to_reading_units(self):
        pages = [
            PdfPage(1, "示例书", (PageBlock("示例书", size=30),)),
            PdfPage(2, "目录\n前言 / 3\n第一章 开始 / 5\n第二章 继续 / 8"),
            PdfPage(3, "前言\n3\n这是前言。", (PageBlock("前言", y0=20, size=18),)),
            PdfPage(4, "准备材料\n4"),
            PdfPage(5, "第一章 开始\n5\n正文。", (PageBlock("第一章 开始", y0=18, size=20),)),
            PdfPage(6, "第一章 开始\n6\n更多正文。"),
            PdfPage(7, "更多正文\n7"),
            PdfPage(8, "第二章 继续\n8\n正文。", (PageBlock("第二章 继续", y0=18, size=20),)),
        ]
        result = analyse_pdf(pages, Path("example.pdf"), min_confidence=0.45)
        self.assertEqual(result.method, "toc")
        self.assertEqual([unit.title for unit in result.units], ["前置材料", "前言", "第一章 开始", "第二章 继续"])
        self.assertEqual(result.units[2].physical_page_start, 5)
        self.assertEqual(result.units[3].physical_page_start, 8)
        self.assertEqual(result.report["ocr"], "disabled")

    def test_unrecognised_pdf_uses_honest_range_label(self):
        result = analyse_pdf([PdfPage(1, ""), PdfPage(2, "")], Path("opaque.pdf"), pages_per_chapter=1)
        self.assertEqual(result.method, "ranges")
        self.assertEqual([unit.title for unit in result.units], ["未识别 PDF 页段：pp. 1-1", "未识别 PDF 页段：pp. 2-2"])

    def test_long_unit_splits_without_text_loss(self):
        pages = [
            PdfPage(1, "第一段。第二段。第三段。"),
            PdfPage(2, "第四段。第五段。第六段。"),
        ]
        unit = SourceUnit("unit-01", "第一章", "chapter", ("第一章",), 1, 2, 0.9)
        parts = split_unit_into_parts(unit, pages, max_unit_tokens=256)
        self.assertGreaterEqual(len(parts), 1)
        self.assertEqual("".join(fragment for part in parts for _, fragment in part.page_fragments), "第一段。第二段。第三段。第四段。第五段。第六段。")
        self.assertEqual([part.part for part in parts], list(range(1, len(parts) + 1)))
        self.assertTrue(all(part.part_count == len(parts) for part in parts))

    def test_import_persists_semantic_metadata_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source = Path(tmp) / "book.pdf"
            source.write_bytes(b"fixture")
            imported = import_pdf(
                source,
                root,
                title="示例书",
                pages=["目录\n第一章 开始 / 2\n第二章 继续 / 3", "第一章 开始\n内容", "第二章 继续\n内容"],
                structure_min_confidence=0.4,
                max_unit_tokens=256,
            )
            self.assertGreaterEqual(len(imported), 2)
            source_metadata = json.loads((root / ".onebookwiki" / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(source_metadata["schema_version"], 3)
            self.assertEqual(source_metadata["title"], "示例书")
            self.assertTrue(source_metadata["source_structure"]["units"])
            self.assertTrue((root / ".onebookwiki" / "structure-report.json").is_file())
            raw = imported[0].read_text(encoding="utf-8")
            self.assertTrue(raw.startswith("# "))
            self.assertNotIn("# Chapter", raw)
            self.assertIn("> Source Unit:", raw)

    def test_force_reimport_removes_stale_raw_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source = Path(tmp) / "book.pdf"
            source.write_bytes(b"fixture")
            import_pdf(source, root, title="示例书", pages=["第一章\n内容", "第二章\n内容"], chapter_count=2)
            stale = root / "raw" / "chapters" / "99-stale.md"
            stale.write_text("# stale\n", encoding="utf-8")
            imported = import_pdf(source, root, title="示例书", pages=["第一章\n更新内容"], force=True)
            self.assertEqual(len(imported), 1)
            self.assertFalse(stale.exists())
            self.assertEqual(len(list((root / "raw" / "chapters").glob("*.md"))), 1)

    def test_generation_rejects_an_import_part_over_its_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw" / "chapters" / "01-unit.md"
            raw.parent.mkdir(parents=True)
            raw.write_text("# Unit\n\n> Chapter: 1\n\n" + "证据。" * 500, encoding="utf-8")
            index = LocalIndex(root)
            index.update(raw.relative_to(root), 1, chunk_text(raw.read_text(encoding="utf-8"), raw.relative_to(root).as_posix(), 1, target_tokens=5000, overlap_tokens=0, max_tokens=5000))
            with self.assertRaisesRegex(GenerationError, "re-import with a smaller --max-unit-tokens"):
                generate_chapters(root, GenerationOptions(dry_run=True, max_input_tokens=256))


if __name__ == "__main__":
    unittest.main()
