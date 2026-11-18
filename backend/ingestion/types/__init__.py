"""Shared types, data contracts, and enums for the ingestion subsystem."""

from backend.ingestion.types.chunking import (
    ChunkingConfig,
    DocumentChunkInput,
)
from backend.ingestion.types.coordinates import (
    CANONICAL_LINE_SEPARATOR,
    CANONICAL_PARAGRAPH_SEPARATOR,
    COORDINATE_INDEXING_CHAR_OFFSETS,
    COORDINATE_INDEXING_PAGE,
    COORDINATE_INDEXING_SPREADSHEET_ROW,
    BoundingBox,
    ChunkLocator,
    ElementLocator,
    parse_row_range,
)
from backend.ingestion.types.context import (
    IngestionContext,
    IngestionOptions,
)
from backend.ingestion.types.elements import (
    BaseExtractedElement,
    ExtractedElement,
    HeadingElement,
    ListElement,
    NormalizedElement,
    NormalizedExtractedElement,
    PageElement,
    ParagraphElement,
    SpreadsheetRowElement,
    TableElement,
    TextBlockElement,
    TextElement,
)
from backend.ingestion.types.enums import (
    ChunkingStrategyType,
    DocumentClassification,
    DocumentIngestionStatus,
    DocumentMimeType,
    DocumentType,
    ElementType,
    PipelineStage,
)
from backend.ingestion.types.extraction import (
    ExtractionResult,
    ExtractionStats,
)
from backend.ingestion.types.metadata import (
    AccessControlPayload,
    DocumentMetadata,
)

__all__ = [
    # Enums
    "DocumentType",
    "DocumentMimeType",
    "DocumentClassification",
    "DocumentIngestionStatus",
    "PipelineStage",
    "ElementType",
    "ChunkingStrategyType",
    # Coordinates & Semantics
    "BoundingBox",
    "ChunkLocator",
    "ElementLocator",
    "parse_row_range",
    "COORDINATE_INDEXING_PAGE",
    "COORDINATE_INDEXING_SPREADSHEET_ROW",
    "COORDINATE_INDEXING_CHAR_OFFSETS",
    "CANONICAL_LINE_SEPARATOR",
    "CANONICAL_PARAGRAPH_SEPARATOR",
    # Metadata & ACL
    "AccessControlPayload",
    "DocumentMetadata",
    # Elements & Discriminated Union
    "BaseExtractedElement",
    "TextElement",
    "TextBlockElement",
    "HeadingElement",
    "ParagraphElement",
    "TableElement",
    "ListElement",
    "SpreadsheetRowElement",
    "PageElement",
    "NormalizedElement",
    "NormalizedExtractedElement",
    "ExtractedElement",
    # Extraction
    "ExtractionStats",
    "ExtractionResult",
    # Chunking
    "ChunkingConfig",
    "DocumentChunkInput",
    # Context
    "IngestionOptions",
    "IngestionContext",
]
