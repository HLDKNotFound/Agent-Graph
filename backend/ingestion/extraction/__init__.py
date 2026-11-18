"""Extraction subsystem contracts and extractor registry."""

from backend.ingestion.extraction.base import DocumentExtractor
from backend.ingestion.extraction.pdf import NativePDFExtractor
from backend.ingestion.extraction.registry import (
    ExtractorRegistry,
    default_extractor_registry,
    register_extractor,
)

__all__ = [
    "DocumentExtractor",
    "NativePDFExtractor",
    "ExtractorRegistry",
    "default_extractor_registry",
    "register_extractor",
]
