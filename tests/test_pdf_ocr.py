import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onebookwiki.pdf_ocr import OcrLine, OcrPageResult, PdfStructureOcrConfig, candidate_pages, overlay_pages
from onebookwiki.source_structure import PageBlock, PdfLine, PdfPage


class PdfOcrTest(unittest.TestCase):
    def test_config_reads_environment_variables(self):
        with patch.dict(os.environ, {
            "ONEBOOKWIKI_PDF_OCR_DET_MODEL": "D:/models/det",
            "ONEBOOKWIKI_PDF_OCR_REC_MODEL": "D:/models/rec",
            "ONEBOOKWIKI_PDF_OCR_DEVICE": "cpu",
            "ONEBOOKWIKI_PDF_OCR_BATCH_SIZE": "4",
            "ONEBOOKWIKI_PDF_OCR_DPI": "200",
            "ONEBOOKWIKI_PDF_OCR_CONFIDENCE": "0.8",
            "ONEBOOKWIKI_PDF_OCR_MAX_PAGES": "7",
        }):
            config = PdfStructureOcrConfig.from_env()
        self.assertEqual(config.detector_model, "D:/models/det")
        self.assertEqual(config.recognizer_model, "D:/models/rec")
        self.assertEqual(config.device, "cpu")
        self.assertEqual(config.batch_size, 4)
        self.assertEqual(config.dpi, 200)
        self.assertEqual(config.confidence, 0.8)
        self.assertEqual(config.max_pages, 7)

    def test_missing_local_model_is_reported_without_importing_paddle(self):
        config = PdfStructureOcrConfig("D:/missing-det", "D:/missing-rec")
        with self.assertRaisesRegex(ValueError, "model directory does not exist"):
            config.validate()

    def test_candidate_pages_prioritise_toc_shape_before_front_window(self):
        pages = [PdfPage(index, "正文") for index in range(1, 21)]
        pages[14] = PdfPage(15, "第一章 起点........(12)\n第二章 继续........(20)")
        report = {"methods": [{"method": "toc", "accepted": False, "confidence": 0.2}], "content_boundaries": {}}
        selected = candidate_pages(pages, report, 3)
        self.assertEqual(selected[0], 15)

    def test_overlay_preserves_native_lines_and_deduplicates(self):
        native_line = PdfLine("第一章 起点", 10, 10, 100, 20)
        native = [PdfPage(1, "第一章 起点", (PageBlock("第一章 起点", lines=(native_line,)),), lines=(native_line,))]
        result = OcrPageResult(1, 200, 300, (
            OcrLine("第一章 起点", 0.99, 10, 10, 100, 20),
            OcrLine("正文补充", 0.99, 10, 40, 100, 50),
        ), 0.99, "digest")
        assisted = overlay_pages(native, {1: result})[0]
        self.assertEqual(assisted.text.splitlines(), ["第一章 起点", "正文补充"])
        self.assertEqual(len(assisted.lines), 2)
        self.assertEqual(len(assisted.blocks), 2)


if __name__ == "__main__":
    unittest.main()
