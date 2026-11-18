"""Abstract base class and contract for document extractors."""

import abc
import asyncio
from pathlib import Path
from typing import BinaryIO

from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.enums import DocumentType
from backend.ingestion.types.extraction import ExtractionResult
from backend.ingestion.types.metadata import DocumentMetadata


class DocumentExtractor(abc.ABC):
    """Abstract base class defining the contract for format-specific document extractors.

    Extensible architecture allowing new document formats (PDF, DOCX, XLSX, OCR) and extraction
    strategies to be plugged in without modifying unrelated components.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique identifier name for this extractor (e.g. 'NativePDFExtractor', 'OpenXMLDocxExtractor')."""
        raise NotImplementedError

    @property
    def version(self) -> str:
        """Extractor engine SemVer version."""
        return "1.0.0"

    @property
    @abc.abstractmethod
    def supported_document_types(self) -> list[DocumentType]:
        """List of high-level document types this extractor handles."""
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def supported_mime_types(self) -> list[str]:
        """List of MIME types this extractor can parse."""
        raise NotImplementedError

    @property
    def supports_ocr(self) -> bool:
        """Whether this extractor integrates OCR for scanned image detection."""
        return False

    def can_extract(self, mime_type: str, document_type: DocumentType | None = None) -> bool:
        """Determine whether this extractor can parse the given MIME or document type."""
        normalized_mime = mime_type.strip().lower()
        if any(m.lower() == normalized_mime for m in self.supported_mime_types):
            return True
        if document_type is not None and document_type in self.supported_document_types:
            return True
        return False

    @abc.abstractmethod
    async def extract(
        self,
        content: bytes | BinaryIO | Path,
        context: IngestionContext,
        metadata: DocumentMetadata | None = None,
    ) -> ExtractionResult:
        """Asynchronously extract structured elements from raw binary content or file path.

        Args:
            content: Raw document bytes, file-like binary stream, or local filesystem Path.
            context: Ingestion context carrying multi-tenant credentials and runtime options.
            metadata: Optional pre-constructed document metadata.

        Returns:
            ExtractionResult containing extracted elements, locator coordinates, and metadata.
        """
        raise NotImplementedError

    def extract_sync(
        self,
        content: bytes | BinaryIO | Path,
        context: IngestionContext,
        metadata: DocumentMetadata | None = None,
    ) -> ExtractionResult:
        """Synchronous convenience wrapper around async extract()."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already in an active event loop, run in a separate thread or use task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run,
                    self.extract(content, context, metadata),
                )
                return future.result()
        else:
            return asyncio.run(self.extract(content, context, metadata))
