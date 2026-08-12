import tempfile
import unittest
import zipfile
from pathlib import Path

from onebookwiki.importers import import_epub, parse_epub


class EpubImportTest(unittest.TestCase):
    def make_epub(self, path: Path):
        with zipfile.ZipFile(path, "w") as book:
            book.writestr("META-INF/container.xml", """<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>""")
            book.writestr("OEBPS/content.opf", """<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' unique-identifier='x'><metadata><dc:title xmlns:dc='http://purl.org/dc/elements/1.1/'>测试书</dc:title><dc:creator xmlns:dc='http://purl.org/dc/elements/1.1/'>作者</dc:creator></metadata><manifest><item id='toc' href='toc.ncx' media-type='application/x-dtbncx+xml'/><item id='c1' href='one.xhtml' media-type='application/xhtml+xml'/><item id='c2' href='two.xhtml' media-type='application/xhtml+xml'/></manifest><spine toc='toc'><itemref idref='c1'/><itemref idref='c2'/></spine></package>""")
            book.writestr("OEBPS/toc.ncx", """<?xml version='1.0'?><ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'><navMap><navPoint id='part'><navLabel><text>第一部</text></navLabel><content src='one.xhtml'/><navPoint id='one'><navLabel><text>第一章 导论</text></navLabel><content src='one.xhtml#start'/></navPoint><navPoint id='two'><navLabel><text>第二章 展开</text></navLabel><content src='two.xhtml#start'/></navPoint></navPoint></navMap></ncx>""")
            book.writestr("OEBPS/one.xhtml", "<html><body><h1>第一章</h1><p>第一段内容。</p></body></html>")
            book.writestr("OEBPS/two.xhtml", "<html><body><h1>第二章</h1><p>第二段内容。</p></body></html>")

    def make_epub_shared_spine(self, path: Path):
        """EPUB where sibling sub-chapters share a single XHTML spine item."""
        with zipfile.ZipFile(path, "w") as book:
            book.writestr("META-INF/container.xml", """<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>""")
            book.writestr("OEBPS/content.opf", """<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' unique-identifier='x'><metadata><dc:title xmlns:dc='http://purl.org/dc/elements/1.1/'>东晋门阀政治</dc:title></metadata><manifest><item id='toc' href='toc.ncx' media-type='application/x-dtbncx+xml'/><item id='id462' href='part0005_split_003.html' media-type='application/xhtml+xml'/></manifest><spine toc='toc'><itemref idref='id462'/></spine></package>""")
            # Three sibling sub-chapters inside one XHTML file, each with a TOC entry
            book.writestr("OEBPS/toc.ncx", """<?xml version='1.0'?><ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'><navMap><navPoint id='section'><navLabel><text>四 郗鉴与京口经营</text></navLabel><content src='part0005_split_003.html'/><navPoint id='sub_a'><navLabel><text>（一）三吴的战略地位</text></navLabel><content src='part0005_split_003.html#c005'/></navPoint><navPoint id='sub_b'><navLabel><text>（二）会稽——三吴的腹心</text></navLabel><content src='part0005_split_003.html#c007'/></navPoint><navPoint id='sub_c'><navLabel><text>（三）建康、会稽间的交通线</text></navLabel><content src='part0005_split_003.html#c009'/></navPoint></navPoint></navMap></ncx>""")
            book.writestr("OEBPS/part0005_split_003.html", "<html><body><h1>四 郗鉴与京口经营</h1><p>引言段落。</p><h2 id=\"c005\">（一）三吴的战略地位</h2><p>三吴的战略地位内容……</p><h2 id=\"c007\">（二）会稽——三吴的腹心</h2><p>会稽的内容……</p><h2 id=\"c009\">（三）建康、会稽间的交通线</h2><p>交通线的内容……</p></body></html>")

    def test_parse_and_write_spine_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            root = Path(tmp) / "project"
            self.make_epub(source)
            parsed = parse_epub(source, keep_front_matter=True)
            self.assertEqual([item["href"] for item in parsed["entries"]], ["OEBPS/one.xhtml", "OEBPS/two.xhtml"])
            self.assertEqual(parsed["entries"][0]["title"], "第一章 导论")
            self.assertEqual(parsed["entries"][0]["breadcrumb"], ["第一部", "第一章 导论"])
            self.assertEqual(parsed["entries"][1]["breadcrumb"], ["第一部", "第二章 展开"])
            files = import_epub(source, root, force=True, keep_front_matter=True)
            self.assertEqual(len(files), 2)
            raw = files[0].read_text(encoding="utf-8")
            self.assertIn("> Spine: c1", raw)
            self.assertTrue(raw.startswith("# 第一章"))
            self.assertNotIn("# Chapter", raw)
            source_metadata = (root / ".onebookwiki" / "source.json").read_text(encoding="utf-8")
            self.assertIn('"schema_version": 3', source_metadata)
            self.assertIn('"source_structure"', source_metadata)
            source = __import__("json").loads(source_metadata)
            outline = source["source_structure"]["outline"]
            self.assertEqual(outline[0]["title"], "第一部")
            self.assertEqual([child["title"] for child in outline[0]["children"]], ["第一章 导论", "第二章 展开"])
            self.assertEqual(outline[0]["children"][0]["pageIds"], ["chapter-01"])

    def test_sibling_subchapters_all_get_pageids(self):
        """Sibling TOC entries sharing one spine item each receive distinct pageIds."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "shared_spine.epub"
            root = Path(tmp) / "project"
            self.make_epub_shared_spine(source)
            files = import_epub(source, root, force=True, keep_front_matter=True)
            # One spine item with 3 fragments → 4 raw chapter files
            # (parent intro + 3 sub-chapters)
            self.assertEqual(len(files), 4,
                "One spine item with 3 distinct fragments → 4 segments")
            source_metadata = __import__("json").loads(
                (root / ".onebookwiki" / "source.json").read_text(encoding="utf-8")
            )
            outline = source_metadata["source_structure"]["outline"]
            # Parent node: "四 郗鉴与京口经营" → owns the fragment-less segment
            self.assertEqual(outline[0]["title"], "四 郗鉴与京口经营")
            self.assertNotEqual(outline[0]["pageIds"], [],
                "Parent node must have a pageId for the intro segment")
            # Three sibling sub-chapters, each with its own distinct pageId
            child_titles = [child["title"] for child in outline[0]["children"]]
            self.assertEqual(
                child_titles,
                ["（一）三吴的战略地位", "（二）会稽——三吴的腹心", "（三）建康、会稽间的交通线"],
            )
            all_page_ids: set[str] = set()
            for child in outline[0]["children"]:
                self.assertNotEqual(
                    child["pageIds"], [],
                    f"'{child['title']}' must have at least one pageId",
                )
                self.assertEqual(len(child["pageIds"]), 1,
                    f"'{child['title']}' should have exactly one pageId")
                all_page_ids.add(child["pageIds"][0])
            # Each sub-chapter gets its own distinct chapter page
            self.assertEqual(len(all_page_ids), 3,
                "Each sub-chapter must have a distinct pageId, not share one")

    def test_parse_epub_splits_by_fragment(self):
        """parse_epub creates separate entries for each distinct TOC fragment."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "shared_spine.epub"
            self.make_epub_shared_spine(source)
            parsed = parse_epub(source, keep_front_matter=True)
            entries = parsed["entries"]
            self.assertEqual(len(entries), 4,
                "4 segments: 1 intro + 3 sub-chapters")
            # Segment 1: intro (no fragment)
            self.assertEqual(entries[0]["title"], "四 郗鉴与京口经营")
            self.assertEqual(entries[0]["fragment"], "")
            self.assertIn("四 郗鉴与京口经营", entries[0]["text"])
            self.assertNotIn("（一）三吴", entries[0]["text"])
            # Segment 2: first sub-chapter
            self.assertEqual(entries[1]["title"], "（一）三吴的战略地位")
            self.assertEqual(entries[1]["fragment"], "c005")
            self.assertIn("三吴的战略地位内容", entries[1]["text"])
            # Segment 3: second sub-chapter
            self.assertEqual(entries[2]["title"], "（二）会稽——三吴的腹心")
            self.assertEqual(entries[2]["fragment"], "c007")
            self.assertIn("会稽的内容", entries[2]["text"])
            # Segment 4: third sub-chapter
            self.assertEqual(entries[3]["title"], "（三）建康、会稽间的交通线")
            self.assertEqual(entries[3]["fragment"], "c009")
            self.assertIn("交通线的内容", entries[3]["text"])
            # All share the same href and spine metadata
            for entry in entries:
                self.assertEqual(entry["href"], "OEBPS/part0005_split_003.html")
                self.assertEqual(entry["spine"], "id462")
                self.assertEqual(entry["spine_index"], 1)

    def test_parse_epub_no_fragments_unchanged(self):
        """When TOC has no fragments, behavior is unchanged (single entry per spine item)."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "no_fragments.epub"
            self.make_epub(source)
            parsed = parse_epub(source, keep_front_matter=True)
            entries = parsed["entries"]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["title"], "第一章 导论")
            self.assertEqual(entries[1]["title"], "第二章 展开")


if __name__ == "__main__":
    unittest.main()
