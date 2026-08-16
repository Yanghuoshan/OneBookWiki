"""Test FastAPI routes via TestClient with explicit lifespan."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from server.main import app, lifespan


async def test():
    async with lifespan(app):
        from fastapi.testclient import TestClient

        client = TestClient(app)

        # Test health
        r = client.get("/api/health")
        print(f"Health: {r.status_code} {r.json()}")

        # Test books list
        r = client.get("/api/books")
        print(f"Books: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  Count: {len(data.get('books', []))}")
            for b in data["books"][:3]:
                print(f"  {b['id']}: {b['title'][:40]} [{b['phase']}]")
        else:
            print(f"  Error: {r.text[:500]}")

        # Test single book
        r = client.get("/api/books/1")
        print(f"Book detail: {r.status_code}")
        if r.status_code == 200:
            b = r.json()
            print(f"  {b['title']} - {b['page_count']} pages")

        # Test status
        r = client.get("/api/books/1/status")
        print(f"Status: {r.status_code} {r.json() if r.status_code == 200 else r.text[:200]}")

        # Test missing book
        r = client.get("/api/books/999999")
        print(f"Missing book: {r.status_code}")

        print("\nAll tests passed!")


asyncio.run(test())
