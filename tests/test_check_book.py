import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CHECKER = PACKAGE_ROOT / "scripts" / "check_book.py"
INDEXER = PACKAGE_ROOT / "scripts" / "ingest_book.py"
EXAMPLE = PACKAGE_ROOT / "examples" / "sample-book"


def run(command, root, *args):
    return subprocess.run([sys.executable, str(command), str(root), *args], capture_output=True, text=True)


class BookCheckerTest(unittest.TestCase):
    def copy_example(self, root: Path):
        for source in EXAMPLE.rglob("*"):
            if source.is_file():
                target = root / source.relative_to(EXAMPLE)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def test_example_indexes_and_checks_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            indexed = subprocess.run([sys.executable, str(INDEXER), "index", str(root), "--backend", "lexical"], capture_output=True, text=True)
            self.assertEqual(indexed.returncode, 0, indexed.stderr)
            checked = run(CHECKER, root)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("## Summary\n0 issue(s)", checked.stdout)

    def test_duplicate_chapter_and_fabricated_quote_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            duplicate = root / "wiki" / "chapters" / "01-copy.md"
            duplicate.write_text((root / "wiki" / "chapters" / "01-attention.md").read_text(encoding="utf-8"), encoding="utf-8")
            original = root / "wiki" / "chapters" / "02-memory.md"
            original.write_text(original.read_text(encoding="utf-8") + '\n> "This fabricated quote is not in the source."\n', encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertIn("duplicate chapter number: 1", checked.stdout)
            self.assertIn("fabricated quote", checked.stdout)

    def test_stale_manifest_is_reported_after_raw_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            subprocess.run([sys.executable, str(INDEXER), "index", str(root), "--backend", "lexical"], check=True, capture_output=True, text=True)
            raw = root / "raw" / "chapters" / "01-attention.md"
            raw.write_text(raw.read_text(encoding="utf-8") + "\nChanged source.\n", encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertIn("stale chapter hash: raw/chapters/01-attention.md", checked.stdout)


if __name__ == "__main__":
    unittest.main()
