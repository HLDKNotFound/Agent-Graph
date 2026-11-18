"""Extraction subsystem contracts and extractor registry."""

from backend.ingestion.extraction.base import DocumentExtractor
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

__all__ = [
    "DocumentExtractor",
    "NativePDFExtractor",
    "ScannedPDFExtractor",
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

