"""Discriminated union and normalized models for heterogeneous enterprise document elements.

Explicit Coordinate Semantics:
- Page numbers are 1-based (page >= 1).
- Spreadsheet row numbers are 1-based physical worksheet positions (row >= 1).
- Character offsets are 0-indexed half-open intervals [char_start, char_end) within the canonical text stream.
- Original reading order is preserved sequentially.
"""

import json
import uuid
from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from backend.ingestion.types.coordinates import (
    ChunkLocator,
    parse_row_range,
)
from backend.ingestion.types.enums import ElementType


class BaseExtractedElement(BaseModel):
    """Abstract base contract for an atomic extracted element with provenance locator.

    All extracted elements support:
    - content: Canonical extracted textual representation
    - element_type: Semantic discriminator (HEADING, PARAGRAPH, TABLE, LIST, TEXT_BLOCK, etc.)
    - page_number: 1-indexed page number (optional for non-paged documents)
    - section_heading: Nearest parent hierarchical heading title
    - sheet_name: Worksheet name for spreadsheet documents
    - row_range: Row coordinate identifier or range (e.g. 'Row 14' or '14:28')
    - char_start: 0-indexed start character offset in canonical stream
    - char_end: 0-indexed end character offset (half-open: char_start <= char_end)
    - reading_order: Preserved reading order sequence index
    - metadata: Format-specific attributes (fonts, OCR scores, styles, etc.)
    - locator: Provenance ChunkLocator container
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    element_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Deterministic or random unique element UUID",
    )
    element_type: ElementType = Field(..., description="Element categorization discriminator")
    content: str | None = Field(default=None, description="Canonical extracted text content")

    # Optional format-specific and spatial provenance coordinates
    page_number: int | None = Field(default=None, ge=1, description="1-indexed page number")
    section_heading: str | None = Field(default=None, description="Nearest section heading context")
    sheet_name: str | None = Field(default=None, max_length=255, description="Worksheet name for spreadsheets")
    row_range: str | None = Field(default=None, max_length=100, description="Row identifier or range")
    char_start: int | None = Field(default=None, ge=0, description="0-indexed start character offset")
    char_end: int | None = Field(default=None, ge=0, description="0-indexed end character offset")

    # Reading order preservation
    reading_order: int | None = Field(default=None, ge=0, description="Preserved reading order sequence index")

    locator: ChunkLocator = Field(
        default_factory=ChunkLocator,
        description="Source provenance coordinates for this element",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Format-specific metadata (fonts, styles, cell formats, OCR scores)",
    )

    @model_validator(mode="before")
    @classmethod
    def _sync_inputs_before(cls, data: Any) -> Any:
        """Pre-process constructor inputs to unify direct locator kwargs and locator objects."""
        if not isinstance(data, dict):
            return data

        # Unpack locator dictionary if provided
        loc = data.get("locator")
        if isinstance(loc, dict):
            for k in ("page_number", "section_heading", "sheet_name", "row_range", "char_start", "char_end"):
                if data.get(k) is None and loc.get(k) is not None:
                    data[k] = loc[k]
        elif isinstance(loc, ChunkLocator):
            for k in ("page_number", "section_heading", "sheet_name", "row_range", "char_start", "char_end"):
                if data.get(k) is None and getattr(loc, k) is not None:
                    data[k] = getattr(loc, k)

        # Bridge content to text if content provided but text is missing
        if "content" in data and ("text" not in data or not data.get("text")):
            data["text"] = data["content"]

        return data

    @model_validator(mode="after")
    def _sync_coordinates_after(self) -> "BaseExtractedElement":
        """Synchronize locator container and direct attributes, ensuring valid semantics."""
        # Sync from locator if locator was populated but direct fields were None
        if self.locator.page_number is not None and self.page_number is None:
            self.page_number = self.locator.page_number
        if self.locator.section_heading is not None and self.section_heading is None:
            self.section_heading = self.locator.section_heading
        if self.locator.sheet_name is not None and self.sheet_name is None:
            self.sheet_name = self.locator.sheet_name
        if self.locator.row_range is not None and self.row_range is None:
            self.row_range = self.locator.row_range
        if self.locator.char_start is not None and self.char_start is None:
            self.char_start = self.locator.char_start
        if self.locator.char_end is not None and self.char_end is None:
            self.char_end = self.locator.char_end

        # Validate offsets
        if self.char_start is not None and self.char_end is not None:
            if self.char_start > self.char_end:
                raise ValueError(
                    f"Invalid character offsets: char_start ({self.char_start}) "
                    f"cannot exceed char_end ({self.char_end})"
                )

        # Validate row range
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

        # Build / update locator with current direct fields
        bbox = self.locator.bounding_box if self.locator else None
        object.__setattr__(
            self,
            "locator",
            ChunkLocator(
                page_number=self.page_number,
                section_heading=self.section_heading,
                sheet_name=self.sheet_name,
                row_range=self.row_range,
                char_start=self.char_start,
                char_end=self.char_end,
                bounding_box=bbox,
            ),
        )

        # Ensure content field is populated from text_content property if None
        if self.content is None:
            try:
                object.__setattr__(self, "content", self.text_content)
            except (NotImplementedError, AttributeError):
                pass

        return self

    @property
    def text_content(self) -> str:
        """Normalized textual representation suitable for embedding or chunking."""
        if self.content is not None:
            return self.content
        raise NotImplementedError("Subclasses must implement text_content property.")

    def has_source_traceability(self) -> bool:
        """Determine whether this element has verifiable provenance coordinates linking to source."""
        if self.locator and self.locator.has_provenance():
            return True
        if any(
            [
                self.page_number is not None,
                self.section_heading is not None,
                self.sheet_name is not None,
                self.row_range is not None,
                self.char_start is not None,
                self.char_end is not None,
            ]
        ):
            return True
        if self.metadata and any(
            k in self.metadata
            for k in [
                "source_file",
                "source_url",
                "document_id",
                "sheet_name",
                "page",
                "line_number",
            ]
        ):
            return True
        return False

    def validate_traceability(self, strict: bool = True) -> bool:
        """Validate that non-empty elements have source provenance coordinates."""
        effective_content = self.content if self.content is not None else ""
        if effective_content.strip():
            if not self.has_source_traceability():
                if strict:
                    raise ValueError(
                        f"Untraceable extracted element: non-empty element of type '{self.element_type}' "
                        f"(id={self.element_id}) lacks source locator coordinates "
                        "(page_number, section_heading, sheet_name, row_range, or char offsets)."
                    )
                return False
        return True

    def to_normalized(self) -> "NormalizedElement":
        """Convert this concrete element into the unified NormalizedElement representation."""
        return NormalizedElement(
            element_id=self.element_id,
            content=self.content if self.content is not None else self.text_content,
            element_type=self.element_type,
            page_number=self.page_number,
            section_heading=self.section_heading,
            sheet_name=self.sheet_name,
            row_range=self.row_range,
            char_start=self.char_start,
            char_end=self.char_end,
            reading_order=self.reading_order,
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize element to dictionary representation."""
        d = self.model_dump(mode="json")
        type_str = self.element_type.value if isinstance(self.element_type, ElementType) else str(self.element_type)
        d["element_type"] = type_str
        d["content"] = self.content if self.content is not None else self.text_content
        d["locator"] = self.locator.to_provenance_payload()
        return d

    def to_json(self, indent: int | None = None) -> str:
        """Serialize element to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BaseExtractedElement":
        """Deserialize element dictionary into appropriate concrete ExtractedElement."""
        adapter = TypeAdapter(ExtractedElement)
        return adapter.validate_python(data)

    @classmethod
    def from_json(cls, json_str: str) -> "BaseExtractedElement":
        """Deserialize JSON string into appropriate concrete ExtractedElement."""
        adapter = TypeAdapter(ExtractedElement)
        return adapter.validate_json(json_str)


class TextElement(BaseExtractedElement):
    """Generic or raw text block element (backwards-compatible with Task 01)."""

    element_type: Literal[ElementType.TEXT] = ElementType.TEXT
    text: str = Field(default="", description="Textual content")

    @property
    def text_content(self) -> str:
        return self.text or self.content or ""


class TextBlockElement(BaseExtractedElement):
    """Generic or unstructured text block element."""

    element_type: Literal[ElementType.TEXT_BLOCK] = ElementType.TEXT_BLOCK
    text: str = Field(default="", description="Textual content")

    @property
    def text_content(self) -> str:
        return self.text or self.content or ""


class HeadingElement(BaseExtractedElement):
    """Hierarchical section heading (e.g. H1 through H6) preserving document outline."""

    element_type: Literal[ElementType.HEADING] = ElementType.HEADING
    text: str = Field(..., min_length=1, description="Heading title text")
    level: int = Field(
        ...,
        ge=1,
        le=6,
        description="Hierarchical level (1 for top-level Title/H1, up to 6 for H6)",
    )

    @model_validator(mode="after")
    def _default_section_heading(self) -> "HeadingElement":
        """A heading element defines its own section heading if none specified."""
        if self.section_heading is None:
            self.section_heading = self.text
            if self.locator and self.locator.section_heading is None:
                object.__setattr__(
                    self,
                    "locator",
                    ChunkLocator(
                        page_number=self.page_number,
                        section_heading=self.text,
                        sheet_name=self.sheet_name,
                        row_range=self.row_range,
                        char_start=self.char_start,
                        char_end=self.char_end,
                        bounding_box=self.locator.bounding_box if self.locator else None,
                    ),
                )
        return self

    @property
    def text_content(self) -> str:
        prefix = "#" * self.level
        return f"{prefix} {self.text}"


class ParagraphElement(BaseExtractedElement):
    """Cohesive narrative paragraph extracted from prose documents."""

    element_type: Literal[ElementType.PARAGRAPH] = ElementType.PARAGRAPH
    text: str = Field(default="", description="Paragraph body text")

    @property
    def text_content(self) -> str:
        return self.text or self.content or ""


class TableElement(BaseExtractedElement):
    """Tabular matrix extracted from PDF, DOCX, or HTML tables."""

    element_type: Literal[ElementType.TABLE] = ElementType.TABLE
    headers: list[str] = Field(
        default_factory=list,
        description="Column header titles",
    )
    rows: list[list[Any]] = Field(
        default_factory=list,
        description="Row cell matrices",
    )
    caption: str | None = Field(
        default=None,
        description="Optional table title or caption",
    )
    markdown_repr: str | None = Field(
        default=None,
        description="Pre-rendered markdown representation of table",
    )

    def to_markdown(self) -> str:
        """Render table as standard GitHub Flavored Markdown table."""
        if self.markdown_repr:
            return self.markdown_repr

        lines: list[str] = []
        if self.caption:
            lines.append(f"**Table: {self.caption}**\n")

        headers = self.headers if self.headers else [f"Col {i+1}" for i in range(len(self.rows[0]) if self.rows else 0)]
        if headers:
            header_str = " | ".join(str(h).strip().replace("\n", " ") for h in headers)
            lines.append(f"| {header_str} |")
            lines.append(f"| {' | '.join(['---'] * len(headers))} |")

        for row in self.rows:
            row_str = " | ".join(str(cell).strip().replace("\n", " ") for cell in row)
            lines.append(f"| {row_str} |")

        return "\n".join(lines)

    @property
    def text_content(self) -> str:
        return self.to_markdown()


class ListElement(BaseExtractedElement):
    """Bulleted or numbered list extracted from documents."""

    element_type: Literal[ElementType.LIST] = ElementType.LIST
    items: list[str] = Field(default_factory=list, description="Ordered list of text items")
    ordered: bool = Field(default=False, description="Whether list is numbered (True) or bulleted (False)")
    start_index: int = Field(default=1, ge=1, description="Starting index for numbered lists")

    def to_markdown(self) -> str:
        """Render list as Markdown list items."""
        if not self.items and self.content:
            return self.content
        lines: list[str] = []
        for idx, item in enumerate(self.items):
            prefix = f"{self.start_index + idx}. " if self.ordered else "- "
            clean_item = str(item).strip().replace("\n", " ")
            lines.append(f"{prefix}{clean_item}")
        return "\n".join(lines)

    @property
    def text_content(self) -> str:
        return self.to_markdown()


class SpreadsheetRowElement(BaseExtractedElement):
    """Standalone spreadsheet record representing a single row with column mappings."""

    element_type: Literal[ElementType.SPREADSHEET_ROW] = ElementType.SPREADSHEET_ROW
    sheet_name: str = Field(..., description="Name of the source worksheet (e.g., 'Salaries_2026')")
    row_index: int = Field(..., ge=1, description="1-indexed row number in the worksheet")
    headers: list[str] = Field(
        default_factory=list,
        description="Column header titles",
    )
    cells: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value mapping of column name to row cell value",
    )
    markdown_repr: str | None = Field(
        default=None,
        description="Pre-rendered markdown table or tuple string",
    )

    @model_validator(mode="after")
    def _default_row_range(self) -> "SpreadsheetRowElement":
        """Populate row_range coordinate with 'Row <row_index>' if not explicitly supplied."""
        if self.row_range is None:
            self.row_range = f"Row {self.row_index}"
            if self.locator:
                object.__setattr__(
                    self,
                    "locator",
                    ChunkLocator(
                        page_number=self.page_number,
                        section_heading=self.section_heading,
                        sheet_name=self.sheet_name,
                        row_range=self.row_range,
                        char_start=self.char_start,
                        char_end=self.char_end,
                        bounding_box=self.locator.bounding_box if self.locator else None,
                    ),
                )
        return self

    def to_markdown(self) -> str:
        """Render row as markdown table record."""
        if self.markdown_repr:
            return self.markdown_repr

        if not self.cells:
            return f"Sheet: {self.sheet_name} | Row: {self.row_index}"

        headers = list(self.cells.keys())
        values = [str(self.cells[h]).strip().replace("\n", " ") for h in headers]
        header_line = f"| Sheet | Row | {' | '.join(headers)} |"
        sep_line = f"| {' | '.join(['---'] * (len(headers) + 2))} |"
        row_line = f"| {self.sheet_name} | {self.row_index} | {' | '.join(values)} |"
        return f"{header_line}\n{sep_line}\n{row_line}"

    @property
    def text_content(self) -> str:
        return self.to_markdown()


class PageElement(BaseExtractedElement):
    """Logical page boundary containing sub-elements and page metrics."""

    element_type: Literal[ElementType.PAGE] = ElementType.PAGE
    page_number: int = Field(..., ge=1, description="1-indexed page number")
    width: float | None = Field(default=None, description="Page width in points/pixels")
    height: float | None = Field(default=None, description="Page height in points/pixels")
    is_scanned: bool = Field(default=False, description="Whether page is scanned/image-only requiring OCR")
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0, description="OCR confidence if applicable")
    sub_elements: list["ExtractedElement"] = Field(
        default_factory=list,
        description="Ordered elements contained on this page",
    )

    @property
    def text_content(self) -> str:
        if self.sub_elements:
            return "\n\n".join(e.text_content for e in self.sub_elements if e.text_content)
        return f"[Page {self.page_number}]"


class NormalizedElement(BaseModel):
    """Uniform normalized representation of extracted document content.

    Ensures that all extraction adapters (PDF, DOCX, XLSX, CSV, OCR, TXT, MD)
    produce the exact same normalized contract with identical coordinate semantics.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    element_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Deterministic or random unique element UUID",
    )
    content: str = Field(..., description="Canonical extracted text content")
    element_type: ElementType = Field(..., description="Semantic categorization discriminator")

    # Normalized locator coordinates (optional format-specific fields)
    page_number: int | None = Field(default=None, ge=1, description="1-indexed page number")
    section_heading: str | None = Field(default=None, description="Nearest section heading context")
    sheet_name: str | None = Field(default=None, max_length=255, description="Worksheet name for spreadsheets")
    row_range: str | None = Field(default=None, max_length=100, description="Row coordinate identifier or range")
    char_start: int | None = Field(default=None, ge=0, description="0-indexed start character offset")
    char_end: int | None = Field(default=None, ge=0, description="0-indexed end character offset")

    # Sequence preservation & format metadata
    reading_order: int | None = Field(default=None, ge=0, description="Reading order sequence position")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Format-specific metadata")

    @model_validator(mode="after")
    def _validate_locator_contract(self) -> "NormalizedElement":
        """Enforce strict locator coordinate semantics."""
        # Character offset validation
        if self.char_start is not None and self.char_end is not None:
            if self.char_start > self.char_end:
                raise ValueError(
                    f"Invalid character offsets: char_start ({self.char_start}) "
                    f"cannot exceed char_end ({self.char_end})"
                )

        # Spreadsheet row validation
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

    def has_source_traceability(self) -> bool:
        """Determine whether this element has verifiable provenance coordinates linking to source."""
        if any(
            [
                self.page_number is not None,
                self.section_heading is not None,
                self.sheet_name is not None,
                self.row_range is not None,
                self.char_start is not None,
                self.char_end is not None,
            ]
        ):
            return True
        if self.metadata and any(
            k in self.metadata
            for k in [
                "source_file",
                "source_url",
                "document_id",
                "sheet_name",
                "page",
                "line_number",
            ]
        ):
            return True
        return False

    def validate_traceability(self, strict: bool = True) -> bool:
        """Validate that non-empty elements have source provenance coordinates."""
        if self.content and self.content.strip():
            if not self.has_source_traceability():
                if strict:
                    raise ValueError(
                        f"Untraceable extracted element: non-empty element of type '{self.element_type}' "
                        f"(id={self.element_id}) lacks source locator coordinates "
                        "(page_number, section_heading, sheet_name, row_range, or char offsets)."
                    )
                return False
        return True

    def to_locator(self) -> ChunkLocator:
        """Construct ChunkLocator representation from coordinates."""
        return ChunkLocator(
            page_number=self.page_number,
            section_heading=self.section_heading,
            sheet_name=self.sheet_name,
            row_range=self.row_range,
            char_start=self.char_start,
            char_end=self.char_end,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary representation."""
        type_str = self.element_type.value if isinstance(self.element_type, ElementType) else str(self.element_type)
        return {
            "element_id": self.element_id,
            "content": self.content,
            "element_type": type_str,
            "page_number": self.page_number,
            "section_heading": self.section_heading,
            "sheet_name": self.sheet_name,
            "row_range": self.row_range,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "reading_order": self.reading_order,
            "metadata": dict(self.metadata),
            "locator": self.to_locator().to_provenance_payload(),
        }

    def to_json(self, indent: int | None = None) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def to_concrete_element(self) -> "ExtractedElement":
        """Convert normalized element back into a concrete typed element."""
        common_kwargs: dict[str, Any] = {
            "element_id": self.element_id,
            "page_number": self.page_number,
            "section_heading": self.section_heading,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "reading_order": self.reading_order,
            "metadata": self.metadata,
        }

        if self.element_type == ElementType.HEADING:
            level = self.metadata.get("level", 1)
            text = self.content.lstrip("#").strip()
            return HeadingElement(text=text or self.content, level=level, **common_kwargs)

        if self.element_type == ElementType.PARAGRAPH:
            return ParagraphElement(text=self.content, **common_kwargs)

        if self.element_type == ElementType.TABLE:
            return TableElement(
                markdown_repr=self.content,
                sheet_name=self.sheet_name,
                row_range=self.row_range,
                **common_kwargs,
            )

        if self.element_type == ElementType.LIST:
            return ListElement(content=self.content, **common_kwargs)

        if self.element_type == ElementType.SPREADSHEET_ROW:
            row_idx = 1
            if self.row_range:
                s, _ = parse_row_range(self.row_range)
                if s is not None:
                    row_idx = s
            return SpreadsheetRowElement(
                sheet_name=self.sheet_name or "Sheet1",
                row_index=row_idx,
                markdown_repr=self.content,
                **common_kwargs,
            )

        if self.element_type == ElementType.PAGE:
            return PageElement(page_number=self.page_number or 1, **common_kwargs)

        if self.element_type == ElementType.TEXT_BLOCK:
            return TextBlockElement(text=self.content, **common_kwargs)

        # Default to TextElement
        return TextElement(text=self.content, **common_kwargs)

    @classmethod
    def from_element(cls, element: BaseExtractedElement) -> "NormalizedElement":
        """Construct NormalizedElement from any BaseExtractedElement instance."""
        return element.to_normalized()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NormalizedElement":
        """Construct NormalizedElement from a dictionary."""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> "NormalizedElement":
        """Construct NormalizedElement from JSON string."""
        return cls.model_validate_json(json_str)


# NormalizedExtractedElement alias for architectural consistency
NormalizedExtractedElement = NormalizedElement

# Discriminated Union type for type-safe polymorphic deserialization
ExtractedElement = Annotated[
    Union[
        TextElement,
        TextBlockElement,
        HeadingElement,
        ParagraphElement,
        TableElement,
        ListElement,
        SpreadsheetRowElement,
        PageElement,
    ],
    Field(discriminator="element_type"),
]

# Update forward references for nested elements in PageElement
PageElement.model_rebuild()
