import unittest
from pathlib import Path
import importlib.util

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


if __name__ == "__main__":
    unittest.main()
