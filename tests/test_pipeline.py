import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from onebookwiki.importers import import_epub

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
BUILD = PACKAGE_ROOT / "scripts" / "build_wiki.py"
CHECK = PACKAGE_ROOT / "scripts" / "check_book.py"


class PipelineTest(unittest.TestCase):
    def make_epub(self, path: Path) -> None:
        with zipfile.ZipFile(path, "w") as book:
            book.writestr("META-INF/container.xml", """<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>""")
            book.writestr("OEBPS/content.opf", """<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf'><metadata><dc:title xmlns:dc='http://purl.org/dc/elements/1.1/'>Pipeline Book</dc:title></metadata><manifest><item id='c1' href='one.xhtml' media-type='application/xhtml+xml'/><item id='c2' href='two.xhtml' media-type='application/xhtml+xml'/></manifest><spine><itemref idref='c1'/><itemref idref='c2'/></spine></package>""")
            book.writestr("OEBPS/one.xhtml", "<html><body><h1>Attention</h1><p>A question organizes attention.</p></body></html>")
            book.writestr("OEBPS/two.xhtml", "<html><body><h1>Memory</h1><p>Notes preserve evidence.</p></body></html>")

    def test_epub_dry_run_creates_index_and_checkpoint_without_wiki(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "book"
            source = Path(tmp) / "book.epub"
            self.make_epub(source)
            result = subprocess.run([sys.executable, str(BUILD), str(source), str(root), "--dry-run", "--keep-front-matter", "--provider", "none", "--backend", "lexical"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("imported 2 raw chapter file(s)", result.stdout)
            self.assertIn("dry run complete; 4 generation node(s) planned", result.stdout)
            self.assertIn("[generate] provider=none, model=default, retries=4", result.stderr)
            self.assertEqual(len(list((root / "raw" / "chapters").glob("*.md"))), 2)
            self.assertTrue((root / ".onebookwiki" / "manifest.json").is_file())
            self.assertFalse((root / "wiki" / "book.md").is_file())
            latest = json.loads((root / ".onebookwiki" / "checkpoints" / "latest.json").read_text(encoding="utf-8"))
            self.assertTrue(latest["run_id"])

    def test_import_is_idempotent_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "book"
            source = Path(tmp) / "book.epub"
            self.make_epub(source)
            first = import_epub(source, root)
            before = first[0].read_text(encoding="utf-8")
            source.write_bytes(source.read_bytes() + b"\n")
            second = import_epub(source, root)
            self.assertEqual(before, second[0].read_text(encoding="utf-8"))

    def test_checker_accepts_generated_structure_fixture(self):
        fixture = PACKAGE_ROOT / "examples" / "sample-book"
        result = subprocess.run([sys.executable, str(CHECK), str(fixture), "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total"], 0)


if __name__ == "__main__":
    unittest.main()
