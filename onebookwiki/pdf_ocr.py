"""Optional local PP-OCRv5 mobile assistance for PDF structure analysis.

This module is deliberately lazy: importing it does not import PaddleOCR or
PaddlePaddle. OCR is used only for an ephemeral structure-analysis view; the
native PyMuPDF page text remains the source of truth for exported chapters.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class PdfStructureOcrConfig:
    detector_model: str = "D:/models/PaddleOCR/PP-OCRv5_mobile_det_infer"
    recognizer_model: str = "D:/models/PaddleOCR/PP-OCRv5_mobile_rec_infer"
    device: str | None = None
    batch_size: int = 1
    dpi: int = 180
    confidence: float = 0.75
    max_pages: int = 12

    @classmethod
    def from_env(cls, *, dpi: int | None = None, confidence: float | None = None) -> "PdfStructureOcrConfig":
        def integer(name: str, default: int, minimum: int = 1) -> int:
            try:
                return max(minimum, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        def number(name: str, default: float) -> float:
            try:
                return min(1.0, max(0.0, float(os.getenv(name, str(default)))))
            except ValueError:
                return default

        return cls(
            detector_model=os.getenv("ONEBOOKWIKI_PDF_OCR_DET_MODEL", cls.detector_model),
            recognizer_model=os.getenv("ONEBOOKWIKI_PDF_OCR_REC_MODEL", cls.recognizer_model),
            device=os.getenv("ONEBOOKWIKI_PDF_OCR_DEVICE") or None,
            batch_size=integer("ONEBOOKWIKI_PDF_OCR_BATCH_SIZE", cls.batch_size),
            dpi=max(72, dpi if dpi is not None else integer("ONEBOOKWIKI_PDF_OCR_DPI", cls.dpi, 72)),
            confidence=min(1.0, max(0.0, confidence if confidence is not None else number("ONEBOOKWIKI_PDF_OCR_CONFIDENCE", cls.confidence))),
            max_pages=integer("ONEBOOKWIKI_PDF_OCR_MAX_PAGES", cls.max_pages),
        )

    def validate(self) -> None:
        for name, value in (("detector", self.detector_model), ("recognizer", self.recognizer_model)):
            path = Path(value).expanduser()
            if not path.is_dir():
                raise ValueError(
                    f"PP-OCRv5 {name} model directory does not exist: {path}. "
                    "Set the matching ONEBOOKWIKI_PDF_OCR_*_MODEL variable to a local directory."
                )
        if self.device and self.device.lower().startswith(("cuda", "gpu")):
            # Paddle accepts several GPU spellings; preserve them for the backend.
            return

    def report(self) -> dict[str, Any]:
        return {
            "engine": "PaddleOCR",
            "api_family": "3.x",
            "detector_model": "PP-OCRv5_mobile_det",
            "recognizer_model": "PP-OCRv5_mobile_rec",
            "detector_model_dir": self.detector_model,
            "recognizer_model_dir": self.recognizer_model,
            "device": self.device or "auto",
            "batch_size": self.batch_size,
            "dpi": self.dpi,
            "confidence": self.confidence,
            "max_pages": self.max_pages,
            "allow_download": False,
        }


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class OcrPageResult:
    page: int
    width: float
    height: float
    lines: tuple[OcrLine, ...]
    mean_confidence: float
    text_digest: str


class LocalPpOcrV5:
    """Small, local-only PP-OCRv5 det/rec adapter for selected PDF pages."""

    def __init__(self, config: PdfStructureOcrConfig | None = None):
        self.config = config or PdfStructureOcrConfig.from_env()
        self.config.validate()
        self.failures: dict[int, str] = {}
        try:
            from paddleocr import TextDetection, TextRecognition
        except ImportError as exc:
            raise ValueError(
                "PP-OCRv5 assistance requires paddleocr and paddlepaddle; "
                "install the optional pdf-ocr dependencies."
            ) from exc
        common: dict[str, Any] = {"enable_mkldnn": False}
        if self.config.device:
            common["device"] = self.config.device
        try:
            # PaddleOCR 3.x's all-in-one PaddleOCR pipeline can expect legacy
            # inference.pdmodel artifacts. The local PP-OCRv5 exports use the
            # current inference.json format, which the task APIs load directly.
            self.detector = TextDetection(
                model_name="PP-OCRv5_mobile_det",
                model_dir=self.config.detector_model,
                **common,
            )
            self.recognizer = TextRecognition(
                model_name="PP-OCRv5_mobile_rec",
                model_dir=self.config.recognizer_model,
                **common,
            )
        except Exception as exc:  # noqa: BLE001 - optional model boundary
            raise ValueError(f"Could not load local PP-OCRv5 models: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _box(box: Any) -> tuple[float, float, float, float] | None:
        try:
            items = list(box)
            if len(items) == 4 and all(not hasattr(item, "__len__") for item in items):
                return tuple(float(item) for item in items)  # type: ignore[return-value]
            points = [(float(item[0]), float(item[1])) for item in items]
            if points:
                return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)
        except (TypeError, ValueError, IndexError):
            return None
        return None

    @staticmethod
    def _crop(image: Any, box: Any) -> Any | None:
        bounds = LocalPpOcrV5._box(box)
        if bounds is None:
            return None
        x0, y0, x1, y1 = (int(round(value)) for value in bounds)
        height, width = image.shape[:2]
        x0, x1 = max(0, min(x0, width - 1)), max(0, min(x1, width - 1))
        y0, y1 = max(0, min(y0, height - 1)), max(0, min(y1, height - 1))
        if x1 <= x0 or y1 <= y0:
            return None
        return image[y0:y1 + 1, x0:x1 + 1]

    def run(self, source: Path, page_numbers: Iterable[int]) -> dict[int, OcrPageResult]:
        try:
            import pymupdf as fitz
            import numpy as np
        except ImportError as exc:
            raise ValueError("PP-OCRv5 assistance requires pymupdf and numpy") from exc
        output: dict[int, OcrPageResult] = {}
        document = fitz.open(source)
        scale = self.config.dpi / 72.0
        self.failures = {}
        for page_number in sorted(set(int(item) for item in page_numbers)):
            if page_number < 1 or page_number > len(document):
                continue
            try:
                page = document[page_number - 1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
                detections = self.detector.predict(image)
                detection = detections[0] if detections else {}
                boxes_value = detection.get("dt_polys")
                boxes = list(boxes_value) if boxes_value is not None else []
                crops: list[Any] = []
                usable_boxes: list[Any] = []
                for box in boxes:
                    crop = self._crop(image, box)
                    if crop is not None:
                        crops.append(crop)
                        usable_boxes.append(box)
                recognitions = self.recognizer.predict(crops, batch_size=self.config.batch_size) if crops else []
                lines: list[OcrLine] = []
                for box, recognition in zip(usable_boxes, recognitions):
                    text = " ".join(str(recognition.get("rec_text") or "").split()).strip()
                    confidence = float(recognition.get("rec_score") or 0.0)
                    bounds = self._box(box)
                    if not text or bounds is None:
                        continue
                    lines.append(OcrLine(text, confidence, *(coordinate / scale for coordinate in bounds)))
                lines.sort(key=lambda item: (item.y0, item.x0, item.text))
                text = "\n".join(item.text for item in lines)
                output[page_number] = OcrPageResult(
                    page_number,
                    float(page.rect.width),
                    float(page.rect.height),
                    tuple(lines),
                    sum(item.confidence for item in lines) / len(lines) if lines else 0.0,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            except Exception as exc:  # noqa: BLE001 - isolate a bad page from the assist pass
                self.failures[page_number] = f"{type(exc).__name__}: {exc}"
        return output


def candidate_pages(pages: list[Any], report: dict[str, Any], max_pages: int) -> list[int]:
    """Choose a prioritized, deterministic set of pages needing OCR assistance."""
    priorities: dict[int, int] = {}

    def add(page_number: Any, priority: int) -> None:
        try:
            number = int(page_number)
        except (TypeError, ValueError):
            return
        if 1 <= number <= len(pages):
            priorities[number] = max(priority, priorities.get(number, 0))

    methods = {str(item.get("method")): item for item in report.get("methods", []) if isinstance(item, dict)}
    toc = methods.get("toc", {})
    # Keep the entire front window eligible, but put likely contents pages first.
    if not toc.get("accepted") or float(toc.get("confidence", 1.0) or 0.0) < 0.82:
        for page in pages[:20]:
            lines = [str(line) for line in str(getattr(page, "text", "")).splitlines()]
            text = " ".join(lines)
            marker = any("目录" in line or "contents" in line.lower() for line in lines)
            toc_shape = bool(re.search(r"(?:/{1,}|[.·…]{2,}|\(\s*\d{1,4}\s*\)|\s{2,}\d{1,4}\s*$)", text))
            add(page.number, 100 if marker else 80 if toc_shape else 40)

    boundaries = report.get("content_boundaries", {})
    for item in list(boundaries.get("accepted", [])) + list(boundaries.get("rejected", [])):
        if not isinstance(item, dict):
            continue
        rejection = item.get("rejection")
        if rejection in {"insufficient_boundary_evidence", "repeated_page_start_heading", "page_label_suffix"}:
            add(item.get("page"), 65)

    for page in pages:
        text = " ".join(str(getattr(page, "text", "")).split())
        if len(text) < 24:
            add(getattr(page, "number", 0), 70)

    limit = max(1, int(max_pages))
    return [page for page, _ in sorted(priorities.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def overlay_pages(native_pages: list[Any], results: dict[int, OcrPageResult], *, minimum_confidence: float = 0.0) -> list[Any]:
    """Return a native-first structural view with deduplicated OCR evidence."""
    from .source_structure import PageBlock, PdfLine, PdfPage, PdfSpan, normalise

    output: list[Any] = []
    for page in native_pages:
        result = results.get(page.number)
        if result is None or not result.lines:
            output.append(page)
            continue

        native_lines = list(getattr(page, "lines", ()) or ())
        if not native_lines:
            native_lines = [PdfLine(text=line) for line in str(page.text).splitlines() if line.strip()]
        seen = {normalise(line.text) for line in native_lines if normalise(line.text)}
        added: list[PdfLine] = []
        for item in result.lines:
            signature = normalise(item.text)
            if not signature or signature in seen or item.confidence < minimum_confidence:
                continue
            seen.add(signature)
            added.append(PdfLine(
                text=item.text,
                x0=item.x0,
                y0=item.y0,
                x1=item.x1,
                y1=item.y1,
                size=0.0,
                spans=(PdfSpan(item.text, item.x0, item.y0, item.x1, item.y1),),
            ))
        if not added:
            output.append(page)
            continue

        merged_lines = tuple(sorted([*native_lines, *added], key=lambda item: (item.y0, item.x0, item.text)))
        ocr_block = PageBlock(
            text="\n".join(line.text for line in added),
            x0=min(line.x0 for line in added),
            y0=min(line.y0 for line in added),
            x1=max(line.x1 for line in added),
            y1=max(line.y1 for line in added),
            lines=tuple(added),
        )
        native_text = str(page.text).strip()
        ocr_text = "\n".join(line.text for line in added)
        output.append(PdfPage(
            page.number,
            f"{native_text}\n{ocr_text}".strip(),
            tuple(getattr(page, "blocks", ()) or ()) + (ocr_block,),
            page.height,
            page.width,
            merged_lines,
        ))
    return output
