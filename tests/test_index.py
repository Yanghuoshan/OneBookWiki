import tempfile
import unittest
from pathlib import Path

from onebookwiki.chunking import chunk_text
from onebookwiki.index import LocalIndex


class IndexTest(unittest.TestCase):
    def test_update_persists_and_reuses_unchanged_chunk_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = root / "raw" / "chapters" / "01-a.md"
            chapter.parent.mkdir(parents=True)
            chapter.write_text("# A\n\n> Chapter: 1\n\nAlpha evidence.\n", encoding="utf-8")
            index = LocalIndex(root)
            chunks = chunk_text(chapter.read_text(encoding="utf-8"), "raw/chapters/01-a.md", 1)
            _, added = index.update(chapter.relative_to(root), 1, chunks)
            self.assertEqual(len(added), len(chunks))
            first_ids = set(index.manifest.chapters["raw/chapters/01-a.md"]["chunk_ids"])
            index = LocalIndex(root)
            self.assertEqual(set(index.manifest.chapters["raw/chapters/01-a.md"]["chunk_ids"]), first_ids)


if __name__ == "__main__":
    unittest.main()
