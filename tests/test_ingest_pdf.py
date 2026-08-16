import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onebookwiki.cli import ingest_pdf as cli_module
from onebookwiki.importers import slugify, import_pdf


class PdfSlugTest(unittest.TestCase):
    def test_uses_book_title_not_previous_book_slug(self):
        self.assertEqual(slugify("Phenomenology of Spirit"), "phenomenology-of-spirit")
        self.assertEqual(slugify("Fou Ren"), "fou-ren")

    def test_non_latin_title_has_deterministic_fallback(self):
        self.assertEqual(slugify("否认"), "book")

    def test_postprocess_option_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixture.pdf"
            root = Path(tmp) / "fixture-project"
            source.write_bytes(b"fixture")
            with patch("onebookwiki.cli.ingest_pdf.import_pdf", return_value=[]) as imported:
                self.assertEqual(cli_module.main(["ingest_pdf.py", str(source), str(root), "--pdf-postprocess", "strict"]), 0)
        self.assertEqual(imported.call_args.kwargs["postprocess"], "strict")

    def test_pdf_ocr_options_are_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixture.pdf"
            root = Path(tmp) / "fixture-project"
            source.write_bytes(b"fixture")
            with patch("onebookwiki.cli.ingest_pdf.import_pdf", return_value=[]) as imported:
                self.assertEqual(cli_module.main(["ingest_pdf.py", str(source), str(root), "--pdf-ocr", "assist", "--pdf-ocr-dpi", "200", "--pdf-ocr-confidence", "0.8"]), 0)
        self.assertEqual(imported.call_args.kwargs["pdf_ocr"], "assist")
        self.assertEqual(imported.call_args.kwargs["pdf_ocr_dpi"], 200)
        self.assertEqual(imported.call_args.kwargs["pdf_ocr_confidence"], 0.8)

    def test_structure_manifest_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixture.pdf"
            root = Path(tmp) / "fixture-project"
            manifest = Path(tmp) / "structure.json"
            source.write_bytes(b"fixture")
            manifest.write_text("{}", encoding="utf-8")
            with patch("onebookwiki.cli.ingest_pdf.import_pdf", return_value=[]) as imported:
                self.assertEqual(cli_module.main(["ingest_pdf.py", str(source), str(root), "--manifest", str(manifest)]), 0)
        self.assertEqual(imported.call_args.kwargs["structure_manifest"], manifest.resolve())


if __name__ == "__main__":
    unittest.main()
