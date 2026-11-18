"""Extraction subsystem contracts and extractor registry."""

from backend.ingestion.extraction.base import DocumentExtractor
from backend.ingestion.extraction.docx import (
    DocxExtractor,
    OpenXMLDocxExtractor,
)
from backend.ingestion.extraction.ocr import (
    OCRBlock,
    OCREngine,
    RapidOCREngine,
    ScannedPageDetector,
    TesseractOCREngine,
    get_ocr_engine,
    render_pdf_page_to_image,
    sort_and_classify_ocr_blocks,
)
from backend.ingestion.extraction.pdf import (
    NativePDFExtractor,
    ScannedPDFExtractor,
)
from backend.ingestion.extraction.registry import (
    ExtractorRegistry,
    default_extractor_registry,
    register_extractor,
)
from backend.ingestion.extraction.spreadsheet import (
    FORMULA_POLICY_DOCUMENTATION,
    CsvExtractor,
    OpenXMLXlsxExtractor,
    SpreadsheetExtractor,
    XlsxExtractor,
)

__all__ = [
    "DocumentExtractor",
    "NativePDFExtractor",
    "ScannedPDFExtractor",
    "DocxExtractor",
    "OpenXMLDocxExtractor",
    "XlsxExtractor",
    "OpenXMLXlsxExtractor",
    "CsvExtractor",
    "SpreadsheetExtractor",
    "FORMULA_POLICY_DOCUMENTATION",
    "ExtractorRegistry",
    "default_extractor_registry",
    "register_extractor",
    "OCRBlock",
    "OCREngine",
    "RapidOCREngine",
    "TesseractOCREngine",
    "get_ocr_engine",
    "ScannedPageDetector",
    "render_pdf_page_to_image",
    "sort_and_classify_ocr_blocks",
]


