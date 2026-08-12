"""Debug the book listing route."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
from server.database import init_db, list_books

db_path = Path("d:/workspace/deepwiki4book/onebookwiki/books/onebookwiki.db")
conn = init_db(str(db_path))

try:
    books = list_books(conn)
    print(f"Count: {len(books)}")
    for b in books[:2]:
        print(f"  {b['id']}: {b['title'][:50]} [{b['phase']}]")

    # Test JSON serialization
    result = {"books": books[:2]}
    json_str = json.dumps(result, ensure_ascii=False)
    print(f"\nJSON (first 200 chars): {json_str[:200]}")
    print("Serialization OK!")
finally:
    conn.close()
