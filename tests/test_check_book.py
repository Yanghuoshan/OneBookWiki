import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CHECKER = PACKAGE_ROOT / "scripts" / "check_book.py"
INDEXER = PACKAGE_ROOT / "scripts" / "ingest_book.py"
EXAMPLE = PACKAGE_ROOT / "examples" / "sample-book"


def run(command, root, *args):
    return subprocess.run([sys.executable, str(command), str(root), *args], capture_output=True, text=True)


class BookCheckerTest(unittest.TestCase):
    def copy_example(self, root: Path):
        for source in EXAMPLE.rglob("*"):
            if source.is_file():
                target = root / source.relative_to(EXAMPLE)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def test_example_indexes_and_checks_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            indexed = subprocess.run([sys.executable, str(INDEXER), "index", str(root), "--backend", "lexical"], capture_output=True, text=True)
            self.assertEqual(indexed.returncode, 0, indexed.stderr)
            checked = run(CHECKER, root)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("## Summary\n0 issue(s)", checked.stdout)

    def test_duplicate_chapter_and_fabricated_quote_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            duplicate = root / "wiki" / "chapters" / "01-copy.md"
            duplicate.write_text((root / "wiki" / "chapters" / "01-attention.md").read_text(encoding="utf-8"), encoding="utf-8")
            original = root / "wiki" / "chapters" / "02-memory.md"
            original.write_text(original.read_text(encoding="utf-8") + '\n> "This fabricated quote is not in the source."\n', encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertIn("duplicate chapter number: 1", checked.stdout)
            self.assertIn("fabricated quote", checked.stdout)

    def test_stale_manifest_is_reported_after_raw_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            subprocess.run([sys.executable, str(INDEXER), "index", str(root), "--backend", "lexical"], check=True, capture_output=True, text=True)
            raw = root / "raw" / "chapters" / "01-attention.md"
            raw.write_text(raw.read_text(encoding="utf-8") + "\nChanged source.\n", encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertIn("stale chapter hash: raw/chapters/01-attention.md", checked.stdout)

    def test_pdf_postprocess_report_contract_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            metadata = root / ".onebookwiki" / "source.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text('{"format": "PDF"}', encoding="utf-8")
            (root / "wiki" / "structure.json").write_text('{"pages": [], "sections": [], "sourceOutline": []}', encoding="utf-8")
            report = metadata.parent / "structure-report.json"
            report.write_text('{"selected_method": "ranges", "ocr": "disabled", "postprocess": {"version": "layout-v1", "mode": "auto", "engine": "pymupdf-existing-text", "semantic_model": "none", "pages_processed": 3, "pages_changed": 2}}', encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertNotIn("postprocess", checked.stdout)
            report.write_text('{"selected_method": "ranges", "ocr": "disabled", "postprocess": {"mode": "auto", "engine": "model", "semantic_model": "enabled", "pages_processed": 1, "pages_changed": 2}}', encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertIn("structure report postprocess must use existing PDF text", checked.stdout)
            self.assertIn("structure report postprocess must declare no semantic model", checked.stdout)
            self.assertIn("structure report postprocess pages_changed exceeds pages_processed", checked.stdout)

    def test_pdf_structure_report_requires_explicit_ocr_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            structure = root / "wiki" / "structure.json"
            structure.write_text('{"pages": [], "sections": [], "sourceOutline": []}', encoding="utf-8")
            metadata = root / ".onebookwiki" / "source.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text('{"format": "PDF"}', encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertIn("PDF source is missing .onebookwiki/structure-report.json", checked.stdout)
            report = metadata.parent / "structure-report.json"
            report.write_text('{"selected_method": "ranges", "ocr": "enabled"}', encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertIn("structure report has invalid OCR mode", checked.stdout)

    def test_pdf_ocr_assist_provenance_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            structure = root / "wiki" / "structure.json"
            structure.write_text('{"pages": [], "sections": [], "sourceOutline": []}', encoding="utf-8")
            metadata_path = root / ".onebookwiki" / "source.json"
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            assist = {
                "mode": "assist", "role": "structure_auxiliary_only", "local_only": True,
                "status": "used", "trigger": "low_confidence", "candidate_pages": [2],
                "processed_pages": [2], "failed_pages": {}, "evidence_pages": [2], "selected": True,
            }
            metadata_path.write_text(json.dumps({"format": "PDF", "source_processing": {"structure_ocr": assist}}), encoding="utf-8")
            (metadata_path.parent / "structure-report.json").write_text(json.dumps({
                "selected_method": "toc", "ocr": "pp-ocrv5-mobile-assist", "ocr_assist": assist,
            }), encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertNotIn("OCR assist", checked.stdout)

    def test_pdf_manifest_snapshot_and_provenance_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.copy_example(root)
            structure = root / "wiki" / "structure.json"
            structure.write_text('{"pages": [], "sections": [], "sourceOutline": []}', encoding="utf-8")
            metadata_path = root / ".onebookwiki" / "source.json"
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema_version": 1,
                "id": "fixture",
                "status": "pending-readable-source",
                "source": {"filename": "fixture.pdf", "page_count": 3, "sha256": "f" * 64},
            }
            manifest_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            provenance = {
                "id": "fixture",
                "status": "pending-readable-source",
                "hash": manifest_hash,
                "source_match": {"filename": True, "page_count": True, "sha256": True},
                "application_mode": "rules_only",
                "snapshot_path": ".onebookwiki/structure-manifest.json",
            }
            metadata_path.write_text(json.dumps({
                "format": "PDF",
                "source_name": "fixture.pdf",
                "source_hash": "f" * 64,
                "page_count": 3,
                "source_structure": {"structure_manifest": provenance},
            }), encoding="utf-8")
            report_path = metadata_path.parent / "structure-report.json"
            report_path.write_text(json.dumps({
                "selected_method": "ranges", "ocr": "disabled", "structure_manifest": provenance,
            }), encoding="utf-8")
            snapshot = metadata_path.parent / "structure-manifest.json"
            snapshot.write_text(json.dumps(manifest), encoding="utf-8")
            checked = run(CHECKER, root)
            self.assertNotIn("PDF structure manifest", checked.stdout)
            snapshot.unlink()
            checked = run(CHECKER, root)
            self.assertIn("PDF structure manifest snapshot is missing or unsafe", checked.stdout)


if __name__ == "__main__":
    unittest.main()
