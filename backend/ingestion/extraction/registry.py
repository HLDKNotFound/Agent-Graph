"""Registry and factory for pluggable document extractors."""

import threading
from typing import Callable, TypeVar

from backend.ingestion.exceptions import (
    ExtractorNotFoundError,
    UnsupportedDocumentTypeError,
)
from backend.ingestion.extraction.base import DocumentExtractor
from backend.ingestion.types.enums import DocumentType

T = TypeVar("T", bound=type[DocumentExtractor] | DocumentExtractor)


class ExtractorRegistry:
    """Thread-safe registry managing available DocumentExtractor implementations.

    Enables open-closed architecture: new extractors for emerging formats or specialized OCR
    engines can register themselves dynamically without modifying core pipeline orchestration.
    """

    def __init__(self) -> None:
        self._extractors: list[DocumentExtractor] = []
        self._lock = threading.RLock()

    def register(
        self,
        extractor: DocumentExtractor | type[DocumentExtractor],
    ) -> DocumentExtractor:
        """Register an extractor instance or class. If a class is provided, it is instantiated."""
        with self._lock:
            instance = extractor() if isinstance(extractor, type) else extractor

            if not isinstance(instance, DocumentExtractor):
                raise TypeError(f"Expected DocumentExtractor instance or subclass, got {type(extractor)}")

            # Avoid duplicate registrations of the exact same extractor name
            self._extractors = [e for e in self._extractors if e.name != instance.name]
            self._extractors.append(instance)
            return instance

    def unregister(self, name: str) -> bool:
        """Remove an extractor by name."""
        with self._lock:
            before_len = len(self._extractors)
            self._extractors = [e for e in self._extractors if e.name != name]
            return len(self._extractors) < before_len

    def get_extractor(
        self,
        document_type: DocumentType | None = None,
        mime_type: str | None = None,
        prefer_ocr: bool = False,
    ) -> DocumentExtractor:
        """Find the most appropriate registered extractor matching the document or MIME type.

        Args:
            document_type: High-level DocumentType enum.
            mime_type: MIME type string.
            prefer_ocr: If True, prioritize extractors that explicitly support OCR.

        Returns:
            DocumentExtractor capable of parsing the document.

        Raises:
            UnsupportedDocumentTypeError if type/MIME is unresolvable.
            ExtractorNotFoundError if no registered extractor can handle the requested format.
        """
        if not document_type and not mime_type:
            raise UnsupportedDocumentTypeError("Must provide either document_type or mime_type to resolve extractor.")

        with self._lock:
            candidates: list[DocumentExtractor] = []
            for extractor in self._extractors:
                matches_type = document_type and document_type in extractor.supported_document_types
                matches_mime = mime_type and any(m.lower() == mime_type.lower() for m in extractor.supported_mime_types)
                if matches_type or matches_mime:
                    candidates.append(extractor)

            if not candidates:
                identifier = f"type={document_type}" if document_type else f"mime={mime_type}"
                raise ExtractorNotFoundError(f"No registered extractor found for {identifier}")

            if prefer_ocr:
                ocr_candidates = [c for c in candidates if c.supports_ocr]
                if ocr_candidates:
                    return ocr_candidates[0]

            return candidates[0]

    def list_extractors(self) -> list[DocumentExtractor]:
        """Return a copy of all currently registered extractors."""
        with self._lock:
            return list(self._extractors)

    def list_supported_document_types(self) -> set[DocumentType]:
        """Return all DocumentType enums supported across all registered extractors."""
        with self._lock:
            types: set[DocumentType] = set()
            for extractor in self._extractors:
                types.update(extractor.supported_document_types)
            return types

    def list_supported_mime_types(self) -> set[str]:
        """Return all MIME types supported across all registered extractors."""
        with self._lock:
            mimes: set[str] = set()
            for extractor in self._extractors:
                mimes.update(extractor.supported_mime_types)
            return mimes

    def clear(self) -> None:
        """Clear all registered extractors (primarily used in testing)."""
        with self._lock:
            self._extractors.clear()


# Global default registry instance
default_extractor_registry = ExtractorRegistry()


def register_extractor(
    registry: ExtractorRegistry | None = None,
) -> Callable[[T], T]:
    """Decorator to register a DocumentExtractor class or instance into the registry.

    Usage:
        @register_extractor()
        class PDFExtractor(DocumentExtractor):
            ...
    """
    target_registry = registry or default_extractor_registry

    def decorator(cls_or_instance: T) -> T:
        target_registry.register(cls_or_instance)
        return cls_or_instance

    return decorator
