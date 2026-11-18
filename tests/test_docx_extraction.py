"""Unit tests for Microsoft Word DOCX Extraction Adapter (Stage 2 — Task 06).

Verifies:
- Document order preservation across interleaved headings, paragraphs, tables, and lists.
- Heading hierarchy detection: Heading 1, Heading 2, Heading 3, Title, Subtitle.
- Section hierarchy tracking and association with nearest parent heading.
- Table extraction: headers, rows, cell contents, and Markdown representation.
- List extraction: bulleted and numbered lists with Markdown formatting.
- Source traceability coordinates: section_heading, char_start, char_end, reading_order.
- Exact canonical text stream slicing consistency.
- Empty document detection and malformed / corrupted archive rejection.
- Password-protected and encrypted document detection.
- Input polymorphism: bytes, io.BytesIO stream, and Path.
- Extractor registry integration for DocumentType.DOCX and MIME types.
- Complete round-trip serialization and deserialization.
"""

import io
from pathlib import Path
import pytest
import docx

from backend.ingestion import (
    CorruptDocumentError,
    DocxExtractor,
    DocumentClassification,
    DocumentMimeType,
    DocumentType,
    ElementType,
    EmptyFileError,
    EncryptedDocumentError,
    ExtractionResult,
    HeadingElement,
    IngestionContext,
    ListElement,
    NormalizedElement,
    OpenXMLDocxExtractor,
    ParagraphElement,
    TableElement,
    UnsupportedDocumentTypeError,
    default_extractor_registry,
)


@pytest.fixture
def sample_context() -> IngestionContext:
    """Fixture providing standardized multi-tenant IngestionContext."""
    return IngestionContext(
        tenant_id="tenant-corp-test",
        document_id="doc-docx-test-12345",
        user_id="user-qa-001",
        department_id="dept-engineering",
        classification=DocumentClassification.INTERNAL,
    )


def create_simple_docx() -> bytes:
    """Generate a clean DOCX document with nested headings (H1, H2, H3) and paragraphs."""
    doc = docx.Document()
    doc.core_properties.title = "Enterprise Governance Handbook"
    doc.core_properties.author = "Corporate Policy Committee"

    # Heading 1
    doc.add_heading("1. Enterprise Governance Policy", level=1)
    doc.add_paragraph("This policy establishes corporate compliance rules across all divisions.")
    doc.add_paragraph("All employees must adhere to data protection standards.")

    # Heading 2
    doc.add_heading("1.1 Department Roles and Responsibilities", level=2)
    doc.add_paragraph("Department managers must review compliance quarterly.")

    # Heading 3
    doc.add_heading("1.1.1 Engineering Division Requirements", level=3)
    doc.add_paragraph("All production code changes require two independent approvals.")

    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def create_interleaved_document() -> bytes:
    """Generate a DOCX containing interleaved headings, paragraphs, tables, and lists."""
    doc = docx.Document()

    # Section 1: Financial Overview
    doc.add_heading("Financial Operations Review", level=1)
    doc.add_paragraph("The corporate financial overview for the recent fiscal period demonstrates positive growth.")

    # Table: Department Budgets
    table = doc.add_table(rows=3, cols=3)
    headers = ["Department", "Allocated Budget", "Expenditure"]
    for col_idx, h in enumerate(headers):
        table.cell(0, col_idx).text = h

    data = [
        ["Engineering", "$1,200,000", "$1,050,000"],
        ["Marketing", "$600,000", "$580,000"],
    ]
    for row_idx, row_data in enumerate(data, start=1):
        for col_idx, val in enumerate(row_data):
            table.cell(row_idx, col_idx).text = val

    # Narrative paragraph after table
    doc.add_paragraph("Expenditures remained within the authorized tolerance margins.")

    # Bulleted List
    doc.add_paragraph("Key action items for upcoming cycle:", style="Normal")
    p_b1 = doc.add_paragraph("Complete cloud infrastructure migration", style="List Bullet")
    p_b2 = doc.add_paragraph("Finalize third-party audit agreements", style="List Bullet")
    p_b3 = doc.add_paragraph("Execute disaster recovery simulation", style="List Bullet")

    # Section 2: Operational Next Steps
    doc.add_heading("Implementation Schedule", level=2)
    doc.add_paragraph("The sequential milestones are defined below:")

    # Numbered List
    p_n1 = doc.add_paragraph("Phase 1: Architecture review", style="List Number")
    p_n2 = doc.add_paragraph("Phase 2: Security scanning and vulnerability remediation", style="List Number")
    p_n3 = doc.add_paragraph("Phase 3: Production deployment", style="List Number")

    # Final paragraph
    doc.add_paragraph("Project completion is scheduled for the end of Q4.")

    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def create_table_only_docx() -> bytes:
    """Generate a DOCX containing only tables."""
    doc = docx.Document()
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Metric"
    t.cell(0, 1).text = "Target"
    t.cell(1, 0).text = "Uptime"
    t.cell(1, 1).text = "99.99%"

    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


def create_empty_docx() -> bytes:
    """Generate a valid DOCX file with no text, headings, or tables."""
    doc = docx.Document()
    stream = io.BytesIO()
    doc.save(stream)
    return stream.getvalue()


class TestDocxExtractorRegistry:
    """Verify registry integration and metadata identification."""

    def test_extractor_registered(self) -> None:
        """Verify DocxExtractor is registered with default registry."""
        extractor = default_extractor_registry.get_extractor(document_type=DocumentType.DOCX)
        assert isinstance(extractor, DocxExtractor)
        assert extractor.name == "DocxExtractor"
        assert extractor.version == "1.0.0"

    def test_extractor_by_mime_type(self) -> None:
        """Verify DocxExtractor resolves via standard DOCX MIME type."""
        extractor = default_extractor_registry.get_extractor(
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert isinstance(extractor, DocxExtractor)

    def test_alias_equivalence(self) -> None:
        """Verify OpenXMLDocxExtractor alias matches DocxExtractor."""
        assert OpenXMLDocxExtractor is DocxExtractor


class TestHeadingHierarchyAndSectionTracking:
    """Verify Heading 1, 2, 3 detection and section hierarchy association."""

    @pytest.mark.anyio
    async def test_nested_headings_levels_preserved(self, sample_context: IngestionContext) -> None:
        """Verify Heading 1, 2, 3 retain their levels in hierarchy."""
        docx_bytes = create_simple_docx()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        headings = result.get_headings()
        assert len(headings) == 3

        h1, h2, h3 = headings
        assert h1.level == 1
        assert h1.text == "1. Enterprise Governance Policy"
        assert h1.content == "# 1. Enterprise Governance Policy"

        assert h2.level == 2
        assert h2.text == "1.1 Department Roles and Responsibilities"
        assert h2.content == "## 1.1 Department Roles and Responsibilities"

        assert h3.level == 3
        assert h3.text == "1.1.1 Engineering Division Requirements"
        assert h3.content == "### 1.1.1 Engineering Division Requirements"

    @pytest.mark.anyio
    async def test_paragraph_associated_with_nearest_heading(self, sample_context: IngestionContext) -> None:
        """Verify paragraphs associate with the nearest parent section heading and section hierarchy."""
        docx_bytes = create_simple_docx()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        paragraphs = result.get_paragraphs()
        assert len(paragraphs) == 4

        # Paragraphs under Heading 1
        p1, p2, p3, p4 = paragraphs
        assert p1.section_heading == "1. Enterprise Governance Policy"
        assert p2.section_heading == "1. Enterprise Governance Policy"
        assert p1.metadata["section_path"] == "1. Enterprise Governance Policy"

        # Paragraph under Heading 2
        assert p3.section_heading == "1.1 Department Roles and Responsibilities"
        assert "1. Enterprise Governance Policy" in p3.metadata["section_hierarchy"]
        assert "1.1 Department Roles and Responsibilities" in p3.metadata["section_hierarchy"]
        assert p3.metadata["section_path"] == "1. Enterprise Governance Policy > 1.1 Department Roles and Responsibilities"

        # Paragraph under Heading 3
        assert p4.section_heading == "1.1.1 Engineering Division Requirements"
        assert p4.metadata["section_hierarchy"] == [
            "1. Enterprise Governance Policy",
            "1.1 Department Roles and Responsibilities",
            "1.1.1 Engineering Division Requirements",
        ]


class TestDocumentOrderAndInterleavedElements:
    """Verify paragraphs, tables, lists, and headings retain their exact original document order."""

    @pytest.mark.anyio
    async def test_interleaved_document_order(self, sample_context: IngestionContext) -> None:
        """Verify elements appear in exact reading order: Heading -> Para -> Table -> Para -> List -> Heading -> List -> Para."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        # Expected element sequence:
        # 0: Heading 1 ("Financial Operations Review")
        # 1: Paragraph ("The corporate financial overview...")
        # 2: Table (Department Budgets)
        # 3: Paragraph ("Expenditures remained within...")
        # 4: Paragraph ("Key action items for upcoming cycle:")
        # 5: List (3 bullet items)
        # 6: Heading 2 ("Implementation Schedule")
        # 7: Paragraph ("The sequential milestones are defined below:")
        # 8: List (3 numbered items)
        # 9: Paragraph ("Project completion is scheduled for the end of Q4.")

        elements = result.elements
        assert len(elements) == 10

        assert elements[0].element_type == ElementType.HEADING
        assert elements[0].section_heading == "Financial Operations Review"

        assert elements[1].element_type == ElementType.PARAGRAPH
        assert "corporate financial overview" in elements[1].content

        assert elements[2].element_type == ElementType.TABLE
        assert isinstance(elements[2], TableElement)
        assert elements[2].section_heading == "Financial Operations Review"

        assert elements[3].element_type == ElementType.PARAGRAPH
        assert "tolerance margins" in elements[3].content

        assert elements[4].element_type == ElementType.PARAGRAPH
        assert "action items" in elements[4].content

        assert elements[5].element_type == ElementType.LIST
        assert isinstance(elements[5], ListElement)
        assert not elements[5].ordered
        assert len(elements[5].items) == 3
        assert "cloud infrastructure migration" in elements[5].items[0]

        assert elements[6].element_type == ElementType.HEADING
        assert elements[6].content == "## Implementation Schedule"

        assert elements[7].element_type == ElementType.PARAGRAPH
        assert "milestones" in elements[7].content

        assert elements[8].element_type == ElementType.LIST
        assert isinstance(elements[8], ListElement)
        assert elements[8].ordered
        assert len(elements[8].items) == 3
        assert "Architecture review" in elements[8].items[0]

        assert elements[9].element_type == ElementType.PARAGRAPH
        assert "end of Q4" in elements[9].content

    @pytest.mark.anyio
    async def test_reading_order_sequence_integers(self, sample_context: IngestionContext) -> None:
        """Verify sequential, 0-indexed reading_order identifiers across all elements."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        for idx, elem in enumerate(result.elements):
            assert elem.reading_order == idx


class TestTableStructurePreservation:
    """Verify tables preserve rows, columns, and Markdown representation."""

    @pytest.mark.anyio
    async def test_table_headers_and_rows_extracted(self, sample_context: IngestionContext) -> None:
        """Verify table cells and row/column matrices are preserved accurately."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        tables = result.get_tables()
        assert len(tables) == 1
        tbl = tables[0]

        assert tbl.headers == ["Department", "Allocated Budget", "Expenditure"]
        assert len(tbl.rows) == 2
        assert tbl.rows[0] == ["Engineering", "$1,200,000", "$1,050,000"]
        assert tbl.rows[1] == ["Marketing", "$600,000", "$580,000"]

        # Markdown table rendering
        md = tbl.to_markdown()
        assert "| Department | Allocated Budget | Expenditure |" in md
        assert "| Engineering | $1,200,000 | $1,050,000 |" in md
        assert "| Marketing | $600,000 | $580,000 |" in md

    @pytest.mark.anyio
    async def test_table_only_document(self, sample_context: IngestionContext) -> None:
        """Verify extraction succeeds for documents consisting solely of tables."""
        docx_bytes = create_table_only_docx()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        assert len(result.elements) == 1
        assert result.elements[0].element_type == ElementType.TABLE
        table = result.elements[0]
        assert isinstance(table, TableElement)
        assert table.headers == ["Metric", "Target"]
        assert table.rows == [["Uptime", "99.99%"]]


class TestListItemsExtraction:
    """Verify bulleted and numbered list detection and Markdown formatting."""

    @pytest.mark.anyio
    async def test_bullet_list_markdown_rendering(self, sample_context: IngestionContext) -> None:
        """Verify bulleted list renders with hyphen bullets."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        lists = result.get_lists()
        assert len(lists) == 2

        bullet_list = lists[0]
        assert not bullet_list.ordered
        assert "- Complete cloud infrastructure migration" in bullet_list.content
        assert "- Finalize third-party audit agreements" in bullet_list.content
        assert "- Execute disaster recovery simulation" in bullet_list.content

    @pytest.mark.anyio
    async def test_numbered_list_markdown_rendering(self, sample_context: IngestionContext) -> None:
        """Verify numbered list renders with ordered indices."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        lists = result.get_lists()
        numbered_list = lists[1]
        assert numbered_list.ordered
        assert "1. Phase 1: Architecture review" in numbered_list.content
        assert "2. Phase 2: Security scanning" in numbered_list.content
        assert "3. Phase 3: Production deployment" in numbered_list.content


class TestCanonicalOffsetsAndTraceability:
    """Verify exact character offsets and source traceability."""

    @pytest.mark.anyio
    async def test_canonical_text_slicing_matches_element_content(self, sample_context: IngestionContext) -> None:
        """Verify raw_text[char_start:char_end] == element.content for all elements."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        assert result.raw_text is not None
        assert len(result.raw_text) > 0

        for elem in result.elements:
            assert elem.char_start is not None
            assert elem.char_end is not None
            assert elem.char_start < elem.char_end

            sliced = result.raw_text[elem.char_start:elem.char_end]
            expected = elem.content
            assert sliced == expected, f"Offset mismatch for {elem.element_type}: '{sliced}' != '{expected}'"

    @pytest.mark.anyio
    async def test_all_elements_have_provenance_traceability(self, sample_context: IngestionContext) -> None:
        """Verify all extracted elements have valid source traceability."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        assert result.validate_traceability(strict=True) is True
        for elem in result.elements:
            assert elem.has_source_traceability() is True
            assert elem.locator.section_heading is not None or elem.locator.char_start is not None


class TestDocumentMetadataAndProperties:
    """Verify core document properties and execution stats."""

    @pytest.mark.anyio
    async def test_core_properties_captured_in_metadata(self, sample_context: IngestionContext) -> None:
        """Verify title, author, and element counts are enriched in DocumentMetadata."""
        docx_bytes = create_simple_docx()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        assert result.metadata.title == "Enterprise Governance Handbook"
        assert result.metadata.author == "Corporate Policy Committee"
        assert result.metadata.document_type == DocumentType.DOCX
        assert result.metadata.mime_type == DocumentMimeType.DOCX.value
        assert result.metadata.custom_metadata["author"] == "Corporate Policy Committee"
        assert result.metadata.custom_metadata["total_elements"] == len(result.elements)

    @pytest.mark.anyio
    async def test_extraction_stats_telemetry(self, sample_context: IngestionContext) -> None:
        """Verify extraction telemetry captures runtime, characters, and counts."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        assert result.stats is not None
        assert result.stats.extractor_name == "DocxExtractor"
        assert result.stats.total_elements == len(result.elements)
        assert result.stats.total_characters > 0
        assert result.stats.duration_seconds >= 0.0
        assert result.stats.ocr_applied is False


class TestErrorHandlingAndEdgeCases:
    """Verify detection of empty, malformed, encrypted, and unsupported files."""

    @pytest.mark.anyio
    async def test_empty_file_rejected(self, sample_context: IngestionContext) -> None:
        """0-byte file input must raise EmptyFileError."""
        extractor = DocxExtractor()
        with pytest.raises(EmptyFileError, match="empty \\(0 bytes\\)"):
            await extractor.extract(b"", sample_context)

    @pytest.mark.anyio
    async def test_corrupted_archive_rejected(self, sample_context: IngestionContext) -> None:
        """Corrupted/truncated binary stream must raise CorruptDocumentError."""
        extractor = DocxExtractor()
        garbage = b"PK\x03\x04corrupted garbage payload that is not a valid zip archive"
        with pytest.raises(CorruptDocumentError, match="Corrupted"):
            await extractor.extract(garbage, sample_context)

    @pytest.mark.anyio
    async def test_missing_document_xml_rejected(self, sample_context: IngestionContext) -> None:
        """ZIP archive missing word/document.xml must raise CorruptDocumentError."""
        import zipfile
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zf:
            zf.writestr("random.txt", "Some text")
        invalid_zip = stream.getvalue()

        extractor = DocxExtractor()
        with pytest.raises(CorruptDocumentError, match="missing essential DOCX parts"):
            await extractor.extract(invalid_zip, sample_context)

    @pytest.mark.anyio
    async def test_encrypted_docx_rejected(self, sample_context: IngestionContext) -> None:
        """Password-protected / encrypted Word document must raise EncryptedDocumentError."""
        # Simulated encrypted compound document containing EncryptedPackage stream
        encrypted_header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100 + b"EncryptedPackage" + b"\x00" * 100
        extractor = DocxExtractor()

        with pytest.raises(EncryptedDocumentError, match="password-protected or encrypted"):
            await extractor.extract(encrypted_header, sample_context)

    @pytest.mark.anyio
    async def test_legacy_binary_doc_rejected(self, sample_context: IngestionContext) -> None:
        """Legacy binary .doc file without EncryptedPackage raises UnsupportedDocumentTypeError."""
        legacy_doc = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200
        extractor = DocxExtractor()

        with pytest.raises(UnsupportedDocumentTypeError, match="(?i)legacy.*Word format"):
            await extractor.extract(legacy_doc, sample_context)

    @pytest.mark.anyio
    async def test_empty_docx_document_warning(self, sample_context: IngestionContext) -> None:
        """Valid DOCX with no text produces a warning and empty elements list."""
        empty_docx = create_empty_docx()
        extractor = DocxExtractor()

        result = await extractor.extract(empty_docx, sample_context)

        assert len(result.elements) == 0
        assert len(result.warnings) > 0
        assert "contains no extractable text" in result.warnings[0]
        assert result.metadata.custom_metadata["is_empty"] is True

    @pytest.mark.anyio
    async def test_unsupported_content_type_rejected(self, sample_context: IngestionContext) -> None:
        """Passing an unsupported object type raises UnsupportedDocumentTypeError."""
        extractor = DocxExtractor()
        with pytest.raises(UnsupportedDocumentTypeError, match="Unsupported content type"):
            await extractor.extract(98765, sample_context)  # type: ignore

    @pytest.mark.anyio
    async def test_nonexistent_path_rejected(self, sample_context: IngestionContext) -> None:
        """Nonexistent filesystem Path raises CorruptDocumentError."""
        extractor = DocxExtractor()
        missing = Path("/tmp/nonexistent_document_file_xyz123.docx")
        with pytest.raises(CorruptDocumentError, match="does not exist"):
            await extractor.extract(missing, sample_context)


class TestInputPolymorphismAndSerialization:
    """Verify input stream handling, Path objects, and serialization round-trip."""

    @pytest.mark.anyio
    async def test_extract_from_bytes_io_stream(self, sample_context: IngestionContext) -> None:
        """Verify extraction directly from an io.BytesIO stream."""
        docx_bytes = create_simple_docx()
        stream = io.BytesIO(docx_bytes)
        extractor = DocxExtractor()

        result = await extractor.extract(stream, sample_context)
        assert len(result.elements) > 0
        assert result.metadata.document_type == DocumentType.DOCX

    @pytest.mark.anyio
    async def test_extract_from_temp_file_path(self, tmp_path: Path, sample_context: IngestionContext) -> None:
        """Verify extraction directly from a filesystem Path."""
        docx_bytes = create_simple_docx()
        doc_file = tmp_path / "corporate_handbook.docx"
        doc_file.write_bytes(docx_bytes)

        extractor = DocxExtractor()
        result = await extractor.extract(doc_file, sample_context)

        assert len(result.elements) > 0
        assert result.metadata.filename == "corporate_handbook.docx"

    @pytest.mark.anyio
    async def test_serialization_roundtrip(self, sample_context: IngestionContext) -> None:
        """Verify full round-trip serialization and deserialization via JSON."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        # To dict and from dict
        d = result.to_dict()
        assert d["document_type"] == "DOCX"
        assert len(d["elements"]) == len(result.elements)

        # To JSON and from JSON
        json_str = result.to_json()
        deserialized = ExtractionResult.from_json(json_str)

        assert deserialized.document_id == result.document_id
        assert deserialized.document_type == result.document_type
        assert len(deserialized.elements) == len(result.elements)
        assert deserialized.raw_text == result.raw_text

    @pytest.mark.anyio
    async def test_to_normalized_elements(self, sample_context: IngestionContext) -> None:
        """Verify to_normalized_elements() generates valid NormalizedElement objects."""
        docx_bytes = create_interleaved_document()
        extractor = DocxExtractor()

        result = await extractor.extract(docx_bytes, sample_context)

        norm_elements = result.to_normalized_elements()
        assert len(norm_elements) == len(result.elements)

        for norm in norm_elements:
            assert isinstance(norm, NormalizedElement)
            assert norm.has_source_traceability()
            assert norm.validate_traceability(strict=True)
