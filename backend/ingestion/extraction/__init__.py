"""Extraction subsystem contracts and extractor registry."""

from backend.ingestion.extraction.base import DocumentExtractor
from backend.ingestion.extraction.registry import (
    ExtractorRegistry,
    default_extractor_registry,
    register_extractor,
)

__all__ = [
    "DocumentExtractor",
    "ExtractorRegistry",
    "default_extractor_registry",
    "register_extractor",
]
