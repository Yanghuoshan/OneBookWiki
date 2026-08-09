import json
import tempfile
import unittest
from pathlib import Path

from onebookwiki.chunking import chunk_text
from onebookwiki.generation import GenerationError, GenerationOptions, generate_chapters
from onebookwiki.importers import import_pdf
from onebookwiki.index import LocalIndex
from onebookwiki.pdf_manifest import validate_manifest
from onebookwiki.pdf_postprocess import postprocess_pdf_pages
from onebookwiki.source_structure import PageBlock, PdfLine, PdfPage, PdfSpan, SourceUnit, _page_label_value, _toc_pages, analyse_pdf, parse_printed_toc, recognise_book_title, split_unit_into_parts


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

    def test_toc_marker_normalises_chinese_spacing(self):
        pages = [PdfPage(1, "目　录\n第一章 开始 / 005")]
        self.assertEqual([page.number for page in _toc_pages(pages)], [1])
        self.assertEqual(
            [(entry["title"], entry["printed_page"]) for entry in parse_printed_toc(pages)],
            [("第一章 开始", 5)],
        )

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

    def test_printed_toc_parses_dense_dotted_parenthesized_rows(self):
        pages = [PdfPage(
            1,
            "目 录\n我怎样理解马克思主义...........................(1)\n工人阶级饱和的类性身份.....................(40)",
        )]
        entries = parse_printed_toc(pages)
        self.assertEqual(
            [(entry["title"], entry["printed_page"], entry["parse_style"]) for entry in entries],
            [
                ("我怎样理解马克思主义", 1, "dense_dotted_parenthesized"),
                ("工人阶级饱和的类性身份", 40, "dense_dotted_parenthesized"),
            ],
        )

    def test_printed_toc_parses_page_title_slash_stream(self):
        pages = [PdfPage(1, "目录\n11/1 语言与存在的否定性 23/2 幼儿期与考古学方法")]
        entries = parse_printed_toc(pages)
        self.assertEqual(
            [(entry["title"], entry["printed_page"], entry["parse_style"]) for entry in entries],
            [
                ("语言与存在的否定性", 11, "page_title_slash_stream"),
                ("幼儿期与考古学方法", 23, "page_title_slash_stream"),
            ],
        )

    def test_printed_toc_parses_title_page_slash_stream(self):
        pages = [PdfPage(1, "目录\n第一章 起点 / 005 第二章 真理 / 009")]
        entries = parse_printed_toc(pages)
        self.assertEqual(
            [(entry["title"], entry["printed_page"], entry["parse_style"]) for entry in entries],
            [("第一章 起点", 5, "title_page_slash_stream"), ("第二章 真理", 9, "title_page_slash_stream")],
        )

    def test_multiline_toc_continuation_repairs_ocr_page_labels(self):
        pages = [
            PdfPage(3, "目录\n第一章 起点 / 005\n第二章 真理 / 069\n第三章 艺术 / i(>5"),
            PdfPage(4, "结论 / l2 3\n作品列表 / 135"),
            PdfPage(5, "第一章 起点\n这里开始的是正文，而不是目录。"),
        ]
        self.assertEqual([page.number for page in _toc_pages(pages)], [3, 4])
        self.assertEqual(_page_label_value("001"), 1)
        self.assertEqual(_page_label_value("l2 3"), 123)
        self.assertEqual(_page_label_value("i(>5"), 105)
        self.assertIsNone(_page_label_value("ISBN 978-7-0000"))
        self.assertEqual(
            [(entry["title"], entry["printed_page"]) for entry in parse_printed_toc(_toc_pages(pages))],
            [("第一章 起点", 5), ("第二章 真理", 69), ("第三章 艺术", 105), ("结论", 123), ("作品列表", 135)],
        )

    def test_toc_uses_validated_offset_and_conclusion_boundary(self):
        pages = [PdfPage(number, "正文") for number in range(1, 29)]
        pages[1] = PdfPage(2, "目录\n第一章 起点 / 005\n第二章 真理 / 009\n第三章 艺术 / 013")
        pages[2] = PdfPage(3, "结论 / 021")
        pages[8] = PdfPage(9, "第一章 起点\n第一章的正文从此处开始，提供足够长的内容证据。")
        pages[12] = PdfPage(13, "第二章 真理\n第二章的正文从此处开始，提供足够长的内容证据。")
        # The third title is deliberately absent. The three direct matches establish
        # offset +4 and safely map it to its TOC-implied physical start.
        pages[16] = PdfPage(17, "第三章正文从此处开始，提供足够长的内容证据。")
        pages[24] = PdfPage(25, "结\n论\n最后总结全书的主要观点，并提供足够长的内容证据。")
        result = analyse_pdf(pages, Path("damaged-toc.pdf"), min_confidence=0.45)
        self.assertEqual(result.method, "toc")
        self.assertEqual(
            [(unit.title, unit.physical_page_start) for unit in result.units],
            [("前置材料", 1), ("第一章 起点", 9), ("第二章 真理", 13), ("第三章 艺术", 17), ("结论", 25)],
        )
        toc = next(method for method in result.report["methods"] if method["method"] == "toc")
        self.assertEqual(toc["direct_match_offset"], 4)
        self.assertTrue(toc["direct_match_offset_evidence"]["accepted"])
        self.assertTrue(toc["safe_alignment"])

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

    def test_chinese_numbered_headings_and_back_matter_are_recognised(self):
        pages = [
            PdfPage(1, "一、第一篇\n第一篇正文有足够长的内容，以证明这里是章节开头。"),
            PdfPage(2, "二、第二篇\n第二篇正文有足够长的内容，以证明这里是章节开头。"),
            PdfPage(3, "附录\n附录正文有足够长的内容，以证明这里是后置材料。"),
            PdfPage(4, "译后记\n译后记正文有足够长的内容，以证明这里是后置材料。"),
        ]
        result = analyse_pdf(pages, Path("numbered.pdf"), mode="headings")
        self.assertEqual(result.method, "headings")
        self.assertEqual(
            [(unit.title, unit.kind, unit.level) for unit in result.units],
            [("一、第一篇", "chapter", 2), ("二、第二篇", "chapter", 2), ("附录", "back_matter", 1), ("译后记", "back_matter", 1)],
        )

    def test_explicit_levels_build_bounded_outline(self):
        manifest = {
            "schema_version": 1,
            "id": "outline",
            "source": {"filename": "outline.pdf", "page_count": 4},
            "units": [
                {"title": "第一部", "start": 1, "end": 1, "kind": "part", "level": 1},
                {"title": "第一章", "start": 2, "end": 2, "kind": "chapter", "level": 2},
                {"title": "第二章", "start": 3, "end": 3, "kind": "chapter", "level": 2},
                {"title": "附录", "start": 4, "end": 4, "kind": "back_matter", "level": 1},
            ],
        }
        result = analyse_pdf([PdfPage(index, "正文") for index in range(1, 5)], Path("outline.pdf"), structure_manifest=manifest)
        self.assertEqual(result.method, "manifest")
        self.assertEqual([node.title for node in result.outline], ["第一部", "附录"])
        self.assertEqual([node.title for node in result.outline[0].children], ["第一章", "第二章"])
        self.assertEqual(result.units[1].parent_id, "unit-01")
        self.assertEqual(result.units[1].breadcrumb, ("第一部", "第一章"))

    def test_rules_only_manifest_is_an_explicit_hint_not_manual_boundaries(self):
        manifest = {
            "schema_version": 1,
            "id": "rules-only",
            "source": {"filename": "rules.pdf", "page_count": 3},
            "toc": {"markers": ["目 录"], "entries": [
                {"title": "一、开始", "printed_page": 1, "level": 1, "kind": "chapter"},
                {"title": "二、继续", "printed_page": 2, "level": 1, "kind": "chapter"},
            ]},
        }
        pages = [
            PdfPage(1, "目 录\n一、开始 / 1\n二、继续 / 2"),
            PdfPage(2, "一、开始\n第一章正文有足够长的内容，以证明这里是章节开头。"),
            PdfPage(3, "二、继续\n第二章正文有足够长的内容，以证明这里是章节开头。"),
        ]
        result = analyse_pdf(pages, Path("rules.pdf"), min_confidence=0.45, structure_manifest=manifest)
        self.assertEqual(result.method, "toc")
        self.assertEqual(result.report["structure_manifest"]["application_mode"], "rules_only")

    def test_manifest_validator_rejects_non_partitioned_or_invalid_units(self):
        valid = {
            "schema_version": 1,
            "id": "valid",
            "source": {"filename": "valid.pdf", "page_count": 3},
            "units": [
                {"title": "一", "start": 1, "end": 1},
                {"title": "二", "start": 2, "end": 3},
            ],
        }
        self.assertEqual(validate_manifest(valid)["id"], "valid")
        overlap = json.loads(json.dumps(valid))
        overlap["units"][1]["start"] = 1
        with self.assertRaisesRegex(ValueError, "exact partition"):
            validate_manifest(overlap)
        gap = json.loads(json.dumps(valid))
        gap["units"][1]["start"] = 3
        with self.assertRaisesRegex(ValueError, "exact partition"):
            validate_manifest(gap)

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

    def test_fuzzy_toc_alignment_handles_numbered_ocr_heading_variants(self):
        pages = [PdfPage(number, "扉页") for number in range(1, 18)]
        pages[1] = PdfPage(2, "目录\n11/语言与存在的否定性\n23/幼儿期与考古学方法\n35/潜能与来临中的哲学的任务")
        pages[4] = PdfPage(5, "1 语言与存在的否定性\n本章正文从这里开始，提供足够长的内容证据。")
        pages[8] = PdfPage(9, "2 幼儿期与考古学方法\n本章正文从这里开始，提供足够长的内容证据。")
        pages[12] = PdfPage(13, "3 潜能与来l脑中的哲学的任务\n本章正文从这里开始，提供足够长的内容证据。")
        result = analyse_pdf(pages, Path("aganben-style.pdf"), min_confidence=0.45)
        self.assertEqual(result.method, "toc")
        self.assertEqual(
            [(unit.title, unit.physical_page_start) for unit in result.units],
            [("前置材料", 1), ("语言与存在的否定性", 5), ("幼儿期与考古学方法", 9), ("潜能与来临中的哲学的任务", 13)],
        )
        toc = next(method for method in result.report["methods"] if method["method"] == "toc")
        self.assertTrue(toc["genuine_toc_evidence"])
        self.assertTrue(toc["safe_alignment"])
        self.assertFalse(toc["direct_match_offset_evidence"]["accepted"])

    def test_fuzzy_toc_alignment_rejects_short_similar_heading(self):
        pages = [
            PdfPage(1, "目录\n11/政治与语言\n23/文学的实验室"),
            PdfPage(2, "政治\n这是一段足够长的正文，但不是目录中的完整标题。"),
            PdfPage(3, "文学的实验室\n第二章正文从这里开始，提供足够长的内容证据。"),
        ]
        result = analyse_pdf(pages, Path("fuzzy-negative.pdf"), min_confidence=0.45)
        toc = next(method for method in result.report["methods"] if method["method"] == "toc")
        self.assertFalse(toc["accepted"])
        self.assertIsNone(toc["matches"][0])

    def test_header_clusters_reject_later_ocr_variants(self):
        pages = [
            PdfPage(1, "一、“怎么办?”中的“怎么”\n第一章正文从这里开始，提供足够长的内容证据。", (PageBlock("一、“怎么办?”中的“怎么”", y0=20, size=20),)),
            PdfPage(2, "一、怎么办中的怎么\n这是相同章节的后续正文，而不是新的章节。", (PageBlock("一、怎么办中的怎么", y0=20, size=20),)),
            PdfPage(3, "二、安东尼奥·葛兰西的绝对经验主义\n第二章正文从这里开始，提供足够长的内容证据。", (PageBlock("二、安东尼奥·葛兰西的绝对经验主义", y0=20, size=20),)),
            PdfPage(4, "二、安东尼奥♦葛兰西的绝对经验主义\n这是相同章节的后续正文，而不是新的章节。", (PageBlock("二、安东尼奥♦葛兰西的绝对经验主义", y0=20, size=20),)),
        ]
        result = analyse_pdf(pages, Path("zen-me-ban-style.pdf"), mode="headings")
        self.assertEqual(result.method, "headings")
        self.assertEqual([unit.physical_page_start for unit in result.units], [1, 3])
        diagnostics = result.report["content_boundaries"]
        self.assertTrue(diagnostics["header_clusters"])
        self.assertEqual(
            [candidate["page"] for candidate in diagnostics["rejected"] if candidate["rejection"] == "running_header"],
            [2, 4],
        )

    def test_endpoint_only_boundaries_cannot_select_toc(self):
        pages = [
            PdfPage(1, "版权页"),
            PdfPage(2, "附录\n附录正文从这里开始，提供足够长的内容证据。"),
            PdfPage(3, "译后记\n译后记正文从这里开始，提供足够长的内容证据。"),
        ]
        result = analyse_pdf(pages, Path("endpoints-only.pdf"))
        self.assertEqual(result.method, "ranges")
        toc = next(method for method in result.report["methods"] if method["method"] == "toc")
        self.assertFalse(toc["accepted"])
        self.assertFalse(toc["genuine_toc_evidence"])
        self.assertEqual(toc["rejection"], "missing_printed_toc_evidence")

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

    def test_import_persists_manifest_snapshot_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source = Path(tmp) / "book.pdf"
            manifest_path = Path(tmp) / "structure.json"
            source.write_bytes(b"fixture")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "id": "fixture",
                "status": "pending-readable-source",
                "source": {"filename": "book.pdf", "page_count": 2, "sha256": digest},
                "toc": {"markers": ["目 录"]},
            }), encoding="utf-8")
            import_pdf(source, root, pages=["一、开始\n第一篇正文有足够长的内容。", "二、继续\n第二篇正文有足够长的内容。"], structure_manifest=manifest_path, max_unit_tokens=256)
            report = json.loads((root / ".onebookwiki" / "structure-report.json").read_text(encoding="utf-8"))
            metadata = json.loads((root / ".onebookwiki" / "source.json").read_text(encoding="utf-8"))
            snapshot = root / ".onebookwiki" / "structure-manifest.json"
            self.assertTrue(snapshot.is_file())
            self.assertEqual(report["structure_manifest"]["id"], "fixture")
            self.assertTrue(report["structure_manifest"]["source_match"]["sha256"])
            self.assertEqual(metadata["source_structure"]["structure_manifest"], report["structure_manifest"])

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
