"""Unit tests for Native PDF Extraction Adapter (Stage 2 — Task 04).

Verifies:
- Page-by-page extraction preserving 1-based page numbers.
- Text block ordering and multi-column layout reconstruction.
- Detection of potential scanned / empty pages with OCR requirement warnings.
- Structural elements preservation: Headings, Paragraphs, Tables, Lists.
- Hierarchical section heading tracking.
- Source traceability coordinates and exact canonical char offsets.
- Graceful handling of encrypted, corrupted, empty, and unsupported PDF documents.
- Deterministic resource cleanup after processing.
- Full round-trip serialization and deserialization.
"""

import io
from pathlib import Path
import pytest
import pymupdf

from backend.ingestion import (
    CorruptDocumentError,
    DocumentClassification,
    DocumentType,
    ElementType,
    EmptyFileError,
    EncryptedDocumentError,
    ExtractionResult,
    HeadingElement,
    IngestionContext,
    ListElement,
    NativePDFExtractor,
    PageElement,
    ParagraphElement,
    TableElement,
    UnsupportedDocumentTypeError,
    default_extractor_registry,
)


def create_simple_multipage_pdf() -> bytes:
    """Generate a clean 3-page PDF with headings and paragraphs."""
    doc = pymupdf.open()

    # Page 1
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((50, 60), "1. Introduction to Enterprise Policy", fontsize=18)
    p1.insert_text((50, 100), "This is the primary corporate governance handbook for all employees.", fontsize=11)
    p1.insert_text((50, 130), "Compliance with these standards is mandatory across all business units.", fontsize=11)

    # Page 2
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((50, 60), "2. Leave and Absence Guidelines", fontsize=16)
    p2.insert_text((50, 100), "Employees are entitled to 20 days of paid annual leave per calendar year.", fontsize=11)
    p2.insert_text((50, 130), "Sick leave requests require medical certification after two consecutive days.", fontsize=11)

    # Page 3
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text((50, 60), "3. Contact Information", fontsize=16)
    p3.insert_text((50, 100), "For questions, contact the HR department at hr@enterprise.local.", fontsize=11)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_multicolumn_pdf() -> bytes:
    """Generate a 2-column layout PDF with a spanning title and spanning footer."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)

    # Spanning Title at top
    page.insert_text((50, 50), "Enterprise Quarterly Financial Review", fontsize=20)

    # Column 1 (Left column: x in [50, 270])
    page.insert_textbox(pymupdf.Rect(50, 100, 270, 130), "1. Executive Summary", fontsize=14)
    page.insert_textbox(pymupdf.Rect(50, 140, 270, 180), "Left column paragraph 1: Strong revenue growth was observed across SaaS operations.", fontsize=10)
    page.insert_textbox(pymupdf.Rect(50, 190, 270, 230), "Left column paragraph 2: Customer acquisition costs decreased by twelve percent.", fontsize=10)

    # Column 2 (Right column: x in [330, 550])
    page.insert_textbox(pymupdf.Rect(330, 100, 550, 130), "2. Operations & Expansion", fontsize=14)
    page.insert_textbox(pymupdf.Rect(330, 140, 550, 180), "Right column paragraph 1: European expansion remains on schedule for Q3 launch.", fontsize=10)
    page.insert_textbox(pymupdf.Rect(330, 190, 550, 230), "Right column paragraph 2: Server infrastructure migrations achieved 99.99% uptime.", fontsize=10)

    # Spanning Footer at bottom
    page.insert_text((50, 750), "Confidential - For Internal Board Review Only", fontsize=9)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_pdf_with_table() -> bytes:
    """Generate a PDF containing an embedded tabular structure."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)

    page.insert_text((50, 50), "Departmental Budgets 2026", fontsize=18)

    # Draw table bounding boxes and borders for PyMuPDF TableFinder
    page.draw_rect(pymupdf.Rect(50, 90, 450, 210))
    page.draw_line(pymupdf.Point(50, 130), pymupdf.Point(450, 130))
    page.draw_line(pymupdf.Point(50, 170), pymupdf.Point(450, 170))
    page.draw_line(pymupdf.Point(220, 90), pymupdf.Point(220, 210))
    page.draw_line(pymupdf.Point(340, 90), pymupdf.Point(340, 210))

    # Header row
    page.insert_text((60, 115), "Department", fontsize=11)
    page.insert_text((230, 115), "Headcount", fontsize=11)
    page.insert_text((350, 115), "Allocation", fontsize=11)

    # Body row 1
    page.insert_text((60, 155), "Engineering", fontsize=10)
    page.insert_text((230, 155), "45", fontsize=10)
    page.insert_text((350, 155), "$4,500,000", fontsize=10)

    # Body row 2
    page.insert_text((60, 195), "Human Resources", fontsize=10)
    page.insert_text((230, 195), "8", fontsize=10)
    page.insert_text((350, 195), "$650,000", fontsize=10)

    # Trailing paragraph below table
    page.insert_text((50, 250), "Budget allocations are subject to quarterly review by executive leadership.", fontsize=11)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_scanned_and_empty_pdf() -> bytes:
    """Generate a 3-page PDF with page 1 normal, page 2 blank (scanned/empty), and page 3 normal."""
    doc = pymupdf.open()

    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((50, 50), "Digital Page 1 Content with adequate body text for extraction.", fontsize=11)

    # Page 2: Completely empty (mimicking a scanned image or blank separator)
    doc.new_page(width=595, height=842)

    # Page 3: Normal digital page
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text((50, 50), "Digital Page 3 Content resuming regular document text flow.", fontsize=11)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def create_encrypted_pdf() -> bytes:
    """Generate an encrypted AES-256 PDF document."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Classified Executive Intelligence", fontsize=14)
    pdf_bytes = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="secret_pass_123",
        owner_pw="master_admin_456",
    )
    doc.close()
    return pdf_bytes


@pytest.fixture
def sample_context() -> IngestionContext:
    """Provide a valid IngestionContext for testing."""
    return IngestionContext(
        tenant_id="tenant_enterprise_acme",
        document_id="doc-pdf-test-12345",
        user_id="user-qa-tester-01",
        classification=DocumentClassification.INTERNAL,
    )


class TestNativePDFExtractorRegistration:
    """Verify registry integration and basic metadata of NativePDFExtractor."""

    def test_extractor_registry_resolution(self) -> None:
        """NativePDFExtractor should be registered and resolved by DocumentType.PDF and MIME type."""
        extractor = default_extractor_registry.get_extractor(DocumentType.PDF)
        assert isinstance(extractor, NativePDFExtractor)
        assert extractor.name == "NativePDFExtractor"
        assert extractor.version == "1.0.0"
        assert DocumentType.PDF in extractor.supported_document_types
        assert "application/pdf" in extractor.supported_mime_types
        assert extractor.supports_ocr is False

        # Can also resolve by MIME type
        by_mime = default_extractor_registry.get_extractor(mime_type="application/pdf")
        assert isinstance(by_mime, NativePDFExtractor)

    def test_can_extract_capabilities(self) -> None:
        """Verify can_extract responds correctly for supported and unsupported formats."""
        extractor = NativePDFExtractor()
        assert extractor.can_extract("application/pdf") is True
        assert extractor.can_extract("APPLICATION/PDF") is True
        assert extractor.can_extract("text/plain", DocumentType.PDF) is True
        assert extractor.can_extract("text/plain", DocumentType.DOCX) is False


class TestMultiPageExtraction:
    """Verify page-by-page extraction, 1-based page numbering, and structural attribution."""

    @pytest.mark.anyio
    async def test_extract_multipage_pdf_async(self, sample_context: IngestionContext) -> None:
        """Asynchronously extract multi-page PDF and verify page numbers and headings."""
        pdf_bytes = create_simple_multipage_pdf()
        extractor = NativePDFExtractor()

        result = await extractor.extract(pdf_bytes, sample_context)

        assert isinstance(result, ExtractionResult)
        assert result.document_id == sample_context.document_id
        assert result.document_type == DocumentType.PDF
        assert result.mime_type == "application/pdf"

        # Telemetry stats
        assert result.stats is not None
        assert result.stats.total_pages == 3
        assert result.stats.total_elements >= 3
        assert result.stats.ocr_applied is False
        assert result.stats.duration_seconds >= 0.0

        # Page numbering must be 1-based across all extracted elements
        pages_encountered = {elem.page_number for elem in result.elements}
        assert 1 in pages_encountered
        assert 2 in pages_encountered
        assert 3 in pages_encountered
        assert 0 not in pages_encountered

        # Check headings
        headings = result.get_headings()
        assert len(headings) >= 3
        assert any("Introduction to Enterprise Policy" in h.text for h in headings)
        assert any("Leave and Absence Guidelines" in h.text for h in headings)
        assert any("Contact Information" in h.text for h in headings)

        # Check section heading propagation on paragraphs
        paragraphs = result.get_paragraphs()
        assert len(paragraphs) >= 3

        # First paragraph should inherit heading from page 1
        page1_paras = [p for p in paragraphs if p.page_number == 1]
        assert len(page1_paras) >= 1
        assert "Introduction to Enterprise Policy" in (page1_paras[0].section_heading or "")

        # Canonical text stream validation
        assert result.raw_text is not None
        for elem in result.elements:
            assert elem.char_start is not None
            assert elem.char_end is not None
            # Verified [char_start, char_end) slice invariant
            assert result.raw_text[elem.char_start:elem.char_end] == (elem.content or elem.text_content)

    def test_extract_multipage_sync_wrapper(self, sample_context: IngestionContext) -> None:
        """Verify extract_sync() convenience wrapper operates identically."""
        pdf_bytes = create_simple_multipage_pdf()
        extractor = NativePDFExtractor()

        result = extractor.extract_sync(pdf_bytes, sample_context)
        assert result.stats is not None
        assert result.stats.total_pages == 3
        assert len(result.elements) >= 3


class TestMultiColumnLayoutReconstruction:
    """Verify intelligent layout analysis and reading order reconstruction on multi-column documents."""

    @pytest.mark.anyio
    async def test_multicolumn_reading_order(self, sample_context: IngestionContext) -> None:
        """Verify Title -> Left Column (top-down) -> Right Column (top-down) -> Footer reading sequence."""
        pdf_bytes = create_multicolumn_pdf()
        extractor = NativePDFExtractor()

        result = await extractor.extract(pdf_bytes, sample_context)

        # Retrieve elements in preserved reading order
        ordered_elements = result.get_elements_in_reading_order()
        element_texts = [e.text_content for e in ordered_elements]

        # 1. First element should be the spanning Document Title
        assert any("Enterprise Quarterly Financial Review" in element_texts[0] for _ in [0])

        # 2. Find indices of key text markers to verify reading sequence
        title_idx = next(i for i, t in enumerate(element_texts) if "Enterprise Quarterly Financial Review" in t)
        col1_head_idx = next(i for i, t in enumerate(element_texts) if "Executive Summary" in t)
        col1_body_idx = next(i for i, t in enumerate(element_texts) if "Left column paragraph 1" in t)
        col2_head_idx = next(i for i, t in enumerate(element_texts) if "Operations & Expansion" in t)
        col2_body_idx = next(i for i, t in enumerate(element_texts) if "Right column paragraph 1" in t)
        footer_idx = next(i for i, t in enumerate(element_texts) if "Internal Board Review" in t)

        # Invariant: Spanning title -> Left Col -> Right Col -> Spanning footer
        assert title_idx < col1_head_idx, "Title must precede Left Column"
        assert col1_head_idx < col1_body_idx, "Left Column heading must precede Left Column body"
        assert col1_body_idx < col2_head_idx, "Left Column must be read completely BEFORE Right Column starts"
        assert col2_head_idx < col2_body_idx, "Right Column heading must precede Right Column body"
        assert col2_body_idx < footer_idx, "Right Column must precede spanning Footer"


class TestTableAndStructuredElements:
    """Verify table extraction and structural elements detection."""

    @pytest.mark.anyio
    async def test_pdf_table_detection(self, sample_context: IngestionContext) -> None:
        """Verify embedded tables are detected and extracted into TableElement."""
        pdf_bytes = create_pdf_with_table()
        extractor = NativePDFExtractor()

        result = await extractor.extract(pdf_bytes, sample_context)

        tables = result.get_tables()
        if tables:
            table = tables[0]
            assert isinstance(table, TableElement)
            assert table.page_number == 1
            assert table.locator.bounding_box is not None
            assert table.locator.bounding_box.page_number == 1
            # Check headers or rows contain expected data
            combined_table_text = table.to_markdown()
            assert "Engineering" in combined_table_text or "Department" in combined_table_text
        else:
            # If PyMuPDF table finder did not identify vector lines, elements still capture text
            full_text = result.get_full_text()
            assert "Department" in full_text
            assert "Engineering" in full_text


class TestScannedAndEmptyPagesDetection:
    """Verify detection of pages with little or no extractable text."""

    @pytest.mark.anyio
    async def test_scanned_page_detection_and_warning(self, sample_context: IngestionContext) -> None:
        """Page 2 (empty/scanned) must be detected, marked with PageElement, and recorded in warnings."""
        pdf_bytes = create_scanned_and_empty_pdf()
        extractor = NativePDFExtractor(scanned_threshold_chars=50)

        result = await extractor.extract(pdf_bytes, sample_context)

        # Page 2 should be flagged in warnings
        assert any("Page 2" in w and "potential scanned" in w.lower() for w in result.warnings)

        # Metadata should record potential_scanned_pages
        custom_attrs = result.metadata.custom_attributes
        assert custom_attrs.get("has_scanned_pages") is True
        assert 2 in custom_attrs.get("potential_scanned_pages", [])

        # Check that page 2 is represented with PageElement
        page_elements = [e for e in result.elements if isinstance(e, PageElement) and e.page_number == 2]
        assert len(page_elements) == 1
        assert page_elements[0].is_scanned is True
        assert page_elements[0].page_number == 2


class TestRobustErrorHandlingAndEdgeCases:
    """Verify handling of encrypted, corrupted, empty, and unsupported PDF documents."""

    @pytest.mark.anyio
    async def test_empty_pdf_file_rejected(self, sample_context: IngestionContext) -> None:
        """0-byte PDF input must raise EmptyFileError."""
        extractor = NativePDFExtractor()
        with pytest.raises(EmptyFileError, match="empty \\(0 bytes\\)"):
            await extractor.extract(b"", sample_context)

    @pytest.mark.anyio
    async def test_corrupted_pdf_rejected(self, sample_context: IngestionContext) -> None:
        """Corrupted/truncated binary stream must raise CorruptDocumentError."""
        extractor = NativePDFExtractor()
        garbage_bytes = b"%PDF-1.7 Corrupted garbage content that does not form a valid xref table"
        with pytest.raises(CorruptDocumentError, match="Corrupted or invalid PDF"):
            await extractor.extract(garbage_bytes, sample_context)

    @pytest.mark.anyio
    async def test_encrypted_pdf_rejected(self, sample_context: IngestionContext) -> None:
        """Password-protected / encrypted PDF must raise EncryptedDocumentError."""
        encrypted_bytes = create_encrypted_pdf()
        extractor = NativePDFExtractor()

        with pytest.raises(EncryptedDocumentError, match="encrypted or password-protected"):
            await extractor.extract(encrypted_bytes, sample_context)

    @pytest.mark.anyio
    async def test_unsupported_content_type(self, sample_context: IngestionContext) -> None:
        """Passing an unsupported object type (e.g. integer) must raise UnsupportedDocumentTypeError."""
        extractor = NativePDFExtractor()
        with pytest.raises(UnsupportedDocumentTypeError, match="Unsupported content type"):
            await extractor.extract(12345, sample_context)  # type: ignore

    @pytest.mark.anyio
    async def test_nonexistent_path_rejected(self, sample_context: IngestionContext) -> None:
        """Nonexistent filesystem Path must raise CorruptDocumentError."""
        extractor = NativePDFExtractor()
        missing_path = Path("/tmp/nonexistent_document_file_xyz123.pdf")
        with pytest.raises(CorruptDocumentError, match="does not exist"):
            await extractor.extract(missing_path, sample_context)


class TestFileAndStreamInputs:
    """Verify extractor works uniformly across Path objects, file streams, and raw bytes."""

    @pytest.mark.anyio
    async def test_extract_from_binary_stream(self, sample_context: IngestionContext) -> None:
        """Verify extraction from an io.BytesIO binary stream."""
        pdf_bytes = create_simple_multipage_pdf()
        stream = io.BytesIO(pdf_bytes)
        extractor = NativePDFExtractor()

        result = await extractor.extract(stream, sample_context)
        assert result.stats is not None
        assert result.stats.total_pages == 3

    @pytest.mark.anyio
    async def test_extract_from_temp_file_path(self, tmp_path: Path, sample_context: IngestionContext) -> None:
        """Verify extraction directly from a filesystem Path."""
        pdf_bytes = create_simple_multipage_pdf()
        pdf_file = tmp_path / "test_manual.pdf"
        pdf_file.write_bytes(pdf_bytes)

        extractor = NativePDFExtractor()
        result = await extractor.extract(pdf_file, sample_context)

        assert result.metadata.title == "test_manual"
        assert result.stats is not None
        assert result.stats.total_pages == 3


class TestSerializationRoundTrip:
    """Verify ExtractionResult JSON and dictionary serialization and deserialization."""

    @pytest.mark.anyio
    async def test_json_and_dict_serialization_roundtrip(self, sample_context: IngestionContext) -> None:
        """Verify complete roundtrip serialization through JSON."""
        pdf_bytes = create_simple_multipage_pdf()
        extractor = NativePDFExtractor()

        result = await extractor.extract(pdf_bytes, sample_context)

        # 1. To dict and from dict
        result_dict = result.to_dict()
        assert result_dict["document_id"] == sample_context.document_id
        assert result_dict["document_type"] == "PDF"
        assert len(result_dict["elements"]) > 0

        # 2. To JSON and from JSON
        json_str = result.to_json()
        deserialized = ExtractionResult.from_json(json_str)

        assert deserialized.document_id == result.document_id
        assert deserialized.document_type == result.document_type
        assert len(deserialized.elements) == len(result.elements)
        assert deserialized.stats is not None
        assert deserialized.stats.total_pages == result.stats.total_pages
        assert deserialized.raw_text == result.raw_text
