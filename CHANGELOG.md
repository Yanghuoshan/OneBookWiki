# Changelog

All notable changes to OneBookWiki will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased

### Added
- Core library (`onebookwiki`) for book ingestion, chunking, indexing, and retrieval
- Support for PDF, EPUB, MOBI, TXT, DOC, DOCX, HTML source formats
- Lexical (keyword) and vector (BGE-M3 / ModelScope) embedding backends
- LLM-powered wiki generation with resumable checkpoints
- FastAPI backend server with file upload and background processing pipeline
- React + Vite frontend reader with admin dashboard
- SQLite database for book metadata and operation logs
- Evidence-grounded retrieval with citation tracking
- CLI scripts for the full pipeline: import → index → generate → render → check
- Cross-platform startup scripts (Linux, macOS, Windows)
- Docker support with multi-stage build
- CI/CD workflows for testing and Docker publishing
