import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.database import create_placeholder, init_db, refresh_token_usage


class ServerDatabaseTest(unittest.TestCase):
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
