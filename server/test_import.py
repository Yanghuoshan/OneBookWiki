"""Quick test script to verify server imports and DB initialization."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.main import app
print("FastAPI app imported successfully")

from server.database import init_db

DB_PATH = Path("books/onebookwiki.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
conn = init_db(str(DB_PATH))
print("DB init OK")

books = conn.execute(
    "SELECT id, title, phase FROM books ORDER BY created_at DESC"
).fetchall()
print("Books in DB:")
for row in books:
    print(f"  {row[0]}: {row[1]} [{row[2]}]")

conn.close()
print("All OK!")
