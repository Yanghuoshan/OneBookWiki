import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from onebookwiki.evidence_registry import register_project
from server.database import create_placeholder, init_db, publish_registry_snapshot, refresh_token_usage


class ServerDatabaseTest(unittest.TestCase):
    def test_grounded_v2_schema_is_complete_and_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = init_db(str(Path(tmp) / "onebookwiki.db"))
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                self.assertTrue({"book_revisions", "evidence_revisions", "statement_revisions", "support_edges", "evidence_closure", "answer_plans", "citation_snapshots"}.issubset(tables))
                columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_turns)")}
                self.assertTrue({"book_revision_id", "answer_plan_id", "citation_snapshot_id", "pinned_revision_status"}.issubset(columns))
            finally:
                conn.close()

    def test_registry_snapshot_pins_book_and_evidence_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = root / "raw" / "chapters" / "01-example.md"
            chapter.parent.mkdir(parents=True)
            chapter.write_text("# Example\n\n> Chapter: 1\n\nBody evidence.\n", encoding="utf-8")
            snapshot = register_project(root)
            conn = init_db(str(root / "onebookwiki.db"))
            try:
                book_id = create_placeholder(conn, "Registry book")
                publish_registry_snapshot(conn, book_id, snapshot)
                book = conn.execute("SELECT active_book_revision_id, contract_version FROM books WHERE id = ?", (book_id,)).fetchone()
                self.assertEqual((book[0], book[1]), (snapshot.book_revision_id, "grounded-v2"))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM book_revisions WHERE id = ?", (snapshot.book_revision_id,)).fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_revisions WHERE book_revision_id = ?", (snapshot.book_revision_id,)).fetchone()[0], len(snapshot.evidence))
            finally:
                conn.close()

    def test_schema_integrity_rejects_missing_v2_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "onebookwiki.db")
            conn = init_db(path)
            conn.execute("DROP TABLE citation_snapshots")
            conn.commit()
            conn.close()
            with self.assertRaises(RuntimeError):
                init_db(path)

    def test_numeric_ids_are_autoincremented_independently_of_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "onebookwiki.db"
            conn = init_db(str(db_path))
            try:
                first = create_placeholder(conn, title="Same title", source_name="one.epub")
                second = create_placeholder(conn, title="Same title", source_name="two.epub")

                self.assertEqual((first, second), (1, 2))
                rows = conn.execute("SELECT id, title FROM books ORDER BY id").fetchall()
                self.assertEqual([(row[0], row[1]) for row in rows], [(1, "Same title"), (2, "Same title")])
            finally:
                conn.close()

    def test_token_usage_refresh_reads_new_entries_and_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = init_db(str(root / "onebookwiki.db"))
            try:
                book_id = create_placeholder(conn, title="Token book")
                usage_file = root / str(book_id) / ".onebookwiki" / "usage.jsonl"
                usage_file.parent.mkdir(parents=True)
                usage_file.write_text(
                    "\n".join(
                        [
                            json.dumps({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}),
                            "not json",
                            json.dumps({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
                            json.dumps(["not", "an", "object"]),
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )

                self.assertEqual(refresh_token_usage(conn, root), 1)
                row = conn.execute(
                    "SELECT prompt_tokens, completion_tokens, total_tokens FROM books WHERE id = ?",
                    (book_id,),
                ).fetchone()
                self.assertEqual(tuple(row), (13, 6, 19))
                self.assertEqual(refresh_token_usage(conn, root), 0)

                with usage_file.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}) + "\n")

                self.assertEqual(refresh_token_usage(conn, root), 1)
                row = conn.execute(
                    "SELECT prompt_tokens, completion_tokens, total_tokens FROM books WHERE id = ?",
                    (book_id,),
                ).fetchone()
                self.assertEqual(tuple(row), (20, 7, 27))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
