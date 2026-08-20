"""Hermetic regression coverage for the build_wiki/generate_wiki release CLIs.

Every test uses a temporary v2 SQLite database and a mocked provider response;
no real provider, external service, production database, or .env is touched.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onebookwiki.checkpoints import CheckpointStore
from onebookwiki.cli import build_wiki, generate_wiki
from onebookwiki.manifest import Manifest
from onebookwiki.providers import GenerationResponse
from server.database import create_placeholder, init_db


def _statement(prefix: str, evidence: dict, chapter: int | None) -> dict:
    scope = {
        "documentary_scope": "reading_unit" if chapter is not None else "book",
        "target_type": "chapter" if chapter is not None else "book",
        "chapter": chapter,
    }
    return {
        "draft_id": f"{prefix}-claim",
        "canonical_key": f"{prefix}.claim",
        "kind": "factual",
        "subject": "Attention",
        "truth_condition": "Questions direct attention.",
        "scope": scope,
        "abstraction": "concrete_detail",
        "qualification": None,
        "confidence": "high",
        "supports": [{
            "evidence_revision_id": evidence["evidence_revision_id"],
            "support_type": "positive",
            "quote": evidence["quote"],
            "span_map": {
                "source_line_start": evidence["source_line_start"],
                "source_line_end": evidence["source_line_end"],
            },
        }],
    }


def payload_for_evidence(evidence: dict, chapter: int | None, *, draft_prefix: str | None = None) -> dict:
    prefix = draft_prefix or (f"chapter-{chapter}" if chapter is not None else "book")
    return {
        "statements": [_statement(prefix, evidence, chapter)],
        "compositions": [{
            "draft_id": f"{prefix}-summary",
            "canonical_key": f"{prefix}.summary",
            "kind": "chapter_summary" if chapter is not None else "overview",
            "members": [{"member_type": "statement", "draft_id": f"{prefix}-claim", "role": "support"}],
            "rendering": {"text": "Attention organizes inquiry.", "span_map": {}},
        }],
    }


def evidence_for(root: Path, chapter: int) -> dict:
    manifest = Manifest.load(root)
    values = [
        dict(item) for item in manifest.evidence_revisions.values()
        if int(item["chapter"]) == chapter
    ]
    values.sort(key=lambda item: int(item["source_line_start"]))
    return values[0]


def response_for(root: Path, chapter: int, *, prefix: str | None = None) -> GenerationResponse:
    evidence = evidence_for(root, chapter)
    return GenerationResponse(
        text=json.dumps(payload_for_evidence(evidence, chapter, draft_prefix=prefix)),
        model="test",
    )


def rollup_response(root: Path, chapter: int) -> GenerationResponse:
    evidence = evidence_for(root, chapter)
    payload = payload_for_evidence(evidence, None, draft_prefix="rollup")
    payload["statements"][0]["scope"] = {"documentary_scope": "rollup", "target_type": "book", "chapter": None}
    return GenerationResponse(text=json.dumps(payload), model="test")


def book_response(root: Path, chapter: int) -> GenerationResponse:
    evidence = evidence_for(root, chapter)
    payload = payload_for_evidence(evidence, None)
    return GenerationResponse(text=json.dumps(payload), model="test")


def make_source(root: Path) -> Path:
    source = root / "book.txt"
    source.write_text(
        "Attention\n=========\n\nQuestions direct attention.\n",
        encoding="utf-8",
    )
    return source


class BuildWikiCliTest(unittest.TestCase):
    def test_missing_database_or_book_id_rejects_before_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "book"
            source = make_source(Path(tmp))
            with patch("onebookwiki.generation.generate_response") as provider:
                code = build_wiki.main([
                    "build_wiki.py", str(source), str(root),
                    "--backend", "lexical", "--provider", "test",
                ])
            self.assertEqual(code, 1)
            provider.assert_not_called()
            self.assertFalse((root / "raw").exists())

    def test_dry_run_never_opens_or_publishes_a_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "book"
            source = make_source(Path(tmp))
            with patch("onebookwiki.generation.generate_response") as provider, \
                 patch("server.database.publish_registry_snapshot") as publish:
                code = build_wiki.main([
                    "build_wiki.py", str(source), str(root),
                    "--backend", "lexical", "--provider", "test", "--dry-run",
                ])
            self.assertEqual(code, 0)
            provider.assert_not_called()
            publish.assert_not_called()
            self.assertFalse((root / "wiki").exists())

    def run_release(self, tmp: str) -> tuple[int, Path, Path]:
        # Index first (dry-run) so evidence_for can resolve real evidence IDs
        # before the mocked provider needs to cite them.
        root = Path(tmp) / "book"
        source = make_source(Path(tmp))
        db_path = Path(tmp) / "onebookwiki.db"
        conn = init_db(str(db_path))
        book_id = create_placeholder(conn, "Test Book")
        conn.close()
        build_wiki.main([
            "build_wiki.py", str(source), str(root),
            "--backend", "lexical", "--provider", "test", "--dry-run",
        ])
        with patch(
            "onebookwiki.generation.generate_response",
            side_effect=[response_for(root, 1), rollup_response(root, 1), book_response(root, 1)],
        ):
            code = build_wiki.main([
                "build_wiki.py", str(source), str(root),
                "--backend", "lexical", "--provider", "test",
                "--database", str(db_path), "--book-id", str(book_id),
                "--skip-check",
            ])
        return code, root, db_path

    def test_lexical_release_publishes_and_projects_without_building_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, root, db_path = self.run_release(tmp)
            self.assertEqual(code, 0)
            manifest = Manifest.load(root)
            self.assertEqual(manifest.publication_health.get("status"), "healthy")
            self.assertTrue((root / "wiki" / "structure.json").is_file())
            self.assertTrue((root / "wiki" / "evidence.json").is_file())
            self.assertFalse((root / ".onebookwiki" / "wiki-vectors").exists())

    def test_release_projects_the_precise_published_manifest_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, root, db_path = self.run_release(tmp)
            self.assertEqual(code, 0)
            manifest = Manifest.load(root)
            structure = json.loads((root / "wiki" / "structure.json").read_text(encoding="utf-8"))
            evidence = json.loads((root / "wiki" / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(structure["bookRevisionId"], manifest.book_revision_id)
            self.assertEqual(evidence["bookRevisionId"], manifest.book_revision_id)

    def test_generation_failure_prevents_rendering_and_metadata_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "book"
            source = make_source(Path(tmp))
            db_path = Path(tmp) / "onebookwiki.db"
            conn = init_db(str(db_path))
            book_id = create_placeholder(conn, "Test Book")
            conn.close()
            with patch("onebookwiki.generation.generate_response", return_value=GenerationResponse(text="not json", model="test")):
                code = build_wiki.main([
                    "build_wiki.py", str(source), str(root),
                    "--backend", "lexical", "--provider", "test",
                    "--database", str(db_path), "--book-id", str(book_id),
                    "--skip-check",
                ])
            self.assertEqual(code, 1)
            self.assertFalse((root / "wiki").exists())
            manifest = Manifest.load(root)
            self.assertNotEqual(manifest.publication_health.get("status"), "healthy")

    def test_chapter_and_book_subcommands_cannot_produce_a_release_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "book"
            source = make_source(Path(tmp))
            db_path = Path(tmp) / "onebookwiki.db"
            conn = init_db(str(db_path))
            book_id = create_placeholder(conn, "Test Book")
            conn.close()
            build_wiki.main([
                "build_wiki.py", str(source), str(root),
                "--backend", "lexical", "--provider", "test", "--dry-run",
            ])
            with patch("onebookwiki.generation.generate_response", side_effect=[response_for(root, 1)]):
                code = generate_wiki.main([
                    "generate_wiki.py", "chapter", str(root),
                    "--provider", "test", "--embedding-backend", "lexical",
                    "--database", str(db_path), "--book-id", str(book_id),
                ])
            self.assertEqual(code, 0)
            self.assertFalse((root / "wiki").exists())


class GenerateWikiCliTest(unittest.TestCase):
    def prepare(self, tmp: str) -> tuple[Path, Path, int]:
        root = Path(tmp) / "book"
        source = make_source(Path(tmp))
        db_path = Path(tmp) / "onebookwiki.db"
        conn = init_db(str(db_path))
        book_id = create_placeholder(conn, "Test Book")
        conn.close()
        code = build_wiki.main([
            "build_wiki.py", str(source), str(root),
            "--backend", "lexical", "--provider", "test", "--dry-run",
        ])
        self.assertEqual(code, 0)
        return root, db_path, book_id

    def test_missing_database_or_book_id_rejects_before_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path, book_id = self.prepare(tmp)
            with patch("onebookwiki.generation.generate_response") as provider:
                code = generate_wiki.main([
                    "generate_wiki.py", "all", str(root),
                    "--provider", "test", "--embedding-backend", "lexical",
                ])
            self.assertEqual(code, 1)
            provider.assert_not_called()

    def test_dry_run_never_opens_a_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path, book_id = self.prepare(tmp)
            with patch("onebookwiki.generation.generate_response") as provider, \
                 patch("server.database.publish_registry_snapshot") as publish:
                code = generate_wiki.main([
                    "generate_wiki.py", "all", str(root),
                    "--provider", "test", "--embedding-backend", "lexical", "--dry-run",
                ])
            self.assertEqual(code, 0)
            provider.assert_not_called()
            publish.assert_not_called()

    def test_all_command_publishes_and_projects_a_release_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path, book_id = self.prepare(tmp)
            with patch(
                "onebookwiki.generation.generate_response",
                side_effect=[response_for(root, 1), rollup_response(root, 1), book_response(root, 1)],
            ):
                code = generate_wiki.main([
                    "generate_wiki.py", "all", str(root),
                    "--provider", "test", "--embedding-backend", "lexical",
                    "--database", str(db_path), "--book-id", str(book_id),
                ])
            self.assertEqual(code, 0)
            manifest = Manifest.load(root)
            self.assertEqual(manifest.publication_health.get("status"), "healthy")
            structure = json.loads((root / "wiki" / "structure.json").read_text(encoding="utf-8"))
            self.assertEqual(structure["bookRevisionId"], manifest.book_revision_id)

    def test_resume_publishes_and_projects_from_the_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, db_path, book_id = self.prepare(tmp)
            with patch("onebookwiki.generation.generate_response", side_effect=[response_for(root, 1)]):
                run_code = generate_wiki.main([
                    "generate_wiki.py", "chapter", str(root),
                    "--provider", "test", "--embedding-backend", "lexical",
                ])
            self.assertEqual(run_code, 0)
            latest = CheckpointStore.latest(root)
            self.assertIsNotNone(latest)
            with patch(
                "onebookwiki.generation.generate_response",
                side_effect=[rollup_response(root, 1), book_response(root, 1)],
            ):
                code = generate_wiki.main([
                    "generate_wiki.py", "resume", str(root),
                    "--provider", "test", "--embedding-backend", "lexical",
                    "--database", str(db_path), "--book-id", str(book_id),
                    "--run-id", latest.run_id,
                ])
            self.assertEqual(code, 0)
            manifest = Manifest.load(root)
            self.assertEqual(manifest.publication_health.get("status"), "healthy")
            self.assertTrue((root / "wiki" / "structure.json").is_file())


if __name__ == "__main__":
    unittest.main()
