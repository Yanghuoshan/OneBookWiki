import json
import tempfile
import unittest
from pathlib import Path

from onebookwiki.chunking import chunk_text
from onebookwiki.generation import GenerationError, GenerationOptions, generate_chapters
from onebookwiki.importers import import_pdf
from onebookwiki.index import LocalIndex
from onebookwiki.pdf_postprocess import postprocess_pdf_pages
from onebookwiki.source_structure import PageBlock, PdfLine, PdfPage, PdfSpan, SourceUnit, analyse_pdf, parse_printed_toc, recognise_book_title, split_unit_into_parts


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

    def test_incomplete_bookmarks_fall_back_to_automatic_structure(self):
        pages = [PdfPage(number, "") for number in range(1, 11)]
        bookmarks = [
            {"level": 1, "title": "前言", "page": 1},
            {"level": 1, "title": "译序", "page": 3},
            {"level": 2, "title": "附录", "page": 9},
        ]
        result = analyse_pdf(pages, Path("incomplete-bookmarks.pdf"), bookmarks=bookmarks, mode="bookmarks", pages_per_chapter=10)
        self.assertEqual(result.method, "ranges")
        self.assertEqual([(unit.physical_page_start, unit.physical_page_end) for unit in result.units], [(1, 10)])
        bookmark_report = result.report["methods"][0]
        self.assertFalse(bookmark_report["accepted"])
        self.assertEqual(bookmark_report["rejection"], "incomplete_physical_page_coverage")
        self.assertEqual(bookmark_report["coverage"]["gaps"], [{"physical_page_start": 3, "physical_page_end": 8}])

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

    def test_printed_toc_parses_alternating_title_and_page_lines(self):
        pages = [PdfPage(
            1,
            "目录\n001\n引言\n013\n第一章 哲学与欲望\n049\n第二章 幸福考验\n061\n结语",
        )]
        entries = parse_printed_toc(pages)
        self.assertEqual(
            [(entry["title"], entry["printed_page"], entry["parse_style"]) for entry in entries],
            [
                ("引言", 1, "page_then_title"),
                ("第一章 哲学与欲望", 13, "page_then_title"),
                ("第二章 幸福考验", 49, "page_then_title"),
                ("结语", 61, "page_then_title"),
            ],
        )

    def test_content_headings_ignore_ocr_running_headers(self):
        pages = [
            PdfPage(1, "版权信息"),
            PdfPage(2, "第一章 哲学与欲望\n本章正文从这里开始，讨论哲学与幸福之间的关联。"),
            PdfPage(3, "第一章 哲学与欲望0]7\n这是一段延续的正文，并不是新的章节。"),
            PdfPage(4, "第二章 幸福考验\n本章正文从这里开始，讨论反哲学与真实幸福。"),
            PdfPage(5, "第二章 幸福考验053\n这是一段延续的正文，并不是新的章节。"),
            PdfPage(6, "结语\n最后总结全书的核心论点以及它的现实含义。"),
        ]
        result = analyse_pdf(pages, Path("content-only.pdf"), mode="headings")
        self.assertEqual(result.method, "headings")
        self.assertEqual(
            [(unit.title, unit.physical_page_start) for unit in result.units],
            [("前置材料", 1), ("第一章 哲学与欲望", 2), ("第二章 幸福考验", 4), ("结语", 6)],
        )
        diagnostics = result.report["content_boundaries"]
        self.assertEqual(diagnostics["running_headers"], {"第一章哲学与欲望": [3], "第二章幸福考验": [5]})
        self.assertEqual([candidate["page"] for candidate in diagnostics["rejected"]], [3, 5])

    def test_real_book_style_toc_and_content_boundaries(self):
        pages = [PdfPage(number, "扉页") for number in range(1, 136)]
        pages[6] = PdfPage(
            7,
            "目录\n001\n引言\n013\n第一章哲学与哲学的欲望\n049\n第二章幸福考验下的哲学与反哲学\n061\n第三章为了幸福，必须改变世界吗？\n085\n第四章哲学的目的地与情感\n133\n结语",
        )
        pages[8] = PdfPage(9, "引言\n本书致力于厘清哲学的任务，并在结尾处对个人设想加以描述。")
        pages[10] = PdfPage(11, "引 言 005\n引言接续讨论真正幸福与哲学任务的关系。")
        pages[17] = PdfPage(18, "第一章哲学与哲学的欲望\n第一章的正文从哲学欲望与反抗的关系开始。")
        pages[20] = PdfPage(21, "第一章哲学与哲学的欲望0]7\n这是第一章的延续正文而不是新的章节。")
        pages[51] = PdfPage(52, "第二章幸福考验下的哲学与反哲学\n第二章从反哲学的立场进入具体讨论。")
        pages[54] = PdfPage(55, "第二章幸福考验下的哲学与反哲学053\n这是第二章的延续正文而不是新的章节。")
        pages[62] = PdfPage(63, "第三章为了幸福，必须改变世界吗？\n第三章探讨改变世界与幸福之间的关系。")
        pages[85] = PdfPage(86, "第四章哲学的目的地与情感\n第四章讨论哲学与多种情感之间的联系。")
        pages[88] = PdfPage(89, "第四章哲学的目的地与情感_〇89\n这是第四章的延续正文而不是新的章节。")
        pages[131] = PdfPage(132, "结语\n最后总结本书观点，并重新回到真正生活的问题。")
        result = analyse_pdf(pages, Path("真实幸福的形而上学.pdf"), explicit_title="真实幸福的形而上学")
        self.assertEqual(result.method, "toc")
        self.assertEqual(
            [(unit.title, unit.physical_page_start, unit.physical_page_end) for unit in result.units],
            [
                ("前置材料", 1, 8),
                ("引言", 9, 17),
                ("第一章哲学与哲学的欲望", 18, 51),
                ("第二章幸福考验下的哲学与反哲学", 52, 62),
                ("第三章为了幸福,必须改变世界吗?", 63, 85),
                ("第四章哲学的目的地与情感", 86, 131),
                ("结语", 132, 135),
            ],
        )
        self.assertEqual(result.report["content_boundaries"]["running_headers"], {
            "引言": [11],
            "第一章哲学与哲学的欲望": [21],
            "第二章幸福考验下的哲学与反哲学": [55],
            "第四章哲学的目的地与情感": [89],
        })

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
            report = json.loads((root / ".onebookwiki" / "structure-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["ocr"], "disabled")
            self.assertEqual(report["postprocess"]["reason"], "layout-unavailable")
            self.assertEqual(source_metadata["source_processing"]["structure_view"], "native")
            raw = imported[0].read_text(encoding="utf-8")
            self.assertTrue(raw.startswith("# "))
            self.assertNotIn("# Chapter", raw)
            self.assertIn("> Source Unit:", raw)

    def test_processed_payload_keeps_native_outline_and_uses_synced_blocks(self):
        def layout_line(text, y0, size=10):
            return PdfLine(text, 20, y0, 260, y0 + size, size=size, spans=(PdfSpan(text, 20, y0, 260, y0 + size, size=size),))

        first = (layout_line("第一章 开始", 20, 20), layout_line("这 是 清 理 后 的 正 文。", 70))
        second = (layout_line("第二章 继续", 20, 20), layout_line("第 二 页 正 文。", 70))
        native = [
            PdfPage(1, "第一章 开始\n这 是 清 理 后 的 正 文。", (PageBlock("第一章 开始", y0=20, size=20),), height=1000, width=300, lines=first),
            PdfPage(2, "第二章 继续\n第 二 页 正 文。", (PageBlock("第二章 继续", y0=20, size=20),), height=1000, width=300, lines=second),
        ]
        outline = analyse_pdf(native, Path("sample.pdf"), mode="headings", min_confidence=0.1)
        processed = postprocess_pdf_pages(native).pages
        self.assertEqual([unit.title for unit in outline.units], ["第一章 开始", "第二章 继续"])
        self.assertEqual([unit.physical_page_start for unit in outline.units], [1, 2])
        self.assertIn("这是清理后的正文。", processed[0].text)
        self.assertTrue(processed[0].blocks)
        parts = split_unit_into_parts(outline.units[0], list(processed), max_unit_tokens=256)
        self.assertIn("这是清理后的正文。", "\n".join(fragment for part in parts for _, fragment in part.page_fragments))

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
