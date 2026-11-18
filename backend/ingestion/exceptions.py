"""Domain exceptions for the document ingestion subsystem."""

from typing import Any


class IngestionError(Exception):
    """Base exception for all document ingestion subsystem errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ExtractionError(IngestionError):
    """Raised when document content extraction fails."""
    pass


class UnsupportedDocumentTypeError(ExtractionError):
    """Raised when a document type or MIME type is not supported by any extractor."""
    pass


class ExtractorNotFoundError(ExtractionError):
    """Raised when no extractor is registered for the requested document type or format."""
    pass


class ExtractorExecutionError(ExtractionError):
    """Raised when an extractor encounters an unrecoverable runtime error during extraction."""
    pass


class CorruptDocumentError(ExtractionError):
    """Raised when input document binary stream is corrupted, truncated, or unparseable."""
    pass


class ValidationError(IngestionError):
    """Raised when document metadata, size, or structure fails validation rules."""
    pass


class PathTraversalError(ValidationError):
    """Raised when a filename or path attempts directory traversal or references unauthorized paths."""
    pass


class EmptyFileError(ValidationError):
    """Raised when an uploaded file is empty (0 bytes)."""
    pass


class FileSizeExceededError(ValidationError):
    """Raised when an uploaded file exceeds the configured maximum file size."""
    pass


class FileSignatureMismatchError(ValidationError):
    """Raised when the binary magic byte signature does not match the file extension or declared MIME type."""
    pass


class MalformedFileError(CorruptDocumentError):
    """Raised when a file's internal structure is corrupted, incomplete, or unparseable."""
    pass


# ==========================================
# STORAGE SUBSYSTEM EXCEPTIONS
# ==========================================

class StorageError(IngestionError):
    """Base exception for file storage operations."""
    pass


class StorageFileNotFoundError(StorageError):
    """Raised when a requested storage key or document cannot be found."""
    pass


class StorageConfigurationError(StorageError):
    """Raised when storage configuration is invalid or required drivers/packages are missing."""
    pass


class TenantStorageViolationError(StorageError):
    """Raised when attempting to access or manipulate a file outside the caller's tenant boundary."""
    pass


class StorageAccessDeniedError(StorageError):
    """Raised when the requesting identity lacks permission to access the stored file."""
    pass


class ChunkingError(IngestionError):
    """Raised when document chunking fails to split or segment content correctly."""
    pass


class DeduplicationError(IngestionError):
    """Raised when deduplication checks (SHA-256 or MinHash LSH) fail."""
    pass


class EmbeddingError(IngestionError):
    """Raised when embedding generation fails."""
    pass


class IndexingError(IngestionError):
    """Raised when vector indexing in Qdrant or persistence in PostgreSQL fails."""
    pass


class OrchestrationError(IngestionError):
    """Raised when pipeline orchestration encounters an error during stage progression."""
    pass

