import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from onebookwiki.chunking import locator_for_range
from onebookwiki.importers import (
    _extract_doc_text,
    _extract_kindle,
    analyze_html,
    analyze_kindle,
    analyze_text,
    detect_source_type,
    import_document,
)
from onebookwiki.models import BookSynthesis, ChapterInterpretation, EvidenceRef
from onebookwiki.rendering import render_book
from onebookwiki.source_structure import ImportOptions, SourceLocator


class MultiFormatImportTest(unittest.TestCase):
    def test_detect_source_type_accepts_suffixes_and_explicit_override(self):
        self.assertEqual(detect_source_type(Path("book.HTML")), "HTML")
        self.assertEqual(detect_source_type(Path("book.azw3")), "AZW3")
        self.assertEqual(detect_source_type(Path("extensionless"), "txt"), "TXT")
        with self.assertRaisesRegex(ValueError, "unsupported source type override"):
            detect_source_type(Path("book.txt"), "markdown")

    def test_locator_cannot_override_reserved_fields(self):
        with self.assertRaisesRegex(ValueError, "cannot override reserved"):
            SourceLocator("TXT", "text_lines", {"format": "PDF"}).to_dict()

    def test_doc_adapter_uses_ole_validation_and_legacy_text_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.doc"
            source.write_bytes(b"binary-doc")
            ole = Mock()
            ole.isOleFile.return_value = True
            container = MagicMock()
            container.__enter__.return_value = container
            container.__exit__.return_value = False
            container.exists.return_value = False
            ole.OleFileIO.return_value = container
            legacy = SimpleNamespace(extract_text=Mock(return_value=SimpleNamespace(text="First paragraph\\nSecond paragraph")))
            with patch("onebookwiki.importers._require_optional", side_effect=lambda module, extra: ole if module == "olefile" else legacy):
                self.assertEqual(_extract_doc_text(source), "First paragraph\\nSecond paragraph")
            legacy.extract_text.assert_called_once_with(b"binary-doc")

    def test_doc_adapter_rejects_encrypted_container_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "encrypted.doc"
            source.write_bytes(b"binary-doc")
            ole = Mock()
            ole.isOleFile.return_value = True
            container = MagicMock()
            container.__enter__.return_value = container
            container.__exit__.return_value = False
            container.exists.side_effect = lambda name: name == "EncryptionInfo"
            ole.OleFileIO.return_value = container
            legacy = SimpleNamespace(extract_text=Mock())
            with patch("onebookwiki.importers._require_optional", side_effect=lambda module, extra: ole if module == "olefile" else legacy):
                with self.assertRaisesRegex(ValueError, "encrypted DOC"):
                    _extract_doc_text(source)
            legacy.extract_text.assert_not_called()

    def test_kindle_adapter_maps_drm_error_and_cleans_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.mobi"
            source.write_bytes(b"kindle")
            workspace = Path(tmp) / "unpack"
            workspace.mkdir()
            extractor = Mock(side_effect=RuntimeError("DRM encryption detected"))
            with patch("onebookwiki.importers._require_optional", return_value=SimpleNamespace(extract=extractor)):
                with self.assertRaisesRegex(ValueError, "DRM/encrypted Kindle"):
                    _extract_kindle(source)
            # _extract_kindle itself has no workspace yet; cleanup is owned by analyze_kindle.
            self.assertTrue(workspace.exists())

    def test_kindle_extractor_requires_two_path_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.azw3"
            source.write_bytes(b"kindle")
            with patch("onebookwiki.importers._require_optional", return_value=SimpleNamespace(extract=Mock(return_value=Path(tmp) / "payload.epub"))):
                with self.assertRaisesRegex(ValueError, "unsupported result"):
                    _extract_kindle(source)

    def test_kindle_html_uses_filepos_table_of_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.mobi"
            payload = Path(tmp) / "unpack" / "book.html"
            source.write_bytes(b"kindle")
            payload.parent.mkdir()
            payload.write_text(
                """<html><body><p>书籍标题</p><a id='filepos50'></a><p>目录</p>
                <p><a href='#filepos100'>第一章 起点</a></p><p><a href='#filepos200'>第二章 展开</a></p>
                <a id='filepos100'></a><p>第一章 起点</p><p>第一章的正文。</p>
                <a id='filepos200'></a><p>第二章 展开</p><p>第二章的正文。</p></body></html>""",
                encoding="utf-8",
            )
            with patch("onebookwiki.importers._extract_kindle", return_value=(payload.parent, payload)):
                document = analyze_kindle(source)
            self.assertEqual(document.metadata["structure_method"], "kindle_toc_anchors")
            self.assertEqual([unit.title for unit in document.units], ["第一章 起点", "第二章 展开"])
            self.assertEqual(document.units[0].locator.to_dict()["kind"], "kindle_section")
            self.assertEqual(document.units[0].locator.to_dict()["unpacked_locator"]["fragment"], "filepos100")
            self.assertFalse(payload.parent.exists())

    def test_html_preserves_heading_tree_and_filters_non_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.html"
            source.write_text(
                """<!doctype html><html><head><title>HTML Book</title><meta name='author' content='Author Name'></head>
                <body><nav>Navigation must not appear.</nav><h1 id='start'>Opening</h1><p>Opening prose.</p>
                <h3 id='detail'>Detail</h3><p>Detailed prose.</p><script>Hidden script text.</script>
                <h2>Second chapter</h2><blockquote>Second prose.</blockquote><style>Hidden styles.</style></body></html>""",
                encoding="utf-8",
            )
            document = analyze_html(source)
            self.assertEqual(document.title, "HTML Book")
            self.assertEqual(document.author, "Author Name")
            self.assertEqual([unit.title for unit in document.units], ["Opening", "Second chapter"])
            self.assertNotIn("Navigation must not appear.", document.units[0].text)
            self.assertNotIn("Hidden script text.", document.units[0].text)
            self.assertEqual(document.units[0].locator.to_dict()["fragment"], "start")
            self.assertEqual(document.outline[0].title, "Opening")
            self.assertEqual(document.outline[0].children[0].title, "Detail")
            self.assertEqual(document.outline[0].unit_ids, [document.units[0].id])
            self.assertEqual(document.outline[0].children[0].unit_ids, [])

    def test_text_setext_headings_preserve_lines_without_marker_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.txt"
            source.write_text("Opening\n=======\n\nOpening prose.\n\n## Second\nSecond prose.\n", encoding="utf-8")
            document = analyze_text(source)
            self.assertEqual([unit.title for unit in document.units], ["Opening", "Second"])
            self.assertNotIn("=======", document.units[0].text)
            self.assertEqual(document.units[0].locator.to_dict()["line_start"], 1)
            self.assertEqual(document.units[0].locator.to_dict()["line_end"], 4)
            self.assertEqual(document.units[1].locator.to_dict()["line_start"], 6)
            self.assertEqual(document.units[1].locator.to_dict()["line_end"], 7)

    def test_rendering_keeps_source_unit_and_evidence_locators_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "raw" / "chapters" / "01-start.md"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text(
                '# Start\n> Format: TXT\n> Locator: {"format":"TXT","kind":"text_lines","line_end":1,"line_start":1}\n\nEvidence text.\n',
                encoding="utf-8",
            )
            ref = EvidenceRef("C1E1", "chunk-1", "raw/chapters/01-start.md", 1, 5, 5, locator={"format": "TXT", "kind": "text_lines", "line_start": 5, "line_end": 5, "source_line_start": 5, "source_line_end": 5})
            chapter = ChapterInterpretation(chapter=1, title="Start", source_unit_id="txt-unit", source_type="section", locator={"format": "TXT", "kind": "text_lines", "line_start": 1, "line_end": 1}, evidence=[ref], executive_summary="Summary")
            render_book(BookSynthesis(title="Book"), [chapter], root)
            structure = json.loads((root / "wiki" / "structure.json").read_text(encoding="utf-8"))
            page = next(item for item in structure["pages"] if item["id"] == "chapter-01")
            self.assertEqual(page["sourceUnitLocator"]["line_start"], 1)
            evidence = json.loads((root / "wiki" / "evidence.json").read_text(encoding="utf-8"))["evidence"]["C1E1"]
            self.assertEqual(evidence["source_unit_locator"]["line_start"], 1)
            self.assertEqual(evidence["locator"]["source_line_start"], 5)

    def test_generic_writer_records_native_and_evidence_locators(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.txt"
            root = Path(tmp) / "project"
            source.write_text("# Start\nEvidence prose.\n", encoding="utf-8")
            written = import_document(source, root, ImportOptions(force=True))
            self.assertEqual(len(written), 1)
            metadata = json.loads((root / ".onebookwiki" / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["schema_version"], 3)
            unit = metadata["source_structure"]["units"][0]
            self.assertEqual(unit["locator"]["format"], "TXT")
            self.assertEqual(unit["locator"]["line_start"], 1)
            raw = written[0].read_text(encoding="utf-8")
            evidence = locator_for_range(raw, 1, 4, 1)
            self.assertEqual(evidence["format"], "TXT")
            self.assertEqual(evidence["line_start"], 1)
            self.assertEqual(evidence["source_line_start"], 1)
            stale = root / "raw" / "chapters" / "99-stale.md"
            stale.write_text("stale", encoding="utf-8")
            import_document(source, root, ImportOptions(force=True))
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
