# Contributing to OneBookWiki

Thank you for contributing! Here's how to get started.

## Development Setup

### Prerequisites
- Python 3.10+
- Node.js 22+
- (Optional) Docker for containerized development

### Install

```bash
# Clone the repository
git clone <repo-url>
cd onebookwiki

# Install Python package in development mode
pip install -e ".[server,imports,rag]"

# Install frontend dependencies
cd frontend && npm install && cd ..
```

### Run Development Server

**Backend (FastAPI):**
```bash
# Linux/macOS
./start.sh

# Windows PowerShell
.\start.ps1

# Windows CMD
start_server.bat
```

**Frontend (Vite dev server with HMR):**
```bash
cd frontend && npm run dev
```

The frontend dev server proxies `/api` requests to `localhost:8000` and serves book files from `../books/`.

## Running Tests

```bash
# Run all tests
python -m unittest discover -s tests -v

# Run a specific test file
python -m unittest tests.test_index -v
```

## Code Style

- Python: `from __future__ import annotations` at the top of every module
- Use `pathlib.Path` for all file system operations (never `os.path`)
- Type annotations are encouraged
- Keep imports organized: standard library → third-party → project

## Project Structure

```
onebookwiki/
├── onebookwiki/       # Core Python library
│   ├── cli/           # CLI entry points (console_scripts)
│   ├── *.py           # Library modules
├── server/            # FastAPI backend
│   ├── main.py        # App entry point
│   ├── database.py    # SQLite layer
│   ├── pipeline.py    # Background processing
│   └── routes/        # API route handlers
├── frontend/          # React + Vite frontend
│   └── src/app/       # React components
├── scripts/           # Legacy CLI scripts (thin wrappers)
├── references/        # Wiki generation templates
├── tests/             # Python tests
└── examples/          # Example book projects
```

## Adding a New Import Format

1. Add the format detection logic in `onebookwiki/importers.py`
2. Add any optional dependency to `pyproject.toml` under `[project.optional-dependencies]`
3. Add it to the `imports` group for the full installer
4. Document the format in README.md

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
