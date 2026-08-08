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


if __name__ == "__main__":
    unittest.main()
