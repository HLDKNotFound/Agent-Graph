"""Unit tests for XLSX and CSV Structured Document Extraction (Stage 2 — Task 07).

Verifies:
- Multiple worksheet extraction and preservation of original sheet names.
- Preservation of original 1-based worksheet row numbers, header rows, and numbering gaps.
- Merged cell expansion across multi-cell spans.
- Handling of empty cells, missing values, and heterogeneous data types (int, float, date, bool, str).
- Preservation of column-to-value relationships and header disambiguation.
- Formula evaluation policy and fallback to formula expressions when cache is missing.
- Consistency of contracts between XLSX and CSV extractors (TableElement & SpreadsheetRowElement).
- Controlled memory processing: read-only streaming mode and row bounds truncation.
- Rejection of encrypted, password-protected, corrupted, and invalid files.
- Canonical character offset slicing and strict source provenance traceability.
- Serialization and normalized element transformations.
"""

import csv
import datetime
import io
from pathlib import Path
import pytest
import openpyxl

from backend.ingestion import (
    CorruptDocumentError,
    CsvExtractor,
    DocumentClassification,
    DocumentMimeType,
    DocumentType,
    ElementType,
    EmptyFileError,
    EncryptedDocumentError,
    ExtractionResult,
    FileSignatureMismatchError,
    FORMULA_POLICY_DOCUMENTATION,
    IngestionContext,
    NormalizedElement,
    OpenXMLXlsxExtractor,
    SpreadsheetExtractor,
    SpreadsheetRowElement,
    TableElement,
    UnsupportedDocumentTypeError,
    XlsxExtractor,
    default_extractor_registry,
)


@pytest.fixture
def sample_context() -> IngestionContext:
    """Fixture providing standardized multi-tenant IngestionContext."""
    return IngestionContext(
        tenant_id="tenant-corp-test",
        document_id="doc-sheet-test-77889",
        user_id="user-analyst-001",
        department_id="dept-finance",
        classification=DocumentClassification.INTERNAL,
    )


def create_multi_sheet_xlsx() -> bytes:
    """Generate a valid XLSX workbook with 3 distinct worksheets."""
    wb = openpyxl.Workbook()

    # Sheet 1: Engineering
    ws1 = wb.active
    ws1.title = "Engineering"
    ws1.append(["EmployeeID", "Name", "Role", "Salary"])
    ws1.append(["ENG-101", "Alice Chen", "Principal Architect", 175000])
    ws1.append(["ENG-102", "Bob Taylor", "Senior Dev", 140000])

    # Sheet 2: Marketing
    ws2 = wb.create_sheet(title="Marketing")
    ws2.append(["Campaign", "Channel", "Budget", "Conversions"])
    ws2.append(["Brand Awareness 2026", "Social Media", 45000, 1200])
    ws2.append(["Enterprise Outreach", "Direct Mail", 25000, 350])

    # Sheet 3: HR_Policies
    ws3 = wb.create_sheet(title="HR_Policies")
    ws3.append(["PolicyCode", "PolicyName", "EffectiveDate"])
    ws3.append(["HR-01", "Remote Work Protocol", "2026-01-01"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def create_gaps_and_header_xlsx() -> bytes:
    """Generate an XLSX workbook with title rows, header rows, and empty row gaps."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fiscal_Data"

    # Row 1: Title
    ws.cell(row=1, column=1, value="Annual Department Allocations")
    # Row 2: Empty gap

    # Row 3: Headers
    ws.cell(row=3, column=1, value="DeptCode")
    ws.cell(row=3, column=2, value="Department")
    ws.cell(row=3, column=3, value="Allocation")

    # Row 4: Data
    ws.cell(row=4, column=1, value="D01")
    ws.cell(row=4, column=2, value="Engineering")
    ws.cell(row=4, column=3, value=500000)

    # Row 5: Empty gap

    # Row 6: Data
    ws.cell(row=6, column=1, value="D02")
    ws.cell(row=6, column=2, value="Product")
    ws.cell(row=6, column=3, value=350000)

    # Row 7: Empty gap

    # Row 8: Data
    ws.cell(row=8, column=1, value="D03")
    ws.cell(row=8, column=2, value="Operations")
    ws.cell(row=8, column=3, value=200000)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def create_merged_cells_xlsx() -> bytes:
    """Generate an XLSX workbook with vertically merged cells."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Departments"

    # Headers at Row 1
    ws.cell(row=1, column=1, value="Division")
    ws.cell(row=1, column=2, value="Team")
    ws.cell(row=1, column=3, value="Lead")

    # Row 2, 3, 4: Division is 'Technology' merged across rows 2..4
    ws.cell(row=2, column=1, value="Technology")
    ws.merge_cells(start_row=2, start_column=1, end_row=4, end_column=1)

    ws.cell(row=2, column=2, value="Core Infrastructure")
    ws.cell(row=2, column=3, value="Sarah Connor")

    ws.cell(row=3, column=2, value="Data Platform")
    ws.cell(row=3, column=3, value="John Connor")

    ws.cell(row=4, column=2, value="Cybersecurity")
    ws.cell(row=4, column=3, value="Kyle Reese")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def create_heterogeneous_types_xlsx() -> bytes:
    """Generate an XLSX workbook with diverse cell types: int, float, bool, date, string."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DataTypes"

    ws.append(["Item", "Count", "Rate", "Active", "CreatedDate"])
    ws.append([
        "Server Node A",
        42,
        99.5,
        True,
        datetime.date(2026, 3, 15),
    ])
    ws.append([
        "Server Node B",
        100.0,  # Float integer
        0.005,
        False,
        datetime.datetime(2026, 5, 20, 14, 30, 0),
    ])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def create_duplicate_headers_xlsx() -> bytes:
    """Generate an XLSX with duplicated and empty column headers."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DupeHeaders"

    # Duplicated 'Metric' and empty column
    ws.cell(row=1, column=1, value="Metric")
    ws.cell(row=1, column=2, value="")  # Empty header
    ws.cell(row=1, column=3, value="Metric")  # Duplicate header

    ws.cell(row=2, column=1, value="Q1 Score")
    ws.cell(row=2, column=2, value="88%")
    ws.cell(row=2, column=3, value="Q2 Target")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def create_formula_xlsx() -> bytes:
    """Generate an XLSX with formula cells."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Formulas"

    ws.append(["Item", "UnitCost", "Quantity", "TotalCost"])
    ws.append(["Laptops", 1200, 10, "=B2*C2"])
    ws.append(["Monitors", 300, 20, "=B3*C3"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def create_sample_csv() -> bytes:
    """Generate a clean CSV string with commas and quoted text."""
    content = (
        "ProjectID,ProjectName,Lead,Budget\n"
        "PRJ-01,Cloud Migration,Alice Smith,500000\n"
        "PRJ-02,\"AI Agent, Platform\",Bob Jones,750000\n"
        "PRJ-03,Zero-Trust Security,Charlie Brown,300000\n"
    )
    return content.encode("utf-8")


def create_gaps_csv() -> bytes:
    """Generate a CSV string with blank line gaps."""
    content = (
        "EmpID,Name,Department\n"
        "E01,Alice,Engineering\n"
        "\n"
        "E02,Bob,Finance\n"
        "\n"
        "\n"
        "E03,Charlie,Legal\n"
    )
    return content.encode("utf-8")


class TestSpreadsheetExtractorRegistry:
    """Verify registry resolution and aliases for XLSX and CSV extractors."""

    def test_xlsx_extractor_registered(self) -> None:
        """XlsxExtractor resolves by DocumentType.XLSX."""
        extractor = default_extractor_registry.get_extractor(document_type=DocumentType.XLSX)
        assert isinstance(extractor, XlsxExtractor)
        assert extractor.name == "XlsxExtractor"
        assert extractor.version == "1.0.0"

    def test_csv_extractor_registered(self) -> None:
        """CsvExtractor resolves by DocumentType.CSV."""
        extractor = default_extractor_registry.get_extractor(document_type=DocumentType.CSV)
        assert isinstance(extractor, CsvExtractor)
        assert extractor.name == "CsvExtractor"

    def test_mime_type_resolution(self) -> None:
        """MIME types resolve to the correct extractor."""
        xlsx_ext = default_extractor_registry.get_extractor(
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert isinstance(xlsx_ext, XlsxExtractor)

        csv_ext = default_extractor_registry.get_extractor(mime_type="text/csv")
        assert isinstance(csv_ext, CsvExtractor)

    def test_aliases_and_composite(self) -> None:
        """Verify OpenXMLXlsxExtractor alias and composite SpreadsheetExtractor."""
        assert OpenXMLXlsxExtractor is XlsxExtractor
        composite = SpreadsheetExtractor()
        assert DocumentType.XLSX in composite.supported_document_types
        assert DocumentType.CSV in composite.supported_document_types


class TestMultiWorksheetExtraction:
    """Verify extraction across multiple worksheets with sheet names preserved."""

    @pytest.mark.anyio
    async def test_multiple_worksheets_extracted(self, sample_context: IngestionContext) -> None:
        """All sheets in an XLSX workbook are extracted with proper sheet names."""
        xlsx_bytes = create_multi_sheet_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        assert result.metadata.custom_metadata["total_sheets"] == 3
        assert result.metadata.custom_metadata["sheet_names"] == ["Engineering", "Marketing", "HR_Policies"]

        tables = result.get_tables()
        assert len(tables) == 3

        # Verify each table preserves its sheet name
        table_sheets = [t.sheet_name for t in tables]
        assert table_sheets == ["Engineering", "Marketing", "HR_Policies"]

        # Verify rows in first sheet
        t1 = tables[0]
        assert t1.headers == ["EmployeeID", "Name", "Role", "Salary"]
        assert len(t1.rows) == 2
        assert t1.rows[0] == ["ENG-101", "Alice Chen", "Principal Architect", 175000]

        # Verify rows in second sheet
        t2 = tables[1]
        assert t2.headers == ["Campaign", "Channel", "Budget", "Conversions"]
        assert len(t2.rows) == 2

        # Verify individual SpreadsheetRowElements preserve sheet_name
        rows = result.get_spreadsheet_rows()
        assert len(rows) == 5  # 2 in Engineering + 2 in Marketing + 1 in HR_Policies

        eng_rows = [r for r in rows if r.sheet_name == "Engineering"]
        assert len(eng_rows) == 2
        assert eng_rows[0].cells["EmployeeID"] == "ENG-101"
        assert eng_rows[0].cells["Salary"] == 175000


class TestOriginalRowNumbersAndGapsPreservation:
    """Verify original physical row numbers and empty row gaps are preserved."""

    @pytest.mark.anyio
    async def test_physical_row_indices_preserved(self, sample_context: IngestionContext) -> None:
        """Original 1-based worksheet row numbers are preserved through gaps and header rows."""
        xlsx_bytes = create_gaps_and_header_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        tables = result.get_tables()
        assert len(tables) == 1
        tbl = tables[0]

        assert tbl.headers == ["DeptCode", "Department", "Allocation"]
        assert tbl.metadata["header_row_index"] == 3
        # Original row indices of data rows are 4, 6, 8 (not renumbered to 1, 2, 3)
        assert tbl.metadata["original_row_indices"] == [4, 6, 8]
        assert tbl.metadata["gaps"] == [5, 7]

        # Check individual row elements
        rows = result.get_spreadsheet_rows()
        assert len(rows) == 3

        r1, r2, r3 = rows
        assert r1.row_index == 4
        assert r1.row_range == "Row 4"
        assert r1.cells["Department"] == "Engineering"

        assert r2.row_index == 6
        assert r2.row_range == "Row 6"
        assert r2.cells["Department"] == "Product"

        assert r3.row_index == 8
        assert r3.row_range == "Row 8"
        assert r3.cells["Department"] == "Operations"


class TestMergedCellsHandling:
    """Verify merged cell values are expanded across all spanned cells."""

    @pytest.mark.anyio
    async def test_merged_cells_expanded(self, sample_context: IngestionContext) -> None:
        """Vertically merged cells have their top-left value propagated to all covered rows."""
        xlsx_bytes = create_merged_cells_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        rows = result.get_spreadsheet_rows()
        assert len(rows) == 3

        # Division was merged across rows 2..4: all three rows must contain 'Technology'
        for r in rows:
            assert r.cells["Division"] == "Technology"

        # Unique teams
        assert rows[0].cells["Team"] == "Core Infrastructure"
        assert rows[1].cells["Team"] == "Data Platform"
        assert rows[2].cells["Team"] == "Cybersecurity"

        # In TableElement rows
        tbl = result.get_tables()[0]
        assert tbl.rows[0][0] == "Technology"
        assert tbl.rows[1][0] == "Technology"
        assert tbl.rows[2][0] == "Technology"


class TestHeterogeneousDataTypes:
    """Verify conversion of int, float, bool, date, datetime, and string cell types."""

    @pytest.mark.anyio
    async def test_cell_data_types(self, sample_context: IngestionContext) -> None:
        """Ensure primitive and temporal data types are correctly formatted."""
        xlsx_bytes = create_heterogeneous_types_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        rows = result.get_spreadsheet_rows()
        assert len(rows) == 2

        row1, row2 = rows

        # Row 1: int, float, bool, date
        assert row1.cells["Count"] == 42
        assert row1.cells["Rate"] == 99.5
        assert row1.cells["Active"] is True
        assert row1.cells["CreatedDate"] == "2026-03-15"

        # Row 2: whole float normalized to int, bool, datetime
        assert row2.cells["Count"] == 100  # 100.0 normalized to 100
        assert row2.cells["Active"] is False
        assert "2026-05-20" in str(row2.cells["CreatedDate"])


class TestHeaderDisambiguationAndMissingValues:
    """Verify duplicate and missing column headers are disambiguated cleanly."""

    @pytest.mark.anyio
    async def test_duplicate_headers_disambiguated(self, sample_context: IngestionContext) -> None:
        """Duplicate and empty headers receive unique names."""
        xlsx_bytes = create_duplicate_headers_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        tbl = result.get_tables()[0]
        # Expected: Metric, Column_2, Metric_2
        assert tbl.headers == ["Metric", "Column_2", "Metric_2"]

        row = result.get_spreadsheet_rows()[0]
        assert "Metric" in row.cells
        assert "Column_2" in row.cells
        assert "Metric_2" in row.cells
        assert row.cells["Metric"] == "Q1 Score"
        assert row.cells["Column_2"] == "88%"
        assert row.cells["Metric_2"] == "Q2 Target"


class TestFormulaEvaluationPolicy:
    """Verify formula evaluation policy and fallback."""

    @pytest.mark.anyio
    async def test_formula_fallback_when_cached_is_none(self, sample_context: IngestionContext) -> None:
        """When workbook has formulas without cached evaluations, extractor extracts formula string."""
        xlsx_bytes = create_formula_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        rows = result.get_spreadsheet_rows()
        assert len(rows) == 2

        # Formula text fallback
        assert rows[0].cells["TotalCost"] == "=B2*C2"
        assert rows[1].cells["TotalCost"] == "=B3*C3"

    def test_formula_policy_documentation_accessible(self) -> None:
        """Verify FORMULA_POLICY_DOCUMENTATION is exported and documented."""
        assert "Formula Evaluation and Cached Value Policy" in FORMULA_POLICY_DOCUMENTATION
        assert "Primary Mode" in FORMULA_POLICY_DOCUMENTATION


class TestStreamingAndMemoryManagement:
    """Verify streaming read-only mode for large spreadsheets and row limits."""

    @pytest.mark.anyio
    async def test_streaming_mode_execution(self, sample_context: IngestionContext) -> None:
        """Streaming mode extracts rows cleanly via read-only generator."""
        xlsx_bytes = create_multi_sheet_xlsx()
        # Force streaming mode by setting threshold to 0 bytes
        extractor = XlsxExtractor(streaming_threshold_bytes=0)

        result = await extractor.extract(xlsx_bytes, sample_context)

        assert len(result.get_tables()) == 3
        assert len(result.get_spreadsheet_rows()) == 5

    @pytest.mark.anyio
    async def test_row_limit_truncation(self, sample_context: IngestionContext) -> None:
        """Worksheet rows are truncated at max_rows with warning."""
        xlsx_bytes = create_multi_sheet_xlsx()
        extractor = XlsxExtractor(max_rows_per_sheet=1)

        result = await extractor.extract(xlsx_bytes, sample_context)

        # Each sheet has only 1 data row extracted
        for tbl in result.get_tables():
            assert len(tbl.rows) == 1

        assert len(result.warnings) > 0
        assert any("truncated" in w for w in result.warnings)


class TestCsvExtractor:
    """Verify CSV extraction adhering to the same contract as XLSX."""

    @pytest.mark.anyio
    async def test_standard_csv_extraction(self, sample_context: IngestionContext) -> None:
        """Standard CSV produces TableElement and SpreadsheetRowElement."""
        csv_bytes = create_sample_csv()
        extractor = CsvExtractor()

        result = await extractor.extract(csv_bytes, sample_context)

        assert result.metadata.document_type == DocumentType.CSV
        assert result.metadata.mime_type == DocumentMimeType.CSV.value

        tables = result.get_tables()
        assert len(tables) == 1
        tbl = tables[0]

        assert tbl.headers == ["ProjectID", "ProjectName", "Lead", "Budget"]
        assert len(tbl.rows) == 3
        # Quoted comma cell preserved
        assert tbl.rows[1][1] == "AI Agent, Platform"

        rows = result.get_spreadsheet_rows()
        assert len(rows) == 3
        assert rows[0].row_index == 2  # Physical line 2
        assert rows[0].cells["ProjectName"] == "Cloud Migration"
        assert rows[1].cells["ProjectName"] == "AI Agent, Platform"

    @pytest.mark.anyio
    async def test_csv_gaps_preserved(self, sample_context: IngestionContext) -> None:
        """Blank line gaps preserve original 1-based physical line numbers."""
        csv_bytes = create_gaps_csv()
        extractor = CsvExtractor()

        result = await extractor.extract(csv_bytes, sample_context)

        rows = result.get_spreadsheet_rows()
        assert len(rows) == 3

        # Lines in create_gaps_csv:
        # 1: EmpID,Name,Department
        # 2: E01,Alice,Engineering
        # 3: (blank)
        # 4: E02,Bob,Finance
        # 5: (blank)
        # 6: (blank)
        # 7: E03,Charlie,Legal
        assert rows[0].row_index == 2
        assert rows[1].row_index == 4
        assert rows[2].row_index == 7

        tbl = result.get_tables()[0]
        assert tbl.metadata["original_row_indices"] == [2, 4, 7]
        assert tbl.metadata["gaps"] == [3, 5, 6]

    @pytest.mark.anyio
    async def test_tsv_delimiter_detection(self, sample_context: IngestionContext) -> None:
        """Tab-separated CSV values are auto-detected by delimiter sniffer."""
        tsv_content = "ID\tTitle\tStatus\n1\tArchitecture Review\tApproved\n2\tSecurity Audit\tPending\n".encode("utf-8")
        extractor = CsvExtractor()

        result = await extractor.extract(tsv_content, sample_context)

        tbl = result.get_tables()[0]
        assert tbl.headers == ["ID", "Title", "Status"]
        assert len(tbl.rows) == 2


class TestTraceabilityAndCanonicalOffsets:
    """Verify source provenance coordinates and canonical string slicing."""

    @pytest.mark.anyio
    async def test_canonical_offsets_slicing(self, sample_context: IngestionContext) -> None:
        """Canonical text slicing matches element.content for all elements."""
        xlsx_bytes = create_multi_sheet_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        assert result.raw_text is not None
        assert len(result.raw_text) > 0

        for elem in result.elements:
            assert elem.char_start is not None
            assert elem.char_end is not None
            assert elem.char_start < elem.char_end

            sliced = result.raw_text[elem.char_start:elem.char_end]
            expected = elem.content
            assert sliced == expected

    @pytest.mark.anyio
    async def test_traceability_validation(self, sample_context: IngestionContext) -> None:
        """All extracted spreadsheet elements carry valid source coordinates."""
        xlsx_bytes = create_multi_sheet_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        assert result.validate_traceability(strict=True) is True
        for elem in result.elements:
            assert elem.has_source_traceability() is True
            assert elem.sheet_name is not None
            assert elem.row_range is not None

    @pytest.mark.anyio
    async def test_normalized_elements_conversion(self, sample_context: IngestionContext) -> None:
        """Result converts to list of NormalizedElements."""
        xlsx_bytes = create_multi_sheet_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        norm_elements = result.to_normalized_elements()
        assert len(norm_elements) == len(result.elements)
        for norm in norm_elements:
            assert isinstance(norm, NormalizedElement)
            assert norm.sheet_name is not None


class TestErrorHandlingAndEdgeCases:
    """Verify detection of empty, malformed, encrypted, and invalid spreadsheet files."""

    @pytest.mark.anyio
    async def test_empty_xlsx_rejected(self, sample_context: IngestionContext) -> None:
        """0-byte XLSX input raises EmptyFileError."""
        extractor = XlsxExtractor()
        with pytest.raises(EmptyFileError, match="empty \\(0 bytes\\)"):
            await extractor.extract(b"", sample_context)

    @pytest.mark.anyio
    async def test_empty_csv_rejected(self, sample_context: IngestionContext) -> None:
        """0-byte CSV input raises EmptyFileError."""
        extractor = CsvExtractor()
        with pytest.raises(EmptyFileError, match="empty \\(0 bytes\\)"):
            await extractor.extract(b"", sample_context)

    @pytest.mark.anyio
    async def test_corrupt_xlsx_rejected(self, sample_context: IngestionContext) -> None:
        """Corrupted ZIP archive raises CorruptDocumentError."""
        extractor = XlsxExtractor()
        with pytest.raises(CorruptDocumentError):
            await extractor.extract(b"PK\x03\x04corrupted binary stream", sample_context)

    @pytest.mark.anyio
    async def test_encrypted_xlsx_rejected(self, sample_context: IngestionContext) -> None:
        """Password-protected or encrypted workbook raises EncryptedDocumentError."""
        encrypted_ole = (
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100 + b"EncryptedPackage" + b"\x00" * 100
        )
        extractor = XlsxExtractor()
        with pytest.raises(EncryptedDocumentError, match="password-protected or encrypted"):
            await extractor.extract(encrypted_ole, sample_context)

    @pytest.mark.anyio
    async def test_null_bytes_in_csv_rejected(self, sample_context: IngestionContext) -> None:
        """Binary null bytes in CSV raise FileSignatureMismatchError."""
        bad_csv = b"Col1,Col2\nVal1,\x00\x00Binary\n"
        extractor = CsvExtractor()
        with pytest.raises(FileSignatureMismatchError, match="null bytes"):
            await extractor.extract(bad_csv, sample_context)

    @pytest.mark.anyio
    async def test_nonexistent_file_path(self, sample_context: IngestionContext) -> None:
        """Nonexistent Path raises CorruptDocumentError."""
        extractor = XlsxExtractor()
        with pytest.raises(CorruptDocumentError, match="does not exist"):
            await extractor.extract(Path("/tmp/nonexistent_sheet_file_9876.xlsx"), sample_context)

    @pytest.mark.anyio
    async def test_unsupported_content_type(self, sample_context: IngestionContext) -> None:
        """Unsupported content type raises UnsupportedDocumentTypeError."""
        extractor = XlsxExtractor()
        with pytest.raises(UnsupportedDocumentTypeError):
            await extractor.extract(12345, sample_context)  # type: ignore

    @pytest.mark.anyio
    async def test_empty_worksheet_warning(self, sample_context: IngestionContext) -> None:
        """Workbook with empty worksheet records a warning and empty status."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "BlankSheet"
        buf = io.BytesIO()
        wb.save(buf)

        extractor = XlsxExtractor()
        result = await extractor.extract(buf.getvalue(), sample_context)

        assert len(result.elements) == 0
        assert len(result.warnings) > 0
        assert result.metadata.custom_metadata["is_empty"] is True


class TestInputPolymorphismAndSerialization:
    """Verify bytes, streams, Path objects, and JSON round-trip serialization."""

    @pytest.mark.anyio
    async def test_extract_from_path(self, tmp_path: Path, sample_context: IngestionContext) -> None:
        """Extraction works directly from a filesystem Path."""
        xlsx_bytes = create_multi_sheet_xlsx()
        file_path = tmp_path / "company_worksheets.xlsx"
        file_path.write_bytes(xlsx_bytes)

        extractor = XlsxExtractor()
        result = await extractor.extract(file_path, sample_context)

        assert result.metadata.filename == "company_worksheets.xlsx"
        assert len(result.get_tables()) == 3

    @pytest.mark.anyio
    async def test_json_roundtrip_serialization(self, sample_context: IngestionContext) -> None:
        """ExtractionResult can be fully serialized and deserialized via JSON."""
        xlsx_bytes = create_multi_sheet_xlsx()
        extractor = XlsxExtractor()

        result = await extractor.extract(xlsx_bytes, sample_context)

        json_str = result.to_json()
        deserialized = ExtractionResult.from_json(json_str)

        assert deserialized.document_id == result.document_id
        assert deserialized.document_type == result.document_type
        assert len(deserialized.elements) == len(result.elements)
        assert deserialized.raw_text == result.raw_text
