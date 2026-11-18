"""OCR engine adapters and scanned document processing for enterprise document ingestion.

Provides:
- RapidOCR (ONNX runtime) and Tesseract engine implementations.
- Scanned page detection based on text density, image presence, and image coverage ratio.
- High-fidelity in-memory PDF page rasterization with configurable DPI.
- Accurate spatial bounding box projection from rendered pixel space to PDF point coordinates.
- Reading order reconstruction, heading classification, and paragraph grouping for OCR blocks.
- Isolated per-page error handling to prevent individual page failures from failing documents.
- 100% offline execution with zero external cloud API dependencies.
"""

import abc
import logging
import re
from typing import Any

import numpy as np
import pymupdf
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.types.coordinates import BoundingBox, ChunkLocator
from backend.ingestion.types.elements import (
    ExtractedElement,
    HeadingElement,
    PageElement,
    ParagraphElement,
    TextBlockElement,
)
from backend.ingestion.types.enums import ElementType

logger = logging.getLogger(__name__)

# Section heading regex patterns for OCR text classification
OCR_NUMBERED_HEADING_PATTERN = re.compile(
    r"^(?:(?:\d+\.)+\d*|[A-Z]\.|\b(?:Section|Chapter|Article|Appendix|Phần|Chương|Điều|Mục)\s+\d+)",
    re.IGNORECASE,
)
OCR_ALL_CAPS_HEADING_PATTERN = re.compile(
    r"^[A-Z0-9\s,:\-–—\.\(\)]{4,60}$",
)


def is_ocr_heading(text: str) -> bool:
    """Determine whether a recognized text line represents a section heading."""
    clean = text.strip()
    if not clean:
        return False
    if OCR_NUMBERED_HEADING_PATTERN.match(clean):
        return True
    if OCR_ALL_CAPS_HEADING_PATTERN.match(clean) and clean.isupper() and len(clean) >= 4:
        return True
    return False



class OCRBlock(BaseModel):
    """Raw text block recognized by an OCR engine with spatial and confidence metadata."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(..., description="Recognized textual content")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Normalized OCR confidence score [0.0, 1.0]")
    bbox: BoundingBox = Field(..., description="Bounding box in canonical PDF point coordinates")
    polygon: list[list[float]] | None = Field(
        default=None,
        description="Optional 4-point polygon coordinates in rendered pixel or point space",
    )
    page_number: int = Field(..., ge=1, description="1-based page number")
    reading_order: int | None = Field(default=None, description="Sequence index in reading order")


class OCREngine(abc.ABC):
    """Abstract base class for pluggable, offline OCR engines."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique engine identifier name (e.g. 'RapidOCR', 'Tesseract')."""
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """OCR engine version string."""
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether this OCR engine is currently installed and ready for inference."""
        raise NotImplementedError

    @abc.abstractmethod
    def extract_text(
        self,
        image: np.ndarray,
        page_num: int,
        scale_x: float,
        scale_y: float,
    ) -> list[OCRBlock]:
        """Recognize text and bounding boxes from an in-memory image array.

        Args:
            image: In-memory image numpy array in RGB format (shape: H x W x 3, dtype: uint8).
            page_num: 1-indexed page number being processed.
            scale_x: Horizontal scale factor (rendered_width / pdf_points_width).
            scale_y: Vertical scale factor (rendered_height / pdf_points_height).

        Returns:
            List of recognized OCRBlock objects with coordinates scaled to PDF points.
        """
        raise NotImplementedError


class RapidOCREngine(OCREngine):
    """Offline OCR engine leveraging RapidOCR with ONNX Runtime.

    Natively recognizes English, Latin alphabets, Vietnamese diacritics, and multi-language
    corporate documents completely offline without requiring network calls or system daemons.
    """

    def __init__(
        self,
        min_confidence: float | None = None,
        languages: str | None = None,
    ) -> None:
        """Initialize RapidOCR engine with configurable confidence threshold."""
        settings = get_ingestion_settings()
        self.min_confidence = min_confidence if min_confidence is not None else settings.ocr_min_confidence
        self.languages = languages or settings.ocr_languages
        self._engine: Any = None
        self._init_error: str | None = None

    @property
    def name(self) -> str:
        return "RapidOCR"

    @property
    def version(self) -> str:
        return "1.4.4"

    @property
    def is_available(self) -> bool:
        """Check if rapidocr_onnxruntime is importable."""
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_engine(self) -> Any:
        """Lazily initialize the RapidOCR engine singleton."""
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR

                # Initialize RapidOCR with default packaged models
                self._engine = RapidOCR()
            except Exception as e:
                self._init_error = str(e)
                logger.error("Failed to initialize RapidOCR engine: %s", e)
                raise RuntimeError(f"RapidOCR engine initialization failed: {e}") from e
        return self._engine

    def extract_text(
        self,
        image: np.ndarray,
        page_num: int,
        scale_x: float,
        scale_y: float,
    ) -> list[OCRBlock]:
        """Run RapidOCR inference and convert outputs to PDF point coordinates."""
        engine = self._get_engine()

        # RapidOCR accepts numpy array in RGB or BGR format
        try:
            results, _ = engine(image)
        except Exception as e:
            logger.error("RapidOCR inference failed on page %d: %s", page_num, e)
            raise RuntimeError(f"RapidOCR inference error on page {page_num}: {e}") from e

        if not results:
            return []

        blocks: list[OCRBlock] = []
        safe_scale_x = scale_x if scale_x > 0 else 1.0
        safe_scale_y = scale_y if scale_y > 0 else 1.0

        for item in results:
            if not item or len(item) < 3:
                continue
            box, text, score = item[0], item[1], item[2]

            clean_text = str(text).strip() if text else ""
            if not clean_text:
                continue

            conf = float(score) if score is not None else 0.0
            if conf < self.min_confidence:
                continue

            # Box is a 4-point polygon: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]] in rendered pixels
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            px_x0, px_x1 = min(xs), max(xs)
            px_y0, px_y1 = min(ys), max(ys)

            # Map from pixel space back to PDF point coordinate space
            pdf_x0 = round(px_x0 / safe_scale_x, 2)
            pdf_y0 = round(px_y0 / safe_scale_y, 2)
            pdf_x1 = round(px_x1 / safe_scale_x, 2)
            pdf_y1 = round(px_y1 / safe_scale_y, 2)

            bbox = BoundingBox(
                x0=pdf_x0,
                y0=pdf_y0,
                x1=pdf_x1,
                y1=pdf_y1,
                page_number=page_num,
            )

            # Normalized polygon in PDF point coordinates
            poly_points = [[round(pt[0] / safe_scale_x, 2), round(pt[1] / safe_scale_y, 2)] for pt in box]

            blocks.append(
                OCRBlock(
                    text=clean_text,
                    confidence=round(conf, 4),
                    bbox=bbox,
                    polygon=poly_points,
                    page_number=page_num,
                )
            )

        return blocks


class TesseractOCREngine(OCREngine):
    """Optional Tesseract OCR engine wrapper using pytesseract."""

    def __init__(
        self,
        min_confidence: float | None = None,
        languages: str | None = None,
    ) -> None:
        settings = get_ingestion_settings()
        self.min_confidence = min_confidence if min_confidence is not None else settings.ocr_min_confidence
        self.languages = languages or settings.ocr_languages

    @property
    def name(self) -> str:
        return "Tesseract"

    @property
    def version(self) -> str:
        return "5.x"

    @property
    def is_available(self) -> bool:
        """Check if pytesseract and tesseract executable are available."""
        try:
            import pytesseract
            # Test getting version without exception
            pytesseract.get_tesseract_version()
            return True
        except Exception:
            return False

    def extract_text(
        self,
        image: np.ndarray,
        page_num: int,
        scale_x: float,
        scale_y: float,
    ) -> list[OCRBlock]:
        """Run Tesseract OCR inference on image array."""
        if not self.is_available:
            raise RuntimeError("Tesseract binary is not installed or not in PATH.")

        import pytesseract

        # Tesseract language mapping
        lang_arg = self.languages.replace("+", "+")

        try:
            data = pytesseract.image_to_data(
                image,
                lang=lang_arg,
                output_type=pytesseract.Output.DICT,
            )
        except Exception as e:
            logger.error("Tesseract inference failed on page %d: %s", page_num, e)
            raise RuntimeError(f"Tesseract OCR error on page {page_num}: {e}") from e

        blocks: list[OCRBlock] = []
        safe_scale_x = scale_x if scale_x > 0 else 1.0
        safe_scale_y = scale_y if scale_y > 0 else 1.0

        n_boxes = len(data.get("text", []))
        for i in range(n_boxes):
            text = data["text"][i].strip()
            conf_val = float(data["conf"][i])
            if not text or conf_val < 0:
                continue

            conf = round(conf_val / 100.0, 4)
            if conf < self.min_confidence:
                continue

            x = data["left"][i]
            y = data["top"][i]
            w = data["width"][i]
            h = data["height"][i]

            pdf_x0 = round(x / safe_scale_x, 2)
            pdf_y0 = round(y / safe_scale_y, 2)
            pdf_x1 = round((x + w) / safe_scale_x, 2)
            pdf_y1 = round((y + h) / safe_scale_y, 2)

            bbox = BoundingBox(
                x0=pdf_x0,
                y0=pdf_y0,
                x1=pdf_x1,
                y1=pdf_y1,
                page_number=page_num,
            )

            blocks.append(
                OCRBlock(
                    text=text,
                    confidence=conf,
                    bbox=bbox,
                    page_number=page_num,
                )
            )

        return blocks


# Engine instance cache
_ENGINE_CACHE: dict[str, OCREngine] = {}


def get_ocr_engine(
    engine_name: str | None = None,
    min_confidence: float | None = None,
    languages: str | None = None,
) -> OCREngine:
    """Resolve and provide a cached OCR engine instance based on configuration.

    Args:
        engine_name: 'rapidocr' or 'tesseract' (default resolved from IngestionSettings).
        min_confidence: Minimum confidence score threshold.
        languages: OCR languages string (e.g. 'eng+vie').

    Returns:
        Configured OCREngine instance.
    """
    settings = get_ingestion_settings()
    name = (engine_name or settings.ocr_engine).strip().lower()
    cache_key = f"{name}_{min_confidence}_{languages}"

    if cache_key in _ENGINE_CACHE:
        return _ENGINE_CACHE[cache_key]

    if name in ("rapidocr", "rapid_ocr", "default"):
        engine: OCREngine = RapidOCREngine(min_confidence=min_confidence, languages=languages)
    elif name == "tesseract":
        tess = TesseractOCREngine(min_confidence=min_confidence, languages=languages)
        if not tess.is_available:
            logger.warning("Tesseract requested but not available; falling back to RapidOCR.")
            engine = RapidOCREngine(min_confidence=min_confidence, languages=languages)
        else:
            engine = tess
    else:
        logger.warning("Unknown OCR engine '%s'; defaulting to RapidOCR.", name)
        engine = RapidOCREngine(min_confidence=min_confidence, languages=languages)

    _ENGINE_CACHE[cache_key] = engine
    return engine


class ScannedPageDetector:
    """Detects whether a PDF page is scanned or image-based using multi-criteria inspection."""

    def __init__(
        self,
        scanned_threshold_chars: int | None = None,
        image_coverage_threshold: float | None = None,
    ) -> None:
        """Initialize detector with configurable thresholds from settings."""
        settings = get_ingestion_settings()
        self.scanned_threshold_chars = (
            scanned_threshold_chars
            if scanned_threshold_chars is not None
            else settings.ocr_scanned_char_threshold
        )
        self.image_coverage_threshold = (
            image_coverage_threshold
            if image_coverage_threshold is not None
            else settings.ocr_image_coverage_threshold
        )

    def detect(
        self,
        page: pymupdf.Page,
        digital_char_count: int,
        has_tables: bool = False,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Inspect page characteristics and determine if OCR should be applied.

        Criteria:
        1. If digital text exceeds threshold and tables exist: definitely native.
        2. If digital text is below threshold:
           - Check for embedded raster images (`page.get_images()`).
           - Compute image surface coverage ratio against total page area.
           - If image coverage exceeds threshold or page has zero digital text: flagged as scanned.

        Returns:
            Tuple of (is_scanned: bool, reason: str, metrics: dict).
        """
        page_rect = page.rect
        page_area = max(page_rect.width * page_rect.height, 1.0)

        # 1. Inspect embedded raster images
        try:
            image_list = page.get_images(full=True)
            image_count = len(image_list)
        except Exception:
            image_list = []
            image_count = 0

        # 2. Compute approximate image area coverage on page
        total_image_area = 0.0
        try:
            for img_info in image_list:
                xref = img_info[0]
                rects = page.get_image_rects(xref)
                for r in rects:
                    total_image_area += r.width * r.height
        except Exception:
            pass

        image_coverage = min(round(total_image_area / page_area, 4), 1.0)

        metrics = {
            "digital_char_count": digital_char_count,
            "char_threshold": self.scanned_threshold_chars,
            "image_count": image_count,
            "image_coverage": image_coverage,
            "has_tables": has_tables,
        }

        # If page has native digital tables and plenty of text, it's digital
        if has_tables and digital_char_count >= self.scanned_threshold_chars:
            return False, "Page has native tables and sufficient digital text.", metrics

        # If digital text exceeds threshold comfortably, it's native
        if digital_char_count >= self.scanned_threshold_chars:
            return False, f"Page has sufficient digital text ({digital_char_count} chars).", metrics

        # Low text count branch (< scanned_threshold_chars)
        if digital_char_count == 0:
            if image_count > 0:
                return (
                    True,
                    f"Scanned page: 0 digital characters detected with {image_count} embedded image(s) (coverage: {image_coverage:.1%}).",
                    metrics,
                )
            return (
                True,
                "Potential blank or vector-rendered page: 0 digital characters detected.",
                metrics,
            )

        # Few characters present (e.g. running header or page number watermark), but large image exists
        if image_count > 0 and image_coverage >= self.image_coverage_threshold:
            return (
                True,
                f"Scanned page with minor text: only {digital_char_count} chars but image coverage is {image_coverage:.1%}.",
                metrics,
            )

        if digital_char_count < self.scanned_threshold_chars:
            return (
                True,
                f"Low extractable text detected ({digital_char_count} < {self.scanned_threshold_chars} chars).",
                metrics,
            )

        return False, "Digital text page.", metrics


def render_pdf_page_to_image(
    page: pymupdf.Page,
    dpi: int = 300,
) -> tuple[np.ndarray, float, float]:
    """Render a PDF page into an in-memory RGB numpy array with configurable DPI.

    Args:
        page: PyMuPDF Page instance.
        dpi: Target rasterization dots per inch (default: 300).

    Returns:
        Tuple of (image_array, scale_x, scale_y).
    """
    safe_dpi = max(72, min(dpi, 600))
    zoom = safe_dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)

    # Render page directly to pixmap without alpha channel
    pix = page.get_pixmap(matrix=matrix, alpha=False)

    # Direct in-memory buffer zero-copy conversion to numpy array
    img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))

    scale_x = pix.width / page.rect.width if page.rect.width > 0 else 1.0
    scale_y = pix.height / page.rect.height if page.rect.height > 0 else 1.0

    return img_array, scale_x, scale_y


def sort_and_classify_ocr_blocks(
    blocks: list[OCRBlock],
    page_num: int,
    current_heading: str | None,
    page_width: float,
    page_height: float,
    engine_name: str,
) -> tuple[list[ExtractedElement], str | None]:
    """Reconstruct reading order and classify recognized OCR blocks into structured elements.

    Preserves:
    - Top-to-bottom, left-to-right reading order (with multi-column grouping).
    - Line grouping into paragraphs when vertically adjacent and horizontally aligned.
    - Heading detection from structural numbering and prominence patterns.
    - Full locator coordinates with BoundingBox mapped to PDF points.
    - OCR confidence scores stored in element metadata.

    Returns:
        Tuple of (list_of_extracted_elements, next_section_heading).
    """
    if not blocks:
        return [], current_heading

    # 1. Spatial sorting into natural reading order:
    # Sort primarily by vertical position (y0), with horizontal tolerance band grouping
    sorted_blocks = sorted(
        blocks,
        key=lambda b: (round(b.bbox.y0 / 12.0) * 12.0, b.bbox.x0),
    )

    # 2. Group adjacent lines into cohesive paragraphs or heading elements
    elements: list[ExtractedElement] = []
    heading_tracker = current_heading

    # Group blocks that belong to the same paragraph
    # Two blocks are grouped if:
    # - vertical gap < 1.5 * line_height
    # - neither block is a detected heading
    grouped_units: list[list[OCRBlock]] = []
    current_group: list[OCRBlock] = []

    for b in sorted_blocks:
        # Check if block is a heading candidate
        is_heading_candidate = is_ocr_heading(b.text)

        if not current_group:
            current_group.append(b)
            if is_heading_candidate:
                grouped_units.append(current_group)
                current_group = []
            continue

        prev = current_group[-1]
        line_height = max(prev.bbox.height, 10.0)
        vert_gap = b.bbox.y0 - prev.bbox.y1

        if is_heading_candidate:
            # Terminate current group and start heading
            grouped_units.append(current_group)
            grouped_units.append([b])
            current_group = []
        elif vert_gap < line_height * 1.6 and abs(b.bbox.x0 - prev.bbox.x0) < page_width * 0.35:
            current_group.append(b)
        else:
            grouped_units.append(current_group)
            current_group = [b]

    if current_group:
        grouped_units.append(current_group)

    # 3. Convert grouped units into ExtractedElement instances
    for group in grouped_units:
        if not group:
            continue

        text_content = " ".join(b.text for b in group).strip()
        if not text_content:
            continue

        # Compute merged bounding box across grouped blocks
        min_x = min(b.bbox.x0 for b in group)
        min_y = min(b.bbox.y0 for b in group)
        max_x = max(b.bbox.x1 for b in group)
        max_y = max(b.bbox.y1 for b in group)
        avg_confidence = round(sum(b.confidence for b in group) / len(group), 4)

        merged_bbox = BoundingBox(
            x0=round(min_x, 2),
            y0=round(min_y, 2),
            x1=round(max_x, 2),
            y1=round(max_y, 2),
            page_number=page_num,
        )

        locator = ChunkLocator(
            page_number=page_num,
            section_heading=heading_tracker,
            bounding_box=merged_bbox,
        )

        element_metadata = {
            "ocr_applied": True,
            "ocr_confidence": avg_confidence,
            "ocr_engine": engine_name,
            "line_count": len(group),
        }

        # Heading classification
        is_heading = len(group) == 1 and is_ocr_heading(text_content)
        if is_heading:
            heading_elem = HeadingElement(
                text=text_content,
                level=1,
                page_number=page_num,
                section_heading=text_content,
                locator=locator,
                metadata=element_metadata,
            )
            heading_tracker = text_content
            elements.append(heading_elem)
        else:
            para_elem = ParagraphElement(
                text=text_content,
                page_number=page_num,
                section_heading=heading_tracker,
                locator=locator,
                metadata=element_metadata,
            )
            elements.append(para_elem)

    return elements, heading_tracker
