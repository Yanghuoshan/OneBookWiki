import json
import tempfile
import unittest
from pathlib import Path

from onebookwiki.wiki_retrieval import assemble_wiki_first_context, search_artifacts, search_wiki


class WikiRetrievalTest(unittest.TestCase):
    def test_wiki_first_context_has_source_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wiki").mkdir()
            (root / ".onebookwiki" / "artifacts" / "chapters").mkdir(parents=True)
            (root / "wiki" / "book.md").write_text("# Book\n\n## Core Thesis\n\nAttention organizes reading.", encoding="utf-8")
            (root / ".onebookwiki" / "artifacts" / "chapters" / "0001.json").write_text(json.dumps({"chapter": 1, "title": "Attention", "executive_summary": "Attention organizes reading."}), encoding="utf-8")
            wiki = search_wiki(root, "attention")
            artifacts = search_artifacts(root, "attention")
            raw = []
            context, selected = assemble_wiki_first_context(wiki, artifacts, raw, max_tokens=500)
            self.assertIn("WIKI PAGE", context)
            self.assertIn("STRUCTURED ARTIFACT", context)
            self.assertEqual([item["source_kind"] for item in selected[:2]], ["wiki", "artifact"])

    def test_context_budget_is_hard_across_all_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wiki").mkdir()
            (root / ".onebookwiki" / "artifacts").mkdir(parents=True)
            long = "attention " * 1000
            (root / "wiki" / "book.md").write_text("# Book\n\n" + long, encoding="utf-8")
            (root / ".onebookwiki" / "artifacts" / "book.json").write_text(json.dumps({"chapter": 1, "text": long}), encoding="utf-8")
            wiki = search_wiki(root, "attention", top_k=4)
            artifacts = search_artifacts(root, "attention", top_k=4)
            context, selected = assemble_wiki_first_context(wiki, artifacts, [], max_tokens=300)
            self.assertTrue(selected)
            self.assertLessEqual(sum(int(item.get("token_count") or 0) for item in selected), 300)
            self.assertLessEqual(len(context.split()), 400)
            self.assertTrue(any(item.get("truncated") for item in selected))


if __name__ == "__main__":
    unittest.main()
