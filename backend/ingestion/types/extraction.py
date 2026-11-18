"""Extraction result container and execution performance contracts."""

import json
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.coordinates import ChunkLocator
from backend.ingestion.types.elements import (
    ExtractedElement,
    HeadingElement,
    ListElement,
    NormalizedElement,
    ParagraphElement,
    SpreadsheetRowElement,
    TableElement,
    TextBlockElement,
)
from backend.ingestion.types.enums import DocumentType, ElementType
from backend.ingestion.types.metadata import DocumentMetadata


class ExtractionStats(BaseModel):
    """Execution telemetry and statistical summary for a document extraction run."""

    model_config = ConfigDict(frozen=True)

    extractor_name: str = Field(..., description="Name of the extractor that processed the document")
    extractor_version: str = Field(default="1.0.0", description="SemVer version of the extractor engine")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Extraction wall-clock runtime in seconds")
    total_pages: int | None = Field(default=None, ge=1, description="Number of document pages parsed")
    total_elements: int = Field(default=0, ge=0, description="Total extracted elements generated")
    total_characters: int = Field(default=0, ge=0, description="Total characters extracted across all elements")
    ocr_applied: bool = Field(default=False, description="Flag indicating whether OCR pipeline was invoked")


class ExtractionResult(BaseModel):
    """Complete structured output produced by a DocumentExtractor."""

    model_config = ConfigDict(extra="allow")

    document_id: str = Field(..., description="Unique document UUID")
    document_type: DocumentType = Field(..., description="Document format type")
    mime_type: str = Field(..., description="Detected MIME content type")
    elements: list[ExtractedElement] = Field(
        default_factory=list,
        description="Chronological or structural list of extracted document elements in reading order",
    )
    metadata: DocumentMetadata = Field(..., description="Extracted and attributed document metadata")
    raw_text: str | None = Field(
        default=None,
        description="Optional canonical raw text stream from native parser",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warning messages encountered during extraction",
    )
    stats: ExtractionStats | None = Field(
        default=None,
        description="Telemetry and runtime statistics for this extraction run",
    )

    def get_full_text(self) -> str:
        """Return the complete aggregated textual representation of the document."""
        if self.raw_text:
            return self.raw_text
        return "\n\n".join(e.content or e.text_content for e in self.elements if (e.content or e.text_content))

    def get_elements_by_type(self, element_type: ElementType) -> list[ExtractedElement]:
        """Filter extracted elements by discriminator type."""
        return [e for e in self.elements if e.element_type == element_type]

    def get_headings(self) -> list[HeadingElement]:
        """Convenience accessor for all heading elements in document hierarchy."""
        return [e for e in self.elements if isinstance(e, HeadingElement)]

    def get_paragraphs(self) -> list[ParagraphElement]:
        """Convenience accessor for narrative paragraph elements."""
        return [e for e in self.elements if isinstance(e, ParagraphElement)]

    def get_tables(self) -> list[TableElement]:
        """Convenience accessor for extracted table matrix elements."""
        return [e for e in self.elements if isinstance(e, TableElement)]

    def get_lists(self) -> list[ListElement]:
        """Convenience accessor for extracted list elements."""
        return [e for e in self.elements if isinstance(e, ListElement)]

    def get_text_blocks(self) -> list[TextBlockElement]:
        """Convenience accessor for generic text block elements."""
        return [e for e in self.elements if isinstance(e, TextBlockElement)]

    def get_spreadsheet_rows(self) -> list[SpreadsheetRowElement]:
        """Convenience accessor for extracted spreadsheet row elements."""
        return [e for e in self.elements if isinstance(e, SpreadsheetRowElement)]

    def preserve_reading_order(self) -> None:
        """Ensure all elements carry sequential, 0-indexed reading_order identifiers."""
        for idx, elem in enumerate(self.elements):
            if elem.reading_order is None:
                object.__setattr__(elem, "reading_order", idx)

    def get_elements_in_reading_order(self) -> list[ExtractedElement]:
        """Return elements ordered by preserved reading order."""
        self.preserve_reading_order()
        return sorted(self.elements, key=lambda e: e.reading_order if e.reading_order is not None else 0)

    def to_normalized_elements(self) -> list[NormalizedElement]:
        """Produce the uniform normalized representation across all extracted elements."""
        self.preserve_reading_order()
        return [e.to_normalized() for e in self.elements]

    def compute_canonical_offsets(self, separator: str = "\n\n") -> "ExtractionResult":
        """Compute canonical string stream and exact [char_start, char_end) offsets for all elements.

        Preserves reading order and ensures that for every element:
        self.raw_text[element.char_start:element.char_end] == element.content
        """
        self.preserve_reading_order()
        offset = 0
        canonical_parts: list[str] = []

        for idx, elem in enumerate(self.elements):
            content = elem.content if elem.content is not None else elem.text_content
            char_len = len(content)
            elem.char_start = offset
            elem.char_end = offset + char_len

            # Update locator to maintain exact synchronization
            elem.locator = ChunkLocator(
                page_number=elem.page_number,
                section_heading=elem.section_heading,
                sheet_name=elem.sheet_name,
                row_range=elem.row_range,
                char_start=elem.char_start,
                char_end=elem.char_end,
                bounding_box=elem.locator.bounding_box if elem.locator else None,
            )

            canonical_parts.append(content)
            offset += char_len + len(separator)

        self.raw_text = separator.join(canonical_parts)
        return self

    def validate_traceability(self, strict: bool = True) -> bool:
        """Verify that every non-empty extracted element can be traced back to its source."""
        for elem in self.elements:
            elem.validate_traceability(strict=strict)
        return True

    def to_dict(self) -> dict[str, Any]:
        """Serialize complete extraction result to dictionary representation."""
        return {
            "document_id": self.document_id,
            "document_type": self.document_type.value if isinstance(self.document_type, DocumentType) else str(self.document_type),
            "mime_type": self.mime_type,
            "elements": [e.to_dict() for e in self.elements],
            "metadata": self.metadata.model_dump(mode="json"),
            "raw_text": self.raw_text,
            "warnings": self.warnings,
            "stats": self.stats.model_dump(mode="json") if self.stats else None,
        }

    def to_json(self, indent: int | None = None) -> str:
        """Serialize complete extraction result to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionResult":
        """Deserialize extraction result dictionary."""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> "ExtractionResult":
        """Deserialize extraction result JSON string."""
        return cls.model_validate_json(json_str)
