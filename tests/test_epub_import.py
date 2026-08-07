import tempfile
import unittest
import zipfile
from pathlib import Path

from onebookwiki.importers import import_epub, parse_epub


class EpubImportTest(unittest.TestCase):
    def make_epub(self, path: Path):
        with zipfile.ZipFile(path, "w") as book:
            book.writestr("META-INF/container.xml", """<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>""")
            book.writestr("OEBPS/content.opf", """<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' unique-identifier='x'><metadata><dc:title xmlns:dc='http://purl.org/dc/elements/1.1/'>测试书</dc:title><dc:creator xmlns:dc='http://purl.org/dc/elements/1.1/'>作者</dc:creator></metadata><manifest><item id='c1' href='one.xhtml' media-type='application/xhtml+xml'/><item id='c2' href='two.xhtml' media-type='application/xhtml+xml'/></manifest><spine><itemref idref='c1'/><itemref idref='c2'/></spine></package>""")
            book.writestr("OEBPS/one.xhtml", "<html><body><h1>第一章</h1><p>第一段内容。</p></body></html>")
            book.writestr("OEBPS/two.xhtml", "<html><body><h1>第二章</h1><p>第二段内容。</p></body></html>")

    def test_parse_and_write_spine_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.epub"
            root = Path(tmp) / "project"
            self.make_epub(source)
            parsed = parse_epub(source, keep_front_matter=True)
            self.assertEqual([item["href"] for item in parsed["entries"]], ["OEBPS/one.xhtml", "OEBPS/two.xhtml"])
            files = import_epub(source, root, force=True, keep_front_matter=True)
            self.assertEqual(len(files), 2)
            self.assertIn("> Spine: c1", files[0].read_text(encoding="utf-8"))
            self.assertTrue((root / ".onebookwiki" / "source.json").is_file())


if __name__ == "__main__":
    unittest.main()
