"""Unit tests for Scanned PDF Detection and OCR Extraction (Stage 2 — Task 05).

Verifies:
- Scanned page detection based on extractable text quantity, image presence, and coverage.
- Native text pages skip OCR (zero unnecessary OCR invocation).
- In-memory PDF page rasterization with configurable DPI and coordinate scaling.
- Text, page numbers, spatial bounding boxes (points), and OCR confidence scores extraction.
- Natural reading order preservation across OCR text blocks.
- Vietnamese and English text recognition.
- Per-page OCR failure isolation without failing the document.
- Environment-driven configuration and runtime options.
- Dynamic registry resolution with prefer_ocr=True.
"""

import io
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw

from backend.ingestion import (
    DocumentType,
    ElementType,
    ExtractionResult,
    HeadingElement,
    IngestionContext,
    IngestionOptions,
    NativePDFExtractor,
    PageElement,
    ParagraphElement,
    RapidOCREngine,
    ScannedPDFExtractor,
    ScannedPageDetector,
    TesseractOCREngine,
    default_extractor_registry,
    get_ingestion_settings,
    get_ocr_engine,
    render_pdf_page_to_image,
)


def create_synthetic_image(
    lines: list[tuple[int, int, str]],
    width: int = 800,
    height: int = 1100,
) -> bytes:
    """Render text lines onto an in-memory image simulating a scanned document page."""
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    for x, y, text in lines:
        draw.text((x, y), text, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def create_scanned_pdf(
    pages_lines: list[list[tuple[int, int, str]]],
    page_width: float = 595.0,
    page_height: float = 842.0,
) -> bytes:
    """Generate a multi-page scanned PDF with embedded raster images."""
    doc = pymupdf.open()
    for lines in pages_lines:
        page = doc.new_page(width=page_width, height=page_height)
        img_bytes = create_synthetic_image(lines, width=int(page_width * 1.5), height=int(page_height * 1.5))
        rect = pymupdf.Rect(0, 0, page_width, page_height)
        page.insert_image(rect, stream=img_bytes)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_hybrid_mixed_pdf() -> bytes:
    """Generate a 3-page PDF with:
    Page 1: Native digital text.
    Page 2: Scanned image page.
    Page 3: Native digital text.
    """
    doc = pymupdf.open()

    # Page 1: Native digital text
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((50, 60), "1. General Corporate Governance", fontsize=16)
    p1.insert_text((50, 100), "This is native digital text that must not invoke OCR under any circumstance.", fontsize=11)
    p1.insert_text((50, 130), "Native font metrics and exact bounding boxes are directly extracted by PyMuPDF.", fontsize=11)

    # Page 2: Scanned image page
    p2 = doc.new_page(width=595, height=842)
    scanned_lines = [
        (80, 80, "2. Scanned Appendix Policy"),
        (80, 160, "This section is embedded inside a scanned raster image requiring OCR."),
        (80, 240, "Confidentiality agreements must be signed by all external contractors."),
    ]
    img_bytes = create_synthetic_image(scanned_lines, width=892, height=1263)
    p2.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=img_bytes)

    # Page 3: Native digital text
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text((50, 60), "3. Concluding Remarks", fontsize=16)
    p3.insert_text((50, 100), "Digital compliance certificates are stored in PostgreSQL relational tables.", fontsize=11)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class TestScannedPDFDetection:
    """Test suite for ScannedPageDetector heuristics."""

    def test_detection_digital_page(self) -> None:
        """Digital page with high character count is classified as not scanned."""
        detector = ScannedPageDetector(scanned_threshold_chars=50)

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 100), "A" * 200, fontsize=12)

        is_scanned, reason, metrics = detector.detect(page=page, digital_char_count=200, has_tables=False)
        doc.close()

        assert is_scanned is False
        assert "sufficient digital text" in reason
        assert metrics["digital_char_count"] == 200

    def test_detection_image_scanned_page(self) -> None:
        """Page with 0 text and embedded full-page image is classified as scanned."""
        detector = ScannedPageDetector(scanned_threshold_chars=50)

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        img_bytes = create_synthetic_image([(50, 50, "Sample Text")])
        page.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=img_bytes)

        is_scanned, reason, metrics = detector.detect(page=page, digital_char_count=0, has_tables=False)
        doc.close()

        assert is_scanned is True
        assert "Scanned page" in reason
        assert metrics["image_count"] >= 1
        assert metrics["image_coverage"] > 0.5

    def test_detection_empty_page(self) -> None:
        """Completely blank page with 0 text and 0 images is flagged as scanned/blank."""
        detector = ScannedPageDetector(scanned_threshold_chars=50)

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)

        is_scanned, reason, metrics = detector.detect(page=page, digital_char_count=0, has_tables=False)
        doc.close()

        assert is_scanned is True
        assert "0 digital characters" in reason
        assert metrics["digital_char_count"] == 0

    def test_detection_native_table_page(self) -> None:
        """Page with structured tables and sufficient text is classified as native."""
        detector = ScannedPageDetector(scanned_threshold_chars=50)

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 50), "Table of allowances for enterprise staff.", fontsize=11)

        is_scanned, reason, _ = detector.detect(page=page, digital_char_count=80, has_tables=True)
        doc.close()

        assert is_scanned is False
        assert "native tables" in reason


class TestPageImageRendering:
    """Test suite for PDF page rasterization and coordinate scaling."""

    def test_render_pdf_page_to_image_scaling(self) -> None:
        """Verify rendered image dimensions and scale factors at configurable DPI."""
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=600)

        img_150, scale_x_150, scale_y_150 = render_pdf_page_to_image(page=page, dpi=150)
        assert isinstance(img_150, np.ndarray)
        assert img_150.dtype == np.uint8
        assert img_150.shape[2] == 3  # RGB
        # 400 pt * (150 / 72) = ~833.33 px
        expected_w_150 = round(400 * (150 / 72.0))
        expected_h_150 = round(600 * (150 / 72.0))
        assert abs(img_150.shape[1] - expected_w_150) <= 2
        assert abs(img_150.shape[0] - expected_h_150) <= 2
        assert abs(scale_x_150 - (150 / 72.0)) < 0.05

        img_300, scale_x_300, scale_y_300 = render_pdf_page_to_image(page=page, dpi=300)
        assert img_300.shape[1] > img_150.shape[1]
        assert scale_x_300 > scale_x_150
        doc.close()


class TestScannedPDFOCRExtraction:
    """Test suite for ScannedPDFExtractor and RapidOCR integration."""

    @pytest.fixture
    def sample_context(self) -> IngestionContext:
        return IngestionContext(
            tenant_id="tenant-enterprise-ocr",
            document_id="doc-ocr-123",
            user_id="user-qa-456",
            department_id="dept-finance",
            options=IngestionOptions(enable_ocr=True),
        )

    def test_scanned_pdf_extraction_rapidocr(self, sample_context: IngestionContext) -> None:
        """Verify OCR extraction on an image-based scanned PDF."""
        pdf_bytes = create_scanned_pdf(
            [
                [
                    (80, 80, "1. Executive Company Policy"),
                    (80, 160, "This scanned document outlines the confidentiality terms."),
                    (80, 240, "All corporate data must be stored on secure encrypted servers."),
                ]
            ]
        )

        extractor = ScannedPDFExtractor(ocr_dpi=150)
        assert extractor.supports_ocr is True
        assert extractor.name == "ScannedPDFExtractor"

        result = extractor.extract_sync(pdf_bytes, sample_context)

        assert isinstance(result, ExtractionResult)
        assert result.stats is not None
        assert result.stats.ocr_applied is True
        assert result.stats.total_pages == 1
        assert len(result.elements) >= 2

        # Verify page provenance and spatial coordinates
        for elem in result.elements:
            assert elem.page_number == 1
            assert elem.locator.page_number == 1
            assert elem.locator.bounding_box is not None
            bbox = elem.locator.bounding_box
            assert bbox.x0 >= 0.0
            assert bbox.y0 >= 0.0
            assert bbox.x1 <= 595.0
            assert bbox.y1 <= 842.0
            # Verify OCR metadata
            assert elem.metadata.get("ocr_applied") is True
            assert "ocr_confidence" in elem.metadata
            assert 0.0 <= elem.metadata["ocr_confidence"] <= 1.0

        # Verify heading and reading order
        first_elem = result.elements[0]
        assert first_elem.element_type == ElementType.HEADING
        assert "1." in first_elem.text_content

        # Verify full text and canonical offsets
        full_text = result.get_full_text()
        assert "Policy" in full_text
        assert "confidentiality" in full_text.lower()
        result.validate_traceability(strict=True)

    def test_native_pdf_skips_ocr(self, sample_context: IngestionContext) -> None:
        """Digital PDF with sufficient text must NOT invoke OCR."""
        doc = pymupdf.open()
        p = doc.new_page(width=595, height=842)
        p.insert_text((50, 60), "1. Introduction", fontsize=16)
        p.insert_text((50, 100), "This is a digital document containing clear font streams.", fontsize=11)
        p.insert_text((50, 130), "No OCR should be executed for this page.", fontsize=11)
        pdf_bytes = doc.tobytes()
        doc.close()

        extractor = ScannedPDFExtractor()
        with patch.object(RapidOCREngine, "extract_text") as mock_ocr:
            result = extractor.extract_sync(pdf_bytes, sample_context)
            mock_ocr.assert_not_called()

        assert result.stats is not None
        assert result.stats.ocr_applied is False
        assert len(result.elements) >= 2

    def test_mixed_pdf_adaptive_ocr(self, sample_context: IngestionContext) -> None:
        """Verify adaptive OCR: native pages extracted natively, scanned page extracted via OCR."""
        pdf_bytes = create_hybrid_mixed_pdf()

        extractor = ScannedPDFExtractor(ocr_dpi=150)
        result = extractor.extract_sync(pdf_bytes, sample_context)

        assert result.stats is not None
        assert result.stats.ocr_applied is True
        assert result.stats.total_pages == 3

        # Elements from Page 1 should be native
        p1_elements = [e for e in result.elements if e.page_number == 1]
        assert len(p1_elements) >= 2
        for e in p1_elements:
            assert e.metadata.get("ocr_applied") is not True

        # Elements from Page 2 should be OCR
        p2_elements = [e for e in result.elements if e.page_number == 2]
        assert len(p2_elements) >= 1
        for e in p2_elements:
            assert e.metadata.get("ocr_applied") is True
            assert e.metadata.get("ocr_confidence") is not None

        # Elements from Page 3 should be native
        p3_elements = [e for e in result.elements if e.page_number == 3]
        assert len(p3_elements) >= 2
        for e in p3_elements:
            assert e.metadata.get("ocr_applied") is not True

        # Verify page sequence in reading order
        pages_in_order = [e.page_number for e in result.get_elements_in_reading_order()]
        assert pages_in_order == sorted(pages_in_order)
        assert 1 in pages_in_order and 2 in pages_in_order and 3 in pages_in_order

    def test_vietnamese_text_ocr_extraction(self, sample_context: IngestionContext) -> None:
        """Verify OCR extraction on scanned PDF containing Vietnamese text."""
        vietnamese_lines = [
            (80, 80, "1. CHINH SACH BAO HIEM VA PHUC LOI"),
            (80, 160, "Nhan vien chinh thuc duoc huong 20 ngay nghi phep nam."),
            (80, 240, "Cac che do bao hiem y te duoc cong ty chi tra toan bo."),
        ]
        pdf_bytes = create_scanned_pdf([vietnamese_lines])

        extractor = ScannedPDFExtractor(ocr_dpi=150, ocr_languages="eng+vie")
        result = extractor.extract_sync(pdf_bytes, sample_context)

        assert result.stats is not None
        assert result.stats.ocr_applied is True
        full_text = result.get_full_text()
        assert "CHINH SACH" in full_text or "BAO HIEM" in full_text
        assert any(e.page_number == 1 for e in result.elements)

    def test_isolated_page_ocr_failure(self, sample_context: IngestionContext) -> None:
        """Failure on an individual page during OCR does NOT fail the entire document."""
        pdf_bytes = create_hybrid_mixed_pdf()

        extractor = ScannedPDFExtractor(ocr_dpi=150)

        # Mock RapidOCR to simulate an exception on the scanned page (page 2)
        original_extract = RapidOCREngine.extract_text

        def mock_extract(self, image, page_num, scale_x, scale_y):
            if page_num == 2:
                raise RuntimeError("Simulated OOM or corrupted raster image on page 2")
            return original_extract(self, image, page_num, scale_x, scale_y)

        with patch.object(RapidOCREngine, "extract_text", side_effect=mock_extract, autospec=True):
            result = extractor.extract_sync(pdf_bytes, sample_context)

        # Document succeeds!
        assert isinstance(result, ExtractionResult)
        assert result.stats.total_pages == 3

        # Warning is captured and reported
        assert any("Page 2: OCR extraction failed" in w for w in result.warnings)

        # Non-failing pages (1 and 3) are preserved
        p1_elements = [e for e in result.elements if e.page_number == 1]
        p3_elements = [e for e in result.elements if e.page_number == 3]
        assert len(p1_elements) >= 2
        assert len(p3_elements) >= 2

        # Page 2 has isolated fallback element retaining provenance
        p2_elements = [e for e in result.elements if e.page_number == 2]
        assert len(p2_elements) == 1
        fallback_page = p2_elements[0]
        assert fallback_page.page_number == 2
        assert fallback_page.metadata.get("ocr_failed") is True

    def test_extractor_registry_resolution_prefer_ocr(self) -> None:
        """Verify default_extractor_registry resolves ScannedPDFExtractor when prefer_ocr=True."""
        native_ext = default_extractor_registry.get_extractor(DocumentType.PDF, prefer_ocr=False)
        assert native_ext.name == "NativePDFExtractor"
        assert native_ext.supports_ocr is False

        ocr_ext = default_extractor_registry.get_extractor(DocumentType.PDF, prefer_ocr=True)
        assert ocr_ext.name == "ScannedPDFExtractor"
        assert ocr_ext.supports_ocr is True

    def test_native_pdf_extractor_enable_ocr_flag(self) -> None:
        """NativePDFExtractor with enable_ocr=True applies OCR to scanned pages."""
        pdf_bytes = create_scanned_pdf([[(80, 80, "1. Executive Policy"), (80, 160, "Paragraph text")]])
        ctx = IngestionContext(tenant_id="t-1", document_id="d-1", options=IngestionOptions(enable_ocr=True))

        # With enable_ocr=True in context
        native_extractor = NativePDFExtractor(ocr_dpi=150)
        res_with_ocr = native_extractor.extract_sync(pdf_bytes, ctx)
        assert res_with_ocr.stats.ocr_applied is True
        assert len(res_with_ocr.elements) >= 1

        # With enable_ocr=False in context (default Task 04 behavior: warning only)
        ctx_no_ocr = IngestionContext(tenant_id="t-1", document_id="d-1", options=IngestionOptions(enable_ocr=False))
        res_no_ocr = native_extractor.extract_sync(pdf_bytes, ctx_no_ocr)
        assert res_no_ocr.stats.ocr_applied is False
        assert any("Flagged as potential scanned" in w for w in res_no_ocr.warnings)

    def test_configurable_ocr_settings_from_environment(self) -> None:
        """Verify OCR configuration respects IngestionSettings and environment overrides."""
        settings = get_ingestion_settings()
        assert settings.ocr_enabled is True
        assert settings.ocr_engine == "rapidocr"
        assert settings.ocr_dpi >= 72
        assert "vie" in settings.ocr_languages

        engine = get_ocr_engine("rapidocr")
        assert engine.name == "RapidOCR"
        assert engine.is_available is True

    def test_tesseract_engine_graceful_availability(self) -> None:
        """TesseractOCREngine correctly reports availability without unhandled crash."""
        engine = TesseractOCREngine()
        assert isinstance(engine.is_available, bool)
        # If tesseract binary is not installed, factory safely falls back to RapidOCR
        resolved = get_ocr_engine("tesseract")
        assert resolved is not None
        assert resolved.name in ("Tesseract", "RapidOCR")
