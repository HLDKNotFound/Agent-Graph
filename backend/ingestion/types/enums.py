"""Enumerations for the document ingestion subsystem."""

from enum import Enum
from pathlib import Path


class DocumentType(str, Enum):
    """Supported high-level enterprise document types."""

    PDF = "PDF"
    DOCX = "DOCX"
    XLSX = "XLSX"
    CSV = "CSV"
    TXT = "TXT"
    MARKDOWN = "MARKDOWN"

    @classmethod
    def from_mime_type(cls, mime_type: str) -> "DocumentType":
        """Resolve DocumentType from standard MIME type string."""
        normalized = mime_type.strip().lower()
        mapping = {
            "application/pdf": cls.PDF,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": cls.DOCX,
            "application/msword": cls.DOCX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": cls.XLSX,
            "application/vnd.ms-excel": cls.XLSX,
            "text/csv": cls.CSV,
            "text/plain": cls.TXT,
            "text/markdown": cls.MARKDOWN,
            "text/x-markdown": cls.MARKDOWN,
        }
        if normalized in mapping:
            return mapping[normalized]
        raise ValueError(f"Unsupported MIME type for document classification: '{mime_type}'")

    @classmethod
    def from_filename(cls, filename: str) -> "DocumentType":
        """Resolve DocumentType from file extension."""
        suffix = Path(filename).suffix.lower()
        mapping = {
            ".pdf": cls.PDF,
            ".docx": cls.DOCX,
            ".doc": cls.DOCX,
            ".xlsx": cls.XLSX,
            ".xls": cls.XLSX,
            ".csv": cls.CSV,
            ".txt": cls.TXT,
            ".md": cls.MARKDOWN,
            ".markdown": cls.MARKDOWN,
        }
        if suffix in mapping:
            return mapping[suffix]
        raise ValueError(f"Unsupported file extension for filename: '{filename}'")


class DocumentMimeType(str, Enum):
    """Standard MIME types recognized by the document ingestion pipeline."""

    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    CSV = "text/csv"
    TXT = "text/plain"
    MARKDOWN = "text/markdown"


class DocumentClassification(str, Enum):
    """Enterprise security classification levels matching PostgreSQL Prisma schema."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class DocumentIngestionStatus(str, Enum):
    """Document lifecycle and processing status matching PostgreSQL Prisma schema."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PipelineStage(str, Enum):
    """Discrete execution stages in the ingestion pipeline."""

    VALIDATION = "VALIDATION"
    EXTRACTION = "EXTRACTION"
    CHUNKING = "CHUNKING"
    DEDUPLICATION = "DEDUPLICATION"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    PERSISTENCE = "PERSISTENCE"


class ElementType(str, Enum):
    """Discriminated element categories produced by document extractors."""

    TEXT = "text"
    TEXT_BLOCK = "text_block"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    SPREADSHEET_ROW = "spreadsheet_row"
    PAGE = "page"

    @classmethod
    def _missing_(cls, value: object):
        """Case-insensitive and alias-aware enum member lookup."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            # Handle aliases like "textblock" or "text_block"
            if normalized in ("textblock", "text_block"):
                return cls.TEXT_BLOCK
            for member in cls:
                if member.value == normalized or member.name.lower() == normalized:
                    return member
        return None


class ChunkingStrategyType(str, Enum):
    """Available chunking strategies defined in Technical Requirements Document."""

    RECURSIVE_STRUCTURAL = "RECURSIVE_STRUCTURAL"
    FIXED_SLIDING_WINDOW = "FIXED_SLIDING_WINDOW"
    TABLE_AWARE = "TABLE_AWARE"
