import tempfile
import unittest
from pathlib import Path

from onebookwiki.evidence_registry import (
    EvidenceRegistryError,
    load_evidence_revision,
    register_project,
)


class EvidenceRegistryTest(unittest.TestCase):
    def make_chapter(self, root: Path, *, collected: str = "2026-08-20", body: str = "Useful evidence.") -> Path:
        chapter = root / "raw" / "chapters" / "01-example.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text(
            f"# Example\n\n> Chapter: 1\n> Collected: {collected}\n\n{body}\n",
            encoding="utf-8",
        )
        return chapter

    def test_repeated_registration_reuses_immutable_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_chapter(root)
            first = register_project(root)
            second = register_project(root)
            self.assertEqual(first.book_revision_id, second.book_revision_id)
            self.assertEqual(set(first.evidence), set(second.evidence))
            revision_id = next(iter(first.evidence))
            revision, body = load_evidence_revision(root, revision_id)
            self.assertEqual(revision["kind"], "evidence_revision")
            self.assertEqual(body, "Useful evidence.")
            self.assertNotIn("Collected", body)

    def test_metadata_date_does_not_change_body_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = self.make_chapter(root, collected="2026-08-19")
            first = register_project(root)
            first_chapter = first.chapters["raw/chapters/01-example.md"]
            chapter.write_text(
                "# Example\n\n> Chapter: 1\n> Collected: 2026-08-20\n\nUseful evidence.\n",
                encoding="utf-8",
            )
            second = register_project(root)
            second_chapter = second.chapters["raw/chapters/01-example.md"]
            self.assertEqual(first_chapter.body_hash, second_chapter.body_hash)

    def test_body_change_creates_new_book_and_evidence_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = self.make_chapter(root)
            first = register_project(root)
            chapter.write_text(
                "# Example\n\n> Chapter: 1\n> Collected: 2026-08-20\n\nChanged evidence.\n",
                encoding="utf-8",
            )
            second = register_project(root)
            self.assertNotEqual(first.book_revision_id, second.book_revision_id)
            self.assertNotEqual(set(first.evidence), set(second.evidence))

    def test_tampered_evidence_body_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_chapter(root)
            snapshot = register_project(root)
            revision_id = next(iter(snapshot.evidence))
            body_path = root / snapshot.evidence[revision_id].body_path
            body_path.write_text("tampered", encoding="utf-8")
            with self.assertRaises(EvidenceRegistryError):
                load_evidence_revision(root, revision_id)

    def test_tampered_source_body_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = self.make_chapter(root)
            snapshot = register_project(root)
            revision_id = next(iter(snapshot.evidence))
            chapter.write_text(
                "# Example\n\n> Chapter: 1\n> Collected: 2026-08-20\n\nTampered source.\n",
                encoding="utf-8",
            )
            with self.assertRaises(EvidenceRegistryError):
                load_evidence_revision(root, revision_id)


if __name__ == "__main__":
    unittest.main()
