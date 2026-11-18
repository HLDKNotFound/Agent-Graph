"""Spatial, structural, and line-level coordinate representations for document elements and chunks.

Explicit Coordinate Semantics:
- Page numbers are 1-based (Page 1 is the first page; 0 or negative values are invalid).
- Spreadsheet row numbers refer to original worksheet row positions (1-based: Row 1 is the first row).
- Character offsets are 0-indexed, half-open intervals [char_start, char_end) where char_start <= char_end.
- Canonical extracted-text representation uses normalized UTF-8 string streams with '\\n' line endings.
"""

import re
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Explicit Coordinate Semantics Documentation & Constants
COORDINATE_INDEXING_PAGE: str = "1-based"
COORDINATE_INDEXING_SPREADSHEET_ROW: str = "1-based"
COORDINATE_INDEXING_CHAR_OFFSETS: str = "0-indexed half-open interval [char_start, char_end)"
CANONICAL_LINE_SEPARATOR: str = "\n"
CANONICAL_PARAGRAPH_SEPARATOR: str = "\n\n"


def parse_row_range(row_range_str: str) -> tuple[int | None, int | None]:
    """Parse a spreadsheet row range string into (start_row, end_row).

    Spreadsheet row numbers refer to original physical worksheet row positions (1-based).
    Examples:
        'Row 14' -> (14, None)
        '14' -> (14, None)
        'Row 15:30' -> (15, 30)
        '15:30' -> (15, 30)
        'Row 15-30' -> (15, 30)
        '15-30' -> (15, 30)
        'Sheet1!Row 14:28' -> (14, 28)
    """
    s = row_range_str.strip()
    # Check for range: e.g. "Row 15:30", "15:30", "15-30", "Row 15 - Row 30"
    range_match = re.search(r"(?:Row\s*)?(-?\d+)\s*(?::|--?)\s*(?:Row\s*)?(-?\d+)", s, re.IGNORECASE)
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2))

    # Check for single row: e.g. "Row 14" or "14"
    single_match = re.search(r"(?:Row\s*)?(-?\d+)", s, re.IGNORECASE)
    if single_match:
        return int(single_match.group(1)), None

    return None, None


class BoundingBox(BaseModel):
    """Visual spatial bounding box coordinate on a document page (e.g., OCR or PDF)."""

    model_config = ConfigDict(frozen=True)

    x0: float = Field(..., description="Left horizontal coordinate")
    y0: float = Field(..., description="Top vertical coordinate")
    x1: float = Field(..., description="Right horizontal coordinate")
    y1: float = Field(..., description="Bottom vertical coordinate")
    page_number: int | None = Field(default=None, ge=1, description="1-indexed page number")
    unit: str = Field(default="pt", description="Measurement unit (e.g. 'pt', 'px', 'normalized')")

    @model_validator(mode="after")
    def validate_coordinates(self) -> "BoundingBox":
        """Verify bounding box coordinates form a valid positive rectangular area."""
        if self.x0 > self.x1:
            raise ValueError(f"Invalid bounding box: x0 ({self.x0}) cannot exceed x1 ({self.x1})")
        if self.y0 > self.y1:
            raise ValueError(f"Invalid bounding box: y0 ({self.y0}) cannot exceed y1 ({self.y1})")
        return self


class ChunkLocator(BaseModel):
    """Source provenance and locator coordinates for verifiable citation and auditability.

    Adheres strictly to the schema definition and database convention:
    - Page numbers: 1-indexed (page >= 1).
    - Spreadsheet row numbers: 1-indexed physical worksheet positions (row >= 1).
    - Character offsets: 0-indexed, half-open interval [char_start, char_end)
      representing the exact character slice within the canonical source text stream.
    """

    model_config = ConfigDict(frozen=True)

    page_number: int | None = Field(
        default=None,
        ge=1,
        description="1-indexed page number in the original document (PDF, OCR, multi-page DOCX)",
    )
    section_heading: str | None = Field(
        default=None,
        description="Nearest parent hierarchical heading or section title",
    )
    sheet_name: str | None = Field(
        default=None,
        max_length=255,
        description="Worksheet name for spreadsheet documents (XLSX, CSV)",
    )
    row_range: str | None = Field(
        default=None,
        max_length=100,
        description="Row coordinate identifier or range (e.g., 'Row 14' or '14:28')",
    )
    char_start: int | None = Field(
        default=None,
        ge=0,
        description="0-indexed starting character offset within source document stream",
    )
    char_end: int | None = Field(
        default=None,
        ge=0,
        description="0-indexed ending character offset (half-open: char_start <= char_end)",
    )
    bounding_box: BoundingBox | None = Field(
        default=None,
        description="Visual spatial bounding box on page if available from OCR or PDF parser",
    )

    @model_validator(mode="after")
    def validate_offsets(self) -> "ChunkLocator":
        """Validate character offsets follow half-open interval rule [char_start, char_end)."""
        if self.char_start is not None and self.char_end is not None:
            if self.char_start > self.char_end:
                raise ValueError(
                    f"Invalid character offsets: char_start ({self.char_start}) "
                    f"cannot exceed char_end ({self.char_end})"
                )
        return self

    @model_validator(mode="after")
    def validate_row_range(self) -> "ChunkLocator":
        """Validate spreadsheet row range adheres to 1-based indexing and start <= end."""
        if self.row_range is not None:
            start_row, end_row = parse_row_range(self.row_range)
            if start_row is not None:
                if start_row < 1:
                    raise ValueError(
                        f"Invalid spreadsheet row number in row_range '{self.row_range}': "
                        f"row numbers must be 1-based (>= 1), got {start_row}"
                    )
                if end_row is not None:
                    if end_row < 1:
                        raise ValueError(
                            f"Invalid spreadsheet row number in row_range '{self.row_range}': "
                            f"row numbers must be 1-based (>= 1), got {end_row}"
                        )
                    if start_row > end_row:
                        raise ValueError(
                            f"Invalid row_range '{self.row_range}': "
                            f"start row ({start_row}) cannot exceed end row ({end_row})"
                        )
        return self

    def has_provenance(self) -> bool:
        """Determine whether at least one source locator coordinate is populated."""
        return any(
            [
                self.page_number is not None,
                self.section_heading is not None,
                self.sheet_name is not None,
                self.row_range is not None,
                self.char_start is not None,
                self.char_end is not None,
                self.bounding_box is not None,
            ]
        )

    def slice_canonical_text(self, canonical_text: str) -> str:
        """Slice text from a canonical extracted-text string using [char_start, char_end)."""
        if self.char_start is None or self.char_end is None:
            raise ValueError("Both char_start and char_end must be defined to slice canonical text.")
        if self.char_end > len(canonical_text):
            raise ValueError(
                f"char_end ({self.char_end}) exceeds canonical text length ({len(canonical_text)})."
            )
        return canonical_text[self.char_start : self.char_end]

    def validate_against_text(self, text: str) -> None:
        """Validate character offsets fall strictly within text boundary."""
        if self.char_start is not None and self.char_start > len(text):
            raise ValueError(
                f"char_start ({self.char_start}) exceeds text length ({len(text)})."
            )
        if self.char_end is not None and self.char_end > len(text):
            raise ValueError(
                f"char_end ({self.char_end}) exceeds text length ({len(text)})."
            )

    def to_provenance_payload(self) -> dict[str, Any]:
        """Convert locator to dictionary for persistence or Qdrant vector payload."""
        payload: dict[str, Any] = {
            "page_number": self.page_number,
            "section_heading": self.section_heading,
            "sheet_name": self.sheet_name,
            "row_range": self.row_range,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }
        if self.bounding_box:
            payload["bounding_box"] = {
                "x0": self.bounding_box.x0,
                "y0": self.bounding_box.y0,
                "x1": self.bounding_box.x1,
                "y1": self.bounding_box.y1,
                "unit": self.bounding_box.unit,
            }
        return payload


# ElementLocator alias for unified locator terminology across extracted elements
ElementLocator = ChunkLocator
