import json
import tempfile
import unittest
from pathlib import Path

from onebookwiki.cli.ingest_book import index_project


class IngestBookTest(unittest.TestCase):
    def test_local_index_rebuilds_when_chunk_profile_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = root / "raw" / "chapters" / "01-example.md"
            chapter.parent.mkdir(parents=True)
            chapter.write_text("# Example\n\n> Chapter: 1\n\nUseful evidence.\n", encoding="utf-8")

            self.assertEqual(index_project(root), (1, 1, 0))
            self.assertEqual(index_project(root), (0, 0, 1))

            manifest = root / ".onebookwiki" / "manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["chapters"]["raw/chapters/01-example.md"]["chunking"]["revision"] = "obsolete"
            manifest.write_text(json.dumps(data), encoding="utf-8")

            self.assertEqual(index_project(root), (1, 0, 0))
            stored = json.loads(manifest.read_text(encoding="utf-8"))
            entry = stored["chapters"]["raw/chapters/01-example.md"]
            self.assertEqual(entry["chunking"]["revision"], "smaller-v1")
            self.assertEqual(entry["index_identity"], {"backend": "lexical", "model": "none"})


if __name__ == "__main__":
    unittest.main()
