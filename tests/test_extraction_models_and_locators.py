"""Unit tests for Extraction Result Models and Locator Contracts (Stage 1 — Task 03).

Verifies:
- Normalized representation across heterogeneous extracted elements.
- Locator coordinates semantics (1-based pages, 1-based spreadsheet rows, [char_start, char_end)).
- Support for HEADING, PARAGRAPH, TABLE, LIST, TEXT_BLOCK, SPREADSHEET_ROW, PAGE.
- Optional format-specific fields when they do not apply.
- Reading order preservation and sequential indexing.
- Rejection of invalid locator structures.
- Canonical extracted-text offset computation and verification.
- Source traceability validation for non-empty elements.
- Full serialization and deserialization support (JSON and dict).
"""

import json
import uuid
import pytest
from pydantic import TypeAdapter, ValidationError

from backend.ingestion import (
    CANONICAL_LINE_SEPARATOR,
    CANONICAL_PARAGRAPH_SEPARATOR,
    COORDINATE_INDEXING_CHAR_OFFSETS,
    COORDINATE_INDEXING_PAGE,
    COORDINATE_INDEXING_SPREADSHEET_ROW,
    BoundingBox,
    ChunkLocator,
    DocumentMetadata,
    DocumentType,
    ElementLocator,
    ElementType,
    ExtractedElement,
    ExtractionResult,
    ExtractionStats,
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
    parse_row_range,
)


class TestCoordinateSemanticsAndLocators:
    """Verify coordinate semantics, 1-based indexing, range parsing, and invalid locator rejection."""

    def test_coordinate_semantics_constants(self) -> None:
        """Verify explicit coordinate indexing conventions."""
        assert COORDINATE_INDEXING_PAGE == "1-based"
        assert COORDINATE_INDEXING_SPREADSHEET_ROW == "1-based"
        assert "[char_start, char_end)" in COORDINATE_INDEXING_CHAR_OFFSETS
        assert CANONICAL_LINE_SEPARATOR == "\n"
        assert CANONICAL_PARAGRAPH_SEPARATOR == "\n\n"

    def test_element_locator_alias(self) -> None:
        """Verify ElementLocator is an alias for ChunkLocator."""
        assert ElementLocator is ChunkLocator
        loc = ElementLocator(page_number=1, char_start=0, char_end=10)
        assert isinstance(loc, ChunkLocator)
        assert loc.page_number == 1

    def test_page_number_1_based_validation(self) -> None:
        """Page numbers must be 1-based positive integers (>= 1)."""
        loc = ChunkLocator(page_number=1)
        assert loc.page_number == 1

        with pytest.raises(ValidationError, match="Input should be greater than or equal to 1"):
            ChunkLocator(page_number=0)

        with pytest.raises(ValidationError, match="Input should be greater than or equal to 1"):
            ChunkLocator(page_number=-5)

    def test_char_offsets_half_open_validation(self) -> None:
        """Character offsets must be non-negative and char_start <= char_end."""
        loc = ChunkLocator(char_start=0, char_end=15)
        assert loc.char_start == 0
        assert loc.char_end == 15

        # Empty interval is valid
        loc_empty = ChunkLocator(char_start=5, char_end=5)
        assert loc_empty.char_start == 5
        assert loc_empty.char_end == 5

        # Negative char_start is invalid
        with pytest.raises(ValidationError, match="Input should be greater than or equal to 0"):
            ChunkLocator(char_start=-1, char_end=10)

        # Negative char_end is invalid
        with pytest.raises(ValidationError, match="Input should be greater than or equal to 0"):
            ChunkLocator(char_start=0, char_end=-1)

        # Inverted char offsets are invalid
        with pytest.raises(ValidationError, match="char_start .* cannot exceed char_end"):
            ChunkLocator(char_start=50, char_end=20)

    def test_spreadsheet_row_range_parsing(self) -> None:
        """Verify parse_row_range helper parses various row coordinate formats."""
        assert parse_row_range("Row 14") == (14, None)
        assert parse_row_range("14") == (14, None)
        assert parse_row_range("Row 15:30") == (15, 30)
        assert parse_row_range("15:30") == (15, 30)
        assert parse_row_range("Row 15-30") == (15, 30)
        assert parse_row_range("Sheet1!Row 14:28") == (14, 28)

    def test_spreadsheet_row_range_validation_valid(self) -> None:
        """Valid row ranges are accepted."""
        loc1 = ChunkLocator(sheet_name="Budgets", row_range="Row 14")
        assert loc1.row_range == "Row 14"

        loc2 = ChunkLocator(sheet_name="Budgets", row_range="Row 15:30")
        assert loc2.row_range == "Row 15:30"

    def test_spreadsheet_row_range_validation_invalid_zero(self) -> None:
        """Spreadsheet row numbers are 1-based; row 0 or negative must be rejected."""
        with pytest.raises(ValidationError, match="row numbers must be 1-based"):
            ChunkLocator(sheet_name="Budgets", row_range="Row 0")

        with pytest.raises(ValidationError, match="row numbers must be 1-based"):
            ChunkLocator(sheet_name="Budgets", row_range="Row 0:10")

        with pytest.raises(ValidationError, match="row numbers must be 1-based"):
            ChunkLocator(sheet_name="Budgets", row_range="Row 10:0")

    def test_spreadsheet_row_range_validation_inverted(self) -> None:
        """Inverted row ranges (start > end) must be rejected."""
        with pytest.raises(ValidationError, match="start row .* cannot exceed end row"):
            ChunkLocator(sheet_name="Budgets", row_range="Row 30:15")

    def test_bounding_box_validation(self) -> None:
        """Bounding box rectangular area validation."""
        valid_box = BoundingBox(x0=0.0, y0=0.0, x1=100.0, y1=200.0, page_number=1)
        assert valid_box.x1 == 100.0

        with pytest.raises(ValidationError, match="x0 .* cannot exceed x1"):
            BoundingBox(x0=150.0, y0=0.0, x1=100.0, y1=200.0)

        with pytest.raises(ValidationError, match="y0 .* cannot exceed y1"):
            BoundingBox(x0=0.0, y0=250.0, x1=100.0, y1=200.0)

    def test_slice_canonical_text(self) -> None:
        """Verify slicing of canonical text stream."""
        canonical = "Enterprise Policy: Employees have 20 days annual leave."
        loc = ChunkLocator(char_start=0, char_end=17)
        assert loc.slice_canonical_text(canonical) == "Enterprise Policy"

        with pytest.raises(ValueError, match="exceeds canonical text length"):
            ChunkLocator(char_start=0, char_end=1000).slice_canonical_text(canonical)

    def test_locator_has_provenance(self) -> None:
        """Verify has_provenance detects populated coordinates."""
        assert not ChunkLocator().has_provenance()
        assert ChunkLocator(page_number=1).has_provenance()
        assert ChunkLocator(section_heading="Intro").has_provenance()
        assert ChunkLocator(sheet_name="Salaries").has_provenance()
        assert ChunkLocator(char_start=0, char_end=10).has_provenance()


class TestExtractedElementModels:
    """Verify each specialized extracted element supports core contracts, formatting, and locators."""

    def test_heading_element_contract_and_formatting(self) -> None:
        """HEADING element supports content, level, section_heading, and markdown."""
        heading = HeadingElement(
            text="Executive Compensation",
            level=2,
            page_number=3,
            char_start=100,
            char_end=124,
        )
        assert heading.element_type == ElementType.HEADING
        assert heading.content == "## Executive Compensation"
        assert heading.text == "Executive Compensation"
        assert heading.level == 2
        assert heading.page_number == 3
        assert heading.section_heading == "Executive Compensation"
        assert heading.char_start == 100
        assert heading.char_end == 124
        assert heading.locator.page_number == 3
        assert heading.locator.section_heading == "Executive Compensation"

    def test_heading_element_level_validation(self) -> None:
        """Heading level must be in [1, 6]."""
        with pytest.raises(ValidationError, match="Input should be greater than or equal to 1"):
            HeadingElement(text="Title", level=0)

        with pytest.raises(ValidationError, match="Input should be less than or equal to 6"):
            HeadingElement(text="Title", level=7)

    def test_paragraph_element_contract(self) -> None:
        """PARAGRAPH element supports narrative content, page, and offsets."""
        para = ParagraphElement(
            text="Annual bonus distribution is approved quarterly.",
            page_number=4,
            section_heading="Compensation",
            char_start=200,
            char_end=249,
        )
        assert para.element_type == ElementType.PARAGRAPH
        assert para.content == "Annual bonus distribution is approved quarterly."
        assert para.page_number == 4
        assert para.section_heading == "Compensation"
        assert para.char_start == 200
        assert para.char_end == 249
        assert para.sheet_name is None
        assert para.row_range is None

    def test_paragraph_element_content_bridge(self) -> None:
        """PARAGRAPH element can be initialized using 'content' keyword directly."""
        para = ParagraphElement(content="Direct content text.")
        assert para.content == "Direct content text."
        assert para.text == "Direct content text."

    def test_table_element_contract_and_markdown(self) -> None:
        """TABLE element supports headers, rows, caption, and markdown table rendering."""
        table = TableElement(
            headers=["Department", "Q1 Budget", "Q2 Budget"],
            rows=[
                ["Engineering", "$1.2M", "$1.4M"],
                ["Marketing", "$500K", "$600K"],
            ],
            caption="FY2026 Quarterly Allocations",
            sheet_name="Budget_Matrix",
            row_range="Row 1:3",
        )
        assert table.element_type == ElementType.TABLE
        assert table.sheet_name == "Budget_Matrix"
        assert table.row_range == "Row 1:3"
        assert "**Table: FY2026 Quarterly Allocations**" in table.content
        assert "| Department | Q1 Budget | Q2 Budget |" in table.content
        assert "| Engineering | $1.2M | $1.4M |" in table.content
        assert table.locator.sheet_name == "Budget_Matrix"
        assert table.locator.row_range == "Row 1:3"

    def test_list_element_unordered_and_ordered(self) -> None:
        """LIST element supports bulleted and numbered items."""
        # Unordered list
        bullet_list = ListElement(
            items=["Requirement A", "Requirement B", "Requirement C"],
            ordered=False,
            page_number=2,
            section_heading="Pre-requisites",
        )
        assert bullet_list.element_type == ElementType.LIST
        assert bullet_list.page_number == 2
        assert bullet_list.content == "- Requirement A\n- Requirement B\n- Requirement C"

        # Ordered list
        num_list = ListElement(
            items=["First step", "Second step"],
            ordered=True,
            start_index=1,
            page_number=2,
        )
        assert num_list.content == "1. First step\n2. Second step"

    def test_text_block_element_contract(self) -> None:
        """TEXT_BLOCK element supports generic or unclassified text blocks."""
        block = TextBlockElement(
            content="Standard text block with arbitrary content.",
            page_number=5,
        )
        assert block.element_type == ElementType.TEXT_BLOCK
        assert block.content == "Standard text block with arbitrary content."
        assert block.page_number == 5

    def test_spreadsheet_row_element_contract(self) -> None:
        """SPREADSHEET_ROW element supports row_index, cells, and default row_range."""
        row = SpreadsheetRowElement(
            sheet_name="Personnel",
            row_index=18,
            cells={"EmployeeID": "EMP-001", "Name": "Alice Smith", "Role": "Director"},
        )
        assert row.element_type == ElementType.SPREADSHEET_ROW
        assert row.sheet_name == "Personnel"
        assert row.row_index == 18
        assert row.row_range == "Row 18"
        assert row.locator.sheet_name == "Personnel"
        assert row.locator.row_range == "Row 18"
        assert "| Sheet | Row | EmployeeID | Name | Role |" in row.content
        assert "| Personnel | 18 | EMP-001 | Alice Smith | Director |" in row.content

    def test_spreadsheet_row_invalid_index(self) -> None:
        """Spreadsheet row index must be 1-based (>= 1)."""
        with pytest.raises(ValidationError, match="Input should be greater than or equal to 1"):
            SpreadsheetRowElement(sheet_name="S1", row_index=0, cells={})


class TestNormalizedElementRepresentation:
    """Verify that all elements produce the uniform NormalizedElement representation."""

    def test_all_elements_support_9_core_fields(self) -> None:
        """Verify each element implements: content, element_type, page_number, section_heading,
        sheet_name, row_range, char_start, char_end, metadata."""
        elements = [
            HeadingElement(text="Header", level=1, page_number=1, char_start=0, char_end=8),
            ParagraphElement(text="Body", page_number=1, char_start=10, char_end=14),
            TableElement(headers=["A"], rows=[["1"]], sheet_name="S1", row_range="Row 1:2"),
            ListElement(items=["Item 1"], page_number=2),
            TextBlockElement(text="Block", page_number=2),
            SpreadsheetRowElement(sheet_name="S1", row_index=5, cells={"A": 1}),
        ]

        for elem in elements:
            assert hasattr(elem, "content")
            assert hasattr(elem, "element_type")
            assert hasattr(elem, "page_number")
            assert hasattr(elem, "section_heading")
            assert hasattr(elem, "sheet_name")
            assert hasattr(elem, "row_range")
            assert hasattr(elem, "char_start")
            assert hasattr(elem, "char_end")
            assert hasattr(elem, "metadata")
            assert isinstance(elem.content, str)
            assert len(elem.content) > 0

    def test_to_normalized_conversion(self) -> None:
        """Verify .to_normalized() generates a NormalizedElement with preserved attributes."""
        heading = HeadingElement(
            text="Architecture Overview",
            level=1,
            page_number=1,
            char_start=0,
            char_end=23,
            metadata={"font": "Arial-Bold"},
        )
        norm = heading.to_normalized()
        assert isinstance(norm, NormalizedElement)
        assert norm.content == "# Architecture Overview"
        assert norm.element_type == ElementType.HEADING
        assert norm.page_number == 1
        assert norm.section_heading == "Architecture Overview"
        assert norm.char_start == 0
        assert norm.char_end == 23
        assert norm.metadata["font"] == "Arial-Bold"

    def test_normalized_to_concrete_element_roundtrip(self) -> None:
        """Verify NormalizedElement can be converted back to concrete typed elements."""
        norm_para = NormalizedElement(
            content="Sample narrative paragraph.",
            element_type=ElementType.PARAGRAPH,
            page_number=2,
            section_heading="Introduction",
            char_start=50,
            char_end=77,
        )
        concrete = norm_para.to_concrete_element()
        assert isinstance(concrete, ParagraphElement)
        assert concrete.content == "Sample narrative paragraph."
        assert concrete.page_number == 2
        assert concrete.section_heading == "Introduction"

        norm_heading = NormalizedElement(
            content="# Top Title",
            element_type=ElementType.HEADING,
            page_number=1,
            metadata={"level": 1},
        )
        concrete_h = norm_heading.to_concrete_element()
        assert isinstance(concrete_h, HeadingElement)
        assert concrete_h.level == 1
        assert concrete_h.text == "Top Title"

    def test_normalized_element_validation_rejection(self) -> None:
        """NormalizedElement rejects invalid coordinates."""
        with pytest.raises(ValidationError, match="char_start .* cannot exceed char_end"):
            NormalizedElement(
                content="test",
                element_type=ElementType.PARAGRAPH,
                char_start=100,
                char_end=50,
            )

        with pytest.raises(ValidationError, match="row numbers must be 1-based"):
            NormalizedElement(
                content="test",
                element_type=ElementType.SPREADSHEET_ROW,
                row_range="Row 0",
            )


class TestReadingOrderPreservation:
    """Verify reading order preservation, sequence indexing, and sorting."""

    def test_preserve_reading_order_in_extraction_result(self) -> None:
        """ExtractionResult automatically numbers elements in 0-indexed reading order."""
        elements = [
            HeadingElement(text="Doc Title", level=1),
            ParagraphElement(text="Intro paragraph."),
            ListElement(items=["Item 1", "Item 2"]),
            TableElement(headers=["Col 1"], rows=[["Val 1"]]),
        ]

        result = ExtractionResult(
            document_id=str(uuid.uuid4()),
            document_type=DocumentType.DOCX,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            elements=elements,
            metadata=DocumentMetadata(
                document_id=str(uuid.uuid4()),
                tenant_id=str(uuid.uuid4()),
                filename="manual.docx",
                file_hash_sha256="c" * 64,
                file_size_bytes=2048,
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                document_type=DocumentType.DOCX,
            ),
        )

        result.preserve_reading_order()
        for idx, elem in enumerate(result.elements):
            assert elem.reading_order == idx

        ordered = result.get_elements_in_reading_order()
        assert [e.reading_order for e in ordered] == [0, 1, 2, 3]

    def test_reading_order_sorting(self) -> None:
        """get_elements_in_reading_order correctly sorts shuffled elements."""
        e0 = ParagraphElement(text="First", reading_order=0)
        e1 = ParagraphElement(text="Second", reading_order=1)
        e2 = ParagraphElement(text="Third", reading_order=2)

        result = ExtractionResult(
            document_id=str(uuid.uuid4()),
            document_type=DocumentType.TXT,
            mime_type="text/plain",
            elements=[e2, e0, e1],  # Intentionally out of order
            metadata=DocumentMetadata(
                document_id=str(uuid.uuid4()),
                tenant_id=str(uuid.uuid4()),
                filename="notes.txt",
                file_hash_sha256="d" * 64,
                file_size_bytes=100,
                mime_type="text/plain",
                document_type=DocumentType.TXT,
            ),
        )

        ordered = result.get_elements_in_reading_order()
        assert ordered[0].content == "First"
        assert ordered[1].content == "Second"
        assert ordered[2].content == "Third"


class TestCanonicalTextOffsets:
    """Verify compute_canonical_offsets produces verifiable character slices."""

    def test_compute_canonical_offsets_verification(self) -> None:
        """Verify canonical text offsets allow exact slice retrieval."""
        h = HeadingElement(text="HR Policy", level=1, page_number=1)
        p = ParagraphElement(text="Employees are entitled to 15 annual vacation days.", page_number=1)
        t = TableElement(headers=["Tier", "Days"], rows=[["1", "15"], ["2", "20"]], page_number=1)

        result = ExtractionResult(
            document_id=str(uuid.uuid4()),
            document_type=DocumentType.PDF,
            mime_type="application/pdf",
            elements=[h, p, t],
            metadata=DocumentMetadata(
                document_id=str(uuid.uuid4()),
                tenant_id=str(uuid.uuid4()),
                filename="policy.pdf",
                file_hash_sha256="e" * 64,
                file_size_bytes=5000,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
            ),
        )

        result.compute_canonical_offsets(separator="\n\n")
        canonical = result.raw_text
        assert canonical is not None

        # Verify each element's offsets slice out its exact content
        for elem in result.elements:
            assert elem.char_start is not None
            assert elem.char_end is not None
            assert elem.char_end - elem.char_start == len(elem.content)
            extracted_slice = canonical[elem.char_start : elem.char_end]
            assert extracted_slice == elem.content

            # Verify locator is also in sync
            assert elem.locator.char_start == elem.char_start
            assert elem.locator.char_end == elem.char_end
            assert elem.locator.slice_canonical_text(canonical) == elem.content


class TestSourceTraceabilityValidation:
    """Verify that every non-empty extracted element can be traced back to its source."""

    def test_traceable_element_with_page(self) -> None:
        """Element with page_number is traceable."""
        para = ParagraphElement(text="Grounded policy statement.", page_number=3)
        assert para.has_source_traceability()
        assert para.validate_traceability(strict=True)

    def test_traceable_element_with_heading(self) -> None:
        """Heading element automatically carries section_heading provenance."""
        heading = HeadingElement(text="Overview", level=1)
        assert heading.has_source_traceability()
        assert heading.validate_traceability(strict=True)

    def test_traceable_element_with_sheet_and_row(self) -> None:
        """Spreadsheet element with sheet_name is traceable."""
        row = SpreadsheetRowElement(sheet_name="Q1_Data", row_index=4, cells={"X": 10})
        assert row.has_source_traceability()
        assert row.validate_traceability(strict=True)

    def test_traceable_element_with_char_offsets(self) -> None:
        """Text element with char offsets is traceable."""
        block = TextBlockElement(text="Snippet", char_start=100, char_end=107)
        assert block.has_source_traceability()
        assert block.validate_traceability(strict=True)

    def test_untraceable_non_empty_element_rejection(self) -> None:
        """Non-empty element lacking any coordinates fails traceability validation."""
        orphan = ParagraphElement(text="Unattributed text with no coordinates.")
        assert not orphan.has_source_traceability()

        with pytest.raises(ValueError, match="Untraceable extracted element"):
            orphan.validate_traceability(strict=True)

    def test_empty_element_passes_traceability(self) -> None:
        """Empty or whitespace-only element does not fail traceability."""
        empty_para = ParagraphElement(text="")
        assert empty_para.validate_traceability(strict=True)


class TestSerializationAndDeserialization:
    """Verify dictionary and JSON round-trips for elements and extraction results."""

    def test_element_to_dict_and_to_json(self) -> None:
        """Verify element serialization outputs expected normalized keys."""
        para = ParagraphElement(
            text="Corporate code of conduct.",
            page_number=1,
            section_heading="Ethics",
            char_start=0,
            char_end=26,
            metadata={"font_size": 11.5},
        )
        d = para.to_dict()
        assert d["content"] == "Corporate code of conduct."
        assert d["element_type"] == "paragraph"
        assert d["page_number"] == 1
        assert d["section_heading"] == "Ethics"
        assert d["char_start"] == 0
        assert d["char_end"] == 26
        assert d["metadata"]["font_size"] == 11.5
        assert d["locator"]["page_number"] == 1

        json_str = para.to_json()
        loaded = json.loads(json_str)
        assert loaded["content"] == "Corporate code of conduct."

    def test_discriminated_union_polymorphic_parsing_all_types(self) -> None:
        """Verify TypeAdapter(list[ExtractedElement]) polymorphically parses all supported types."""
        raw_data = [
            {"element_type": "heading", "text": "H1 Title", "level": 1, "page_number": 1},
            {"element_type": "paragraph", "text": "Body text", "page_number": 1},
            {"element_type": "table", "headers": ["A"], "rows": [["1"]], "sheet_name": "S1"},
            {"element_type": "list", "items": ["Item A", "Item B"], "page_number": 2},
            {"element_type": "text_block", "text": "Raw block", "page_number": 2},
            {"element_type": "spreadsheet_row", "sheet_name": "S1", "row_index": 2, "cells": {"A": 1}},
        ]

        adapter = TypeAdapter(list[ExtractedElement])
        elements = adapter.validate_python(raw_data)

        assert len(elements) == 6
        assert isinstance(elements[0], HeadingElement)
        assert isinstance(elements[1], ParagraphElement)
        assert isinstance(elements[2], TableElement)
        assert isinstance(elements[3], ListElement)
        assert isinstance(elements[4], TextBlockElement)
        assert isinstance(elements[5], SpreadsheetRowElement)

    def test_extraction_result_json_roundtrip(self) -> None:
        """Verify complete ExtractionResult serialization and deserialization."""
        metadata = DocumentMetadata(
            document_id=str(uuid.uuid4()),
            tenant_id=str(uuid.uuid4()),
            filename="report.pdf",
            file_hash_sha256="f" * 64,
            file_size_bytes=4096,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
        )

        elements: list[ExtractedElement] = [
            HeadingElement(text="Financial Summary", level=1, page_number=1),
            ParagraphElement(text="Revenues grew by 15% YoY.", page_number=1),
            TableElement(headers=["Quarter", "Rev"], rows=[["Q1", "$10M"]], page_number=2),
        ]

        stats = ExtractionStats(
            extractor_name="TestExtractor",
            duration_seconds=0.42,
            total_elements=3,
            total_pages=2,
            ocr_applied=False,
        )

        original = ExtractionResult(
            document_id=metadata.document_id,
            document_type=DocumentType.PDF,
            mime_type="application/pdf",
            elements=elements,
            metadata=metadata,
            stats=stats,
        )

        # JSON round-trip
        json_str = original.to_json()
        recovered = ExtractionResult.from_json(json_str)

        assert recovered.document_id == original.document_id
        assert len(recovered.elements) == 3
        assert isinstance(recovered.elements[0], HeadingElement)
        assert isinstance(recovered.elements[1], ParagraphElement)
        assert isinstance(recovered.elements[2], TableElement)
        assert recovered.stats is not None
        assert recovered.stats.extractor_name == "TestExtractor"
        assert recovered.stats.duration_seconds == 0.42
