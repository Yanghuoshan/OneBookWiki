import unittest

from onebookwiki.pdf_postprocess import postprocess_pdf_pages
from onebookwiki.source_structure import PdfLine, PdfPage, PdfSpan


def line(text, x0, y0, x1=280, y1=None, size=10):
    y1 = y1 if y1 is not None else y0 + size
    span = PdfSpan(text, x0, y0, x1, y1, size=size, font="Test")
    return PdfLine(text, x0, y0, x1, y1, size=size, spans=(span,))


def page(number, lines, width=300, height=1000):
    return PdfPage(number, "\n".join(item.text for item in lines), height=height, width=width, lines=tuple(lines))


class PdfPostprocessTest(unittest.TestCase):
    def test_cjk_spacing_and_english_hyphen_wrap(self):
        pages = [page(1, [
            line("这 是 一 个 测 试。", 30, 100, 270),
            line("capital-", 30, 120, 140),
            line("ism works.", 30, 132, 140),
        ])]
        result = postprocess_pdf_pages(pages, mode="auto")
        self.assertIn("这是一个测试。", result.pages[0].text)
        self.assertIn("capitalism works.", result.pages[0].text)
        self.assertEqual(result.report["structure_view"], "native")
        self.assertEqual(result.report["payload_view"], "postprocessed")

    def test_page_labels_and_repeated_headers_are_removed(self):
        pages = [page(index, [
            line("Running Header", 30, 20, 150),
            line(str(index), 140, 950, 150),
            line(f"正文第 {index} 页。", 30, 100, 260),
        ]) for index in range(1, 6)]
        result = postprocess_pdf_pages(pages, mode="auto")
        self.assertTrue(all("Running Header" not in item.text for item in result.pages))
        self.assertTrue(all("\n1\n" not in f"\n{item.text}\n" for item in result.pages))
        self.assertIn("正文第 1 页。", result.pages[0].text)
        self.assertGreater(result.report["removed"]["running_headers"], 0)
        self.assertGreater(result.report["removed"]["page_labels"], 0)

    def test_two_columns_are_ordered_left_then_right(self):
        pages = [page(1, [
            line("Left first", 20, 100, 120),
            line("Right first", 180, 100, 280),
            line("Left second", 20, 120, 120),
            line("Right second", 180, 120, 280),
            line("Left third", 20, 140, 120),
            line("Right third", 180, 140, 280),
        ])]
        result = postprocess_pdf_pages(pages, mode="auto")
        text = result.pages[0].text
        self.assertLess(text.index("Left first"), text.index("Left second"))
        self.assertLess(text.index("Left third"), text.index("Right first"))
        self.assertEqual(result.report["columns"]["two_column_pages"], 1)

    def test_layout_unavailable_keeps_injected_pages_unchanged(self):
        pages = [PdfPage(1, "OCR 原始 文本"), PdfPage(2, "second page")]
        result = postprocess_pdf_pages(pages, mode="auto")
        self.assertEqual(tuple(result.pages), tuple(pages))
        self.assertEqual(result.report["reason"], "layout-unavailable")

    def test_empty_page_does_not_disable_other_layout_pages(self):
        pages = [
            page(1, [line("这 是 正 文。", 30, 100, 260)]),
            PdfPage(2, ""),
        ]
        result = postprocess_pdf_pages(pages, mode="auto")
        self.assertTrue(result.report["applied"])
        self.assertIn("这是正文。", result.pages[0].text)
        self.assertEqual(result.pages[1], pages[1])

    def test_report_and_output_are_deterministic(self):
        pages = [page(1, [line("A paragraph.", 20, 100, 160), line("continues.", 20, 112, 160)])]
        first = postprocess_pdf_pages(pages, mode="auto")
        second = postprocess_pdf_pages(pages, mode="auto")
        self.assertEqual(first.pages, second.pages)
        self.assertEqual(first.report, second.report)


if __name__ == "__main__":
    unittest.main()
