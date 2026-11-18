"""Unit tests for Document Ingestion Architecture and Shared Data Contracts."""

import uuid
import pytest
from pydantic import TypeAdapter, ValidationError

from backend.ingestion import (
    AccessControlPayload,
    BoundingBox,
    ChunkLocator,
    ChunkingConfig,
    ChunkingStrategy,
    ChunkingStrategyType,
    DeduplicationResult,
    DeduplicationStrategy,
    DocumentChunkInput,
    DocumentClassification,
    DocumentExtractor,
    DocumentIngestionStatus,
    DocumentMetadata,
    DocumentMimeType,
    DocumentType,
    ElementType,
    EmbeddingProvider,
    EmbeddingResult,
    ExtractedElement,
    ExtractionResult,
    ExtractionStats,
    ExtractorNotFoundError,
    ExtractorRegistry,
    HeadingElement,
    IngestionContext,
    IngestionOptions,
    IngestionOrchestrator,
    IngestionPipelineResult,
    PageElement,
    ParagraphElement,
    PipelineStage,
    SpreadsheetRowElement,
    TableElement,
    TextElement,
    UnsupportedDocumentTypeError,
    VectorIndexProvider,
    VectorIndexResult,
    register_extractor,
)


class TestDocumentEnums:
    """Test document types, MIME types, and status enums."""

    def test_document_type_from_mime(self) -> None:
        assert DocumentType.from_mime_type("application/pdf") == DocumentType.PDF
        assert (
            DocumentType.from_mime_type(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            == DocumentType.DOCX
        )
        assert (
            DocumentType.from_mime_type(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            == DocumentType.XLSX
        )
        assert DocumentType.from_mime_type("text/csv") == DocumentType.CSV
        assert DocumentType.from_mime_type("text/plain") == DocumentType.TXT
        assert DocumentType.from_mime_type("text/markdown") == DocumentType.MARKDOWN

    def test_document_type_from_mime_unsupported(self) -> None:
        with pytest.raises(ValueError, match="Unsupported MIME type"):
            DocumentType.from_mime_type("audio/mp3")

    def test_document_type_from_filename(self) -> None:
        assert DocumentType.from_filename("policy.pdf") == DocumentType.PDF
        assert DocumentType.from_filename("contract.DOCX") == DocumentType.DOCX
        assert DocumentType.from_filename("budget_2026.xlsx") == DocumentType.XLSX
        assert DocumentType.from_filename("metrics.csv") == DocumentType.CSV
        assert DocumentType.from_filename("readme.md") == DocumentType.MARKDOWN

    def test_document_type_from_filename_unsupported(self) -> None:
        with pytest.raises(ValueError, match="Unsupported file extension"):
            DocumentType.from_filename("archive.zip")

    def test_prisma_status_alignment(self) -> None:
        # Enums match PostgreSQL Prisma definitions
        assert DocumentIngestionStatus.PENDING == "PENDING"
        assert DocumentIngestionStatus.PROCESSING == "PROCESSING"
        assert DocumentIngestionStatus.COMPLETED == "COMPLETED"
        assert DocumentIngestionStatus.FAILED == "FAILED"
        assert DocumentIngestionStatus.CANCELLED == "CANCELLED"

        assert DocumentClassification.PUBLIC == "PUBLIC"
        assert DocumentClassification.INTERNAL == "INTERNAL"
        assert DocumentClassification.CONFIDENTIAL == "CONFIDENTIAL"
        assert DocumentClassification.RESTRICTED == "RESTRICTED"


class TestCoordinatesAndLocators:
    """Test ChunkLocator and BoundingBox spatial contracts."""

    def test_valid_bounding_box(self) -> None:
        box = BoundingBox(x0=10.0, y0=20.0, x1=100.0, y1=200.0, page_number=1)
        assert box.x0 == 10.0
        assert box.y1 == 200.0
        assert box.page_number == 1

    def test_invalid_bounding_box(self) -> None:
        with pytest.raises(ValidationError, match="x0 .* cannot exceed x1"):
            BoundingBox(x0=150.0, y0=20.0, x1=100.0, y1=200.0)

        with pytest.raises(ValidationError, match="y0 .* cannot exceed y1"):
            BoundingBox(x0=10.0, y0=250.0, x1=100.0, y1=200.0)

    def test_chunk_locator_valid_half_open_offsets(self) -> None:
        locator = ChunkLocator(
            page_number=3,
            section_heading="Section 2.1 Leave Policy",
            char_start=100,
            char_end=250,
        )
        assert locator.char_start == 100
        assert locator.char_end == 250
        payload = locator.to_provenance_payload()
        assert payload["page_number"] == 3
        assert payload["section_heading"] == "Section 2.1 Leave Policy"
        assert payload["char_start"] == 100
        assert payload["char_end"] == 250

    def test_chunk_locator_invalid_offsets(self) -> None:
        with pytest.raises(ValidationError, match="char_start .* cannot exceed char_end"):
            ChunkLocator(char_start=300, char_end=200)

    def test_chunk_locator_spreadsheet_provenance(self) -> None:
        locator = ChunkLocator(
            sheet_name="Salaries_2026",
            row_range="Row 15:30",
        )
        payload = locator.to_provenance_payload()
        assert payload["sheet_name"] == "Salaries_2026"
        assert payload["row_range"] == "Row 15:30"
        assert payload["page_number"] is None


class TestExtractedElementsDiscriminatedUnion:
    """Test heterogeneous extracted elements, markdown serialization, and discriminated union."""

    def test_heading_element(self) -> None:
        heading = HeadingElement(text="Executive Overview", level=2)
        assert heading.element_type == ElementType.HEADING
        assert heading.text_content == "## Executive Overview"
        assert heading.level == 2

    def test_paragraph_element(self) -> None:
        para = ParagraphElement(text="This is an enterprise policy document body paragraph.")
        assert para.element_type == ElementType.PARAGRAPH
        assert para.text_content == "This is an enterprise policy document body paragraph."

    def test_table_element_markdown(self) -> None:
        table = TableElement(
            headers=["Role", "Level", "Allowance"],
            rows=[
                ["Engineer", "Senior", "$1,000"],
                ["Manager", "Lead", "$2,000"],
            ],
            caption="Annual Benefits Matrix",
        )
        md = table.to_markdown()
        assert "**Table: Annual Benefits Matrix**" in md
        assert "| Role | Level | Allowance |" in md
        assert "| Engineer | Senior | $1,000 |" in md

    def test_spreadsheet_row_element_markdown(self) -> None:
        row = SpreadsheetRowElement(
            sheet_name="Department_Budgets",
            row_index=12,
            cells={"Department": "Engineering", "Budget": 500000, "Currency": "USD"},
        )
        md = row.to_markdown()
        assert "| Sheet | Row | Department | Budget | Currency |" in md
        assert "| Department_Budgets | 12 | Engineering | 500000 | USD |" in md

    def test_discriminated_union_polymorphic_parsing(self) -> None:
        raw_elements_data = [
            {"element_type": "heading", "text": "Chapter 1", "level": 1},
            {"element_type": "paragraph", "text": "Chapter content begins here."},
            {"element_type": "table", "headers": ["A", "B"], "rows": [["1", "2"]]},
            {
                "element_type": "spreadsheet_row",
                "sheet_name": "Sheet1",
                "row_index": 2,
                "cells": {"ColA": "Val1"},
            },
        ]

        adapter = TypeAdapter(list[ExtractedElement])
        elements = adapter.validate_python(raw_elements_data)

        assert len(elements) == 4
        assert isinstance(elements[0], HeadingElement)
        assert isinstance(elements[1], ParagraphElement)
        assert isinstance(elements[2], TableElement)
        assert isinstance(elements[3], SpreadsheetRowElement)

    def test_page_element_with_nested_elements(self) -> None:
        page = PageElement(
            page_number=1,
            width=595.0,
            height=842.0,
            is_scanned=False,
            sub_elements=[
                HeadingElement(text="Page 1 Header", level=1),
                ParagraphElement(text="Content of page 1."),
            ],
        )
        assert "[Page 1]" not in page.text_content
        assert "# Page 1 Header" in page.text_content
        assert "Content of page 1." in page.text_content


class TestExtractionResult:
    """Test ExtractionResult container and convenience queries."""

    def test_extraction_result_helpers(self) -> None:
        metadata = DocumentMetadata(
            document_id=str(uuid.uuid4()),
            tenant_id=str(uuid.uuid4()),
            filename="policy.pdf",
            file_hash_sha256="a" * 64,
            file_size_bytes=1024,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
        )

        elements: list[ExtractedElement] = [
            HeadingElement(text="Title", level=1),
            ParagraphElement(text="Intro paragraph."),
            TableElement(headers=["H1"], rows=[["V1"]]),
            SpreadsheetRowElement(sheet_name="S1", row_index=1, cells={"C1": "V1"}),
        ]

        stats = ExtractionStats(
            extractor_name="TestExtractor",
            duration_seconds=0.25,
            total_elements=4,
            total_pages=1,
        )

        result = ExtractionResult(
            document_id=metadata.document_id,
            document_type=DocumentType.PDF,
            mime_type="application/pdf",
            elements=elements,
            metadata=metadata,
            stats=stats,
        )

        assert len(result.get_headings()) == 1
        assert len(result.get_paragraphs()) == 1
        assert len(result.get_tables()) == 1
        assert len(result.get_spreadsheet_rows()) == 1
        full_text = result.get_full_text()
        assert "# Title" in full_text
        assert "Intro paragraph." in full_text


class TestDocumentChunkAndQdrantPayload:
    """Test DocumentChunkInput adheres to Qdrant vector payload and PostgreSQL schemas."""

    def test_chunk_to_qdrant_payload(self) -> None:
        doc_id = str(uuid.uuid4())
        tenant_id = str(uuid.uuid4())

        acl = AccessControlPayload(
            tenant_id=tenant_id,
            roles=["HR", "EXECUTIVE"],
            departments=["Human Resources"],
            users=["user_123"],
        )

        locator = ChunkLocator(
            page_number=12,
            section_heading="3.2 Annual Leave Allowances",
            char_start=14200,
            char_end=15820,
        )

        chunk = DocumentChunkInput(
            tenant_id=tenant_id,
            document_id=doc_id,
            chunk_index=8,
            content="According to corporate HR policy, employees have 12 leave days...",
            content_hash="b" * 64,
            token_count=180,
            locator=locator,
            acl=acl,
        )

        payload = chunk.to_qdrant_payload()
        assert payload["document_id"] == doc_id
        assert payload["chunk_index"] == 8
        assert payload["content"].startswith("According to corporate HR policy")
        assert payload["source_location"]["page_number"] == 12
        assert payload["source_location"]["section_heading"] == "3.2 Annual Leave Allowances"
        assert payload["acl"]["tenant_id"] == tenant_id
        assert "HR" in payload["acl"]["roles"]
        assert "Human Resources" in payload["acl"]["departments"]

    def test_chunk_to_db_record(self) -> None:
        doc_id = str(uuid.uuid4())
        tenant_id = str(uuid.uuid4())

        chunk = DocumentChunkInput(
            tenant_id=tenant_id,
            document_id=doc_id,
            chunk_index=0,
            content="Sample text",
            content_hash="c" * 64,
            token_count=10,
            locator=ChunkLocator(page_number=1, char_start=0, char_end=11),
            acl=AccessControlPayload(tenant_id=tenant_id),
        )

        db_rec = chunk.to_db_record()
        assert db_rec["tenant_id"] == tenant_id
        assert db_rec["document_id"] == doc_id
        assert db_rec["chunk_index"] == 0
        assert db_rec["page_number"] == 1
        assert db_rec["char_start"] == 0
        assert db_rec["char_end"] == 11


class TestIngestionContext:
    """Test IngestionContext zero-trust tenant isolation and ACL derivation."""

    def test_context_acl_derivation(self) -> None:
        context = IngestionContext(
            tenant_id="tenant_org_99",
            document_id="doc_555",
            roles=["FINANCE_ANALYST"],
            departments=["Finance Directorate"],
            allowed_users=["user_fin_01"],
            classification=DocumentClassification.CONFIDENTIAL,
        )

        acl = context.to_acl_payload()
        assert acl.tenant_id == "tenant_org_99"
        assert acl.roles == ["FINANCE_ANALYST"]
        assert acl.departments == ["Finance Directorate"]
        assert acl.users == ["user_fin_01"]


class TestExtractorRegistryAndExtensibility:
    """Test extractor registry, open-closed extensibility, and error handling."""

    def test_custom_extractor_registration_and_retrieval(self) -> None:
        registry = ExtractorRegistry()

        class DummyMarkdownExtractor(DocumentExtractor):
            @property
            def name(self) -> str:
                return "DummyMarkdownExtractor"

            @property
            def supported_document_types(self) -> list[DocumentType]:
                return [DocumentType.MARKDOWN, DocumentType.TXT]

            @property
            def supported_mime_types(self) -> list[str]:
                return ["text/markdown", "text/plain"]

            async def extract(self, content, context, metadata=None):
                return ExtractionResult(
                    document_id=context.document_id,
                    document_type=DocumentType.MARKDOWN,
                    mime_type="text/markdown",
                    metadata=metadata
                    or DocumentMetadata(
                        document_id=context.document_id,
                        tenant_id=context.tenant_id,
                        filename="test.md",
                        file_hash_sha256="d" * 64,
                        file_size_bytes=10,
                        mime_type="text/markdown",
                        document_type=DocumentType.MARKDOWN,
                    ),
                )

        registry.register(DummyMarkdownExtractor())

        extractor = registry.get_extractor(document_type=DocumentType.MARKDOWN)
        assert extractor.name == "DummyMarkdownExtractor"

        extractor_by_mime = registry.get_extractor(mime_type="text/plain")
        assert extractor_by_mime.name == "DummyMarkdownExtractor"

        assert DocumentType.MARKDOWN in registry.list_supported_document_types()
        assert "text/markdown" in registry.list_supported_mime_types()

    def test_extractor_not_found(self) -> None:
        registry = ExtractorRegistry()
        with pytest.raises(ExtractorNotFoundError, match="No registered extractor found"):
            registry.get_extractor(document_type=DocumentType.PDF)

    def test_decorator_registration(self) -> None:
        registry = ExtractorRegistry()

        @register_extractor(registry=registry)
        class MockPDFExtractor(DocumentExtractor):
            @property
            def name(self) -> str:
                return "MockPDFExtractor"

            @property
            def supported_document_types(self) -> list[DocumentType]:
                return [DocumentType.PDF]

            @property
            def supported_mime_types(self) -> list[str]:
                return ["application/pdf"]

            async def extract(self, content, context, metadata=None):
                raise NotImplementedError

        assert len(registry.list_extractors()) == 1
        assert registry.get_extractor(document_type=DocumentType.PDF).name == "MockPDFExtractor"


class TestSubsystemInterfacesUninstantiability:
    """Verify abstract contracts enforce implementation of abstract methods."""

    def test_abstract_contracts_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            DocumentExtractor()  # type: ignore

        with pytest.raises(TypeError):
            ChunkingStrategy()  # type: ignore

        with pytest.raises(TypeError):
            DeduplicationStrategy()  # type: ignore

        with pytest.raises(TypeError):
            EmbeddingProvider()  # type: ignore

        with pytest.raises(TypeError):
            VectorIndexProvider()  # type: ignore

        with pytest.raises(TypeError):
            IngestionOrchestrator()  # type: ignore
