"""Test script to verify static file mounting in production mode."""
import os
from pathlib import Path

# Simulate production environment
os.environ["ONEBOOKWIKI_ENV"] = "production"

from server.config import PROJECT_ROOT

print(f"PROJECT_ROOT: {PROJECT_ROOT}")
print(f"Exists: {PROJECT_ROOT.exists()}")

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
print(f"\nFRONTEND_DIST: {FRONTEND_DIST}")
print(f"Exists: {FRONTEND_DIST.exists()}")
print(f"Is directory: {FRONTEND_DIST.is_dir()}")

if FRONTEND_DIST.is_dir():
    print("\nContents of frontend/dist:")
    for item in FRONTEND_DIST.iterdir():
        print(f"  - {item.name} ({'dir' if item.is_dir() else 'file'})")

_assets_dir = FRONTEND_DIST / "assets"
print(f"\nASSETS_DIR: {_assets_dir}")
print(f"Exists: {_assets_dir.exists()}")
print(f"Is directory: {_assets_dir.is_dir()}")

if _assets_dir.is_dir():
    print("\nContents of assets:")
    for item in _assets_dir.iterdir():
        print(f"  - {item.name} ({item.stat().st_size} bytes)")

# Test if the server can import and mount correctly
print("\n" + "="*60)
print("Testing FastAPI app initialization...")
print("="*60)

try:
    from server.main import app
    print("\n✅ App imported successfully")

    # Check routes
    print("\nMounted routes:")
    for route in app.routes:
        if hasattr(route, 'path'):
            print(f"  - {route.path}")

    # Test with a test client
    from fastapi.testclient import TestClient
    client = TestClient(app)

    print("\nTesting endpoints:")

    # Test root
    response = client.get("/")
    print(f"  GET / -> {response.status_code}")

    # Test assets (use actual filename from dist)
    asset_files = list(_assets_dir.glob("*.js")) if _assets_dir.is_dir() else []
    if asset_files:
        asset_name = asset_files[0].name
        response = client.get(f"/assets/{asset_name}")
        print(f"  GET /assets/{asset_name} -> {response.status_code}")
        if response.status_code != 200:
            print(f"    ❌ Expected 200, got {response.status_code}")
            print(f"    Response: {response.text[:200]}")

    # Test API
    response = client.get("/api/health")
    print(f"  GET /api/health -> {response.status_code}")

except Exception as e:
    print(f"\n❌ Error: {e}")
    import traceback
    traceback.print_exc()
