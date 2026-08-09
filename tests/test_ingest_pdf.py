import tempfile
import unittest
from pathlib import Path
import importlib.util
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ingest_pdf.py"
spec = importlib.util.spec_from_file_location("ingest_pdf", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PdfSlugTest(unittest.TestCase):
    def test_uses_book_title_not_previous_book_slug(self):
        self.assertEqual(module.slugify("Phenomenology of Spirit"), "phenomenology-of-spirit")
        self.assertEqual(module.slugify("Fou Ren"), "fou-ren")

    def test_non_latin_title_has_deterministic_fallback(self):
        self.assertEqual(module.slugify("否认"), "book")

    def test_postprocess_option_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixture.pdf"
            root = Path(tmp) / "fixture-project"
            source.write_bytes(b"fixture")
            with patch.object(module, "import_pdf", return_value=[]) as imported:
                self.assertEqual(module.main(["ingest_pdf.py", str(source), str(root), "--pdf-postprocess", "strict"]), 0)
        self.assertEqual(imported.call_args.kwargs["postprocess"], "strict")

    def test_structure_manifest_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixture.pdf"
            root = Path(tmp) / "fixture-project"
            manifest = Path(tmp) / "structure.json"
            source.write_bytes(b"fixture")
            manifest.write_text("{}", encoding="utf-8")
            with patch.object(module, "import_pdf", return_value=[]) as imported:
                self.assertEqual(module.main(["ingest_pdf.py", str(source), str(root), "--manifest", str(manifest)]), 0)
        self.assertEqual(imported.call_args.kwargs["structure_manifest"], manifest.resolve())


if __name__ == "__main__":
    unittest.main()
