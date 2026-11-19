"""Comprehensive unit tests for Chunk Provenance and Locator Validation (Stage 3 — Task 10).

Covers:
1. Format-specific locator field requirements (PDF, DOCX, XLSX, CSV, TXT, MARKDOWN):
   - REQUIRED vs OPTIONAL vs NOT_APPLICABLE coordinates.
   - Rejection of non-applicable fields (e.g. sheet_name on PDF/DOCX, row_range on Word).
2. Boundary and coordinate constraints:
   - 1-based page numbers referencing existing document pages.
   - Page out of bounds detection (page > total_pages).
   - Spreadsheet worksheet name existence in workbook.
   - 1-based row numbers and row range validation (start <= end).
   - Spreadsheet row range out of bounds detection (start/end > worksheet row count).
   - Character offset boundary intervals ([char_start, char_end) where char_start <= char_end <= text_length).
3. Missing provenance invariant:
   - Rejecting chunks devoid of any locator coordinates.
4. Error reporting and diagnostics:
   - Useful ProvenanceValidationError attributes (document_id, chunk_id, chunk_index, field, invalid_value, error_code).
   - Batch report generation in non-strict mode (ProvenanceReport, ChunkValidationOutcome).
5. Source location reconstruction:
   - Formatted verifiable citation badges ([Page X, Section: Y], [Worksheet: S, Rows R]).
   - Canonical text slicing from canonical text stream using char offsets.
6. Persistence and serialization preservation:
   - PostgreSQL DB record round-trip reconstruction (ChunkLocator.from_db_record).
   - Qdrant vector payload round-trip reconstruction (ChunkLocator.from_qdrant_payload).
   - Invariant preservation verification (verify_provenance_preservation).
7. Integration with ChunkingEngine:
   - Validation enforcement during chunk_document() pipeline progression.
"""

import pytest

from backend.ingestion import (
    AccessControlPayload,
    ChunkLocator,
    ChunkProvenanceValidator,
    ChunkValidationOutcome,
    ChunkingConfig,
    ChunkingEngine,
    ChunkingStrategyType,
    DocumentBoundaryContext,
    DocumentChunkInput,
    DocumentClassification,
    DocumentMetadata,
    DocumentMimeType,
    DocumentType,
    ExtractionResult,
    FieldRequirement,
    HeadingElement,
    IngestionContext,
    LocatorValidationErrorDetail,
    ParagraphElement,
    ProvenanceErrorCode,
    ProvenanceReport,
    ProvenanceValidationError,
    SourceLocationReconstruction,
    SpreadsheetRowElement,
    TableElement,
    default_provenance_validator,
    reconstruct_chunk_source_location,
    validate_chunk_provenance,
    validate_chunks_provenance,
)


# ==============================================================================
# FIXTURES & TEST HELPERS
# ==============================================================================

@pytest.fixture
def test_acl() -> AccessControlPayload:
    """Fixture providing zero-trust access control payload."""
    return AccessControlPayload(
        tenant_id="tenant-enterprise-vn-001",
        roles=["ENGINEERING_LEAD"],
        departments=["Engineering"],
        users=["user-corp-author-42"],
    )


@pytest.fixture
def sample_chunk(test_acl: AccessControlPayload) -> DocumentChunkInput:
    """Helper to construct a valid base DocumentChunkInput."""
    return DocumentChunkInput(
        chunk_id="chunk-test-uuid-001",
        tenant_id="tenant-enterprise-vn-001",
        document_id="doc-corp-spec-2026",
        chunk_index=0,
        content="Enterprise hybrid search integrates dense semantic embeddings with sparse BM25 indices.",
        content_hash="b" * 64,
        token_count=14,
        locator=ChunkLocator(
            page_number=1,
            section_heading="1. Overview",
            char_start=0,
            char_end=85,
        ),
        acl=test_acl,
    )


def build_boundary_context(
    doc_type: DocumentType = DocumentType.PDF,
    total_pages: int | None = 5,
    sheet_names: list[str] | None = None,
    sheet_row_counts: dict[str, int] | None = None,
    canonical_text: str | None = None,
) -> DocumentBoundaryContext:
    """Helper to assemble a DocumentBoundaryContext."""
    return DocumentBoundaryContext(
        document_id="doc-corp-spec-2026",
        document_type=doc_type,
        total_pages=total_pages,
        sheet_names=sheet_names or (["Sheet1", "Salaries"] if doc_type == DocumentType.XLSX else []),
        sheet_row_counts=sheet_row_counts or ({"Sheet1": 100, "Salaries": 50} if doc_type == DocumentType.XLSX else {}),
        canonical_text=canonical_text or "Enterprise hybrid search integrates dense semantic embeddings with sparse BM25 indices.",
    )


# ==============================================================================
# 1. FORMAT-SPECIFIC LOCATOR REQUIREMENTS (PDF, DOCX, XLSX, CSV)
# ==============================================================================

class TestFormatSpecificLocatorRules:
    """Tests verifying format-specific rules (REQUIRED, OPTIONAL, NOT_APPLICABLE)."""

    def test_pdf_valid_coordinates(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify valid PDF chunk with page_number and offsets passes validation."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=5)

        outcome = validator.validate_chunk(sample_chunk, context=boundary, strict=True)
        assert outcome.is_valid is True
        assert len(outcome.errors) == 0

    def test_pdf_missing_page_number_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify PDF chunk without page_number is strictly rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=5)

        # Remove required page_number
        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(section_heading="Overview", char_start=0, char_end=50)
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.document_id == "doc-corp-spec-2026"
        assert err.field == "page_number"
        assert err.details["error_code"] == ProvenanceErrorCode.MISSING_REQUIRED_FIELD.value

    def test_pdf_non_applicable_sheet_name_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify PDF chunk with spreadsheet sheet_name is rejected as non-applicable."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=5)

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(page_number=1, sheet_name="Sheet1")
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.field == "sheet_name"
        assert err.details["error_code"] == ProvenanceErrorCode.NON_APPLICABLE_FIELD_PRESENT.value

    def test_pdf_non_applicable_row_range_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify PDF chunk with spreadsheet row_range is rejected as non-applicable."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=5)

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(page_number=1, row_range="Row 1:10")
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.field == "row_range"
        assert err.details["error_code"] == ProvenanceErrorCode.NON_APPLICABLE_FIELD_PRESENT.value

    def test_docx_valid_coordinates_without_page_number(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify DOCX chunk with section_heading and char offsets but no page_number is valid."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.DOCX, total_pages=None)

        docx_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(
                page_number=None,
                section_heading="2.1 Architecture Specification",
                char_start=0,
                char_end=85,
            )
        })

        outcome = validator.validate_chunk(docx_chunk, context=boundary, strict=True)
        assert outcome.is_valid is True

    def test_docx_non_applicable_spreadsheet_coordinates_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify DOCX chunk with sheet_name or row_range is rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.DOCX)

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(section_heading="Overview", sheet_name="Worksheet1")
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        assert exc_info.value.field == "sheet_name"
        assert exc_info.value.details["error_code"] == ProvenanceErrorCode.NON_APPLICABLE_FIELD_PRESENT.value

    def test_xlsx_valid_coordinates(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify XLSX chunk with sheet_name and row_range is valid."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(
            doc_type=DocumentType.XLSX,
            sheet_names=["Salaries", "Q4_Budget"],
            sheet_row_counts={"Salaries": 100, "Q4_Budget": 50},
        )

        xlsx_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(
                sheet_name="Salaries",
                row_range="Row 15:30",
                section_heading="Worksheet: Salaries",
            )
        })

        outcome = validator.validate_chunk(xlsx_chunk, context=boundary, strict=True)
        assert outcome.is_valid is True

    def test_xlsx_missing_sheet_name_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify XLSX chunk missing required sheet_name is rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.XLSX)

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(row_range="Row 10:20")
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        assert exc_info.value.field == "sheet_name"
        assert exc_info.value.details["error_code"] == ProvenanceErrorCode.MISSING_REQUIRED_FIELD.value

    def test_csv_valid_coordinates(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify CSV chunk with row_range and optional sheet_name is valid."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(
            doc_type=DocumentType.CSV,
            sheet_names=["employees"],
            sheet_row_counts={"employees": 500},
        )

        csv_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(
                sheet_name="employees",
                row_range="Row 2:50",
            )
        })

        outcome = validator.validate_chunk(csv_chunk, context=boundary, strict=True)
        assert outcome.is_valid is True


# ==============================================================================
# 2. PHYSICAL BOUNDARY CHECKS (PAGES, ROWS, CHAR OFFSETS)
# ==============================================================================

class TestPhysicalBoundaryConstraints:
    """Tests verifying physical document boundaries against source metadata."""

    def test_page_number_zero_or_negative_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify 0 or negative page number violates 1-based indexing constraint."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(total_pages=10)

        # ChunkLocator model validator allows positive ints; testing validator catches < 1
        with pytest.raises(ValueError):
            ChunkLocator(page_number=0)

        with pytest.raises(ValueError):
            ChunkLocator(page_number=-1)

    def test_page_number_exceeds_total_pages_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify page number exceeding document total pages is rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=5)

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(page_number=12, section_heading="Annex")
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.field == "page_number"
        assert err.invalid_value == 12
        assert err.details["error_code"] == ProvenanceErrorCode.PAGE_OUT_OF_BOUNDS.value
        assert "exceeds total document pages (5)" in err.message

    def test_unknown_worksheet_name_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify referencing a non-existent worksheet name in XLSX is rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(
            doc_type=DocumentType.XLSX,
            sheet_names=["Active_Employees", "Terminated"],
        )

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(sheet_name="Payroll_Secrets", row_range="Row 1:10")
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.field == "sheet_name"
        assert err.invalid_value == "Payroll_Secrets"
        assert err.details["error_code"] == ProvenanceErrorCode.UNKNOWN_SHEET_NAME.value

    def test_spreadsheet_row_range_out_of_bounds_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify row range referencing rows beyond worksheet maximum is rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(
            doc_type=DocumentType.XLSX,
            sheet_names=["Salaries"],
            sheet_row_counts={"Salaries": 40},
        )

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(sheet_name="Salaries", row_range="Row 50:60")
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.field == "row_range"
        assert err.details["error_code"] == ProvenanceErrorCode.ROW_OUT_OF_BOUNDS.value
        assert "exceeds worksheet maximum rows (40)" in err.message

    def test_spreadsheet_row_range_start_exceeds_end_rejected(self) -> None:
        """Verify row range where start_row > end_row is rejected by ChunkLocator validator."""
        with pytest.raises(ValueError) as exc_info:
            ChunkLocator(row_range="Row 35:20")
        assert "start row (35) cannot exceed end row (20)" in str(exc_info.value)

    def test_char_start_exceeds_char_end_rejected(self) -> None:
        """Verify char_start > char_end violates half-open interval rule [char_start, char_end)."""
        with pytest.raises(ValueError) as exc_info:
            ChunkLocator(char_start=500, char_end=100)
        assert "cannot exceed char_end" in str(exc_info.value)

    def test_char_offsets_exceed_canonical_text_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify character offsets exceeding source canonical text length are rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(
            canonical_text="Short canonical text string.",  # len = 28
        )

        invalid_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(page_number=1, char_start=10, char_end=150)
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(invalid_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.field == "char_end"
        assert err.details["error_code"] == ProvenanceErrorCode.CHAR_OFFSETS_OUT_OF_BOUNDS.value
        assert "exceeds canonical text length (28)" in err.message


# ==============================================================================
# 3. MISSING PROVENANCE & EMPTY LOCATOR INVARIANTS
# ==============================================================================

class TestMissingProvenanceInvariants:
    """Tests ensuring chunks devoid of coordinates are rejected."""

    def test_completely_empty_locator_rejected(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify a chunk with all locator fields set to None is rejected."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context()

        empty_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator()  # all None
        })

        with pytest.raises(ProvenanceValidationError) as exc_info:
            validator.validate_chunk(empty_chunk, context=boundary, strict=True)

        err = exc_info.value
        assert err.field == "locator"
        assert err.details["error_code"] == ProvenanceErrorCode.MISSING_PROVENANCE.value
        assert "has no source provenance coordinates" in err.message


# ==============================================================================
# 4. ERROR DIAGNOSTICS & BATCH REPORTING (NON-STRICT MODE)
# ==============================================================================

class TestDiagnosticReporting:
    """Tests verifying non-strict reporting and batch validation outcomes."""

    def test_non_strict_chunk_validation_returns_outcome(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify non-strict mode returns ChunkValidationOutcome with error details without raising."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=3)

        # Create chunk with multiple defects: page out of bounds + non-applicable sheet_name
        defective_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(page_number=10, sheet_name="ForbiddenSheet")
        })

        outcome = validator.validate_chunk(defective_chunk, context=boundary, strict=False)
        assert outcome.is_valid is False
        assert outcome.chunk_id == defective_chunk.chunk_id
        assert outcome.document_id == defective_chunk.document_id
        assert len(outcome.errors) == 2

        error_fields = [e.field for e in outcome.errors]
        assert "sheet_name" in error_fields
        assert "page_number" in error_fields

    def test_batch_validate_chunks_report(self, sample_chunk: DocumentChunkInput, test_acl: AccessControlPayload) -> None:
        """Verify validate_chunks produces aggregated ProvenanceReport across multiple chunks."""
        validator = ChunkProvenanceValidator()
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=5)

        chunk1 = sample_chunk.model_copy(update={
            "chunk_id": "chunk-01",
            "chunk_index": 0,
            "locator": ChunkLocator(page_number=1),
        })
        chunk2 = sample_chunk.model_copy(update={
            "chunk_id": "chunk-02",
            "chunk_index": 1,
            "locator": ChunkLocator(page_number=2),
        })
        # Invalid chunk with page out of bounds
        chunk3 = sample_chunk.model_copy(update={
            "chunk_id": "chunk-03",
            "chunk_index": 2,
            "locator": ChunkLocator(page_number=99),
        })

        report = validator.validate_chunks([chunk1, chunk2, chunk3], context=boundary, strict=False)
        assert isinstance(report, ProvenanceReport)
        assert report.total_chunks == 3
        assert report.valid_chunks == 2
        assert report.invalid_chunks == 1
        assert report.is_valid is False
        assert len(report.failures) == 1
        assert report.failures[0].chunk_id == "chunk-03"


# ==============================================================================
# 5. SOURCE LOCATION RECONSTRUCTION & CITATION BADGES
# ==============================================================================

class TestSourceLocationReconstruction:
    """Tests verifying reconstruction of citation badges and text slices."""

    def test_reconstruct_pdf_source_location(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify citation badge and text reconstruction for PDF chunk."""
        canonical_text = "Enterprise hybrid search integrates dense semantic embeddings with sparse BM25 indices."
        boundary = build_boundary_context(
            doc_type=DocumentType.PDF,
            canonical_text=canonical_text,
        )

        pdf_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(
                page_number=3,
                section_heading="3.2 Hybrid Retrieval",
                char_start=0,
                char_end=24,  # "Enterprise hybrid search"
            )
        })

        reconstruction = default_provenance_validator.reconstruct_source_location(pdf_chunk, boundary=boundary)
        assert isinstance(reconstruction, SourceLocationReconstruction)
        assert reconstruction.document_id == pdf_chunk.document_id
        assert reconstruction.chunk_id == pdf_chunk.chunk_id
        assert reconstruction.citation_badge == "[Page 3, Section: 3.2 Hybrid Retrieval]"
        assert reconstruction.reconstructed_text == "Enterprise hybrid search"
        assert reconstruction.provenance_metadata["page_number"] == 3

    def test_reconstruct_spreadsheet_source_location(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify citation badge reconstruction for XLSX spreadsheet chunk."""
        xlsx_chunk = sample_chunk.model_copy(update={
            "locator": ChunkLocator(
                sheet_name="Salaries",
                row_range="Row 10:25",
            )
        })

        reconstruction = default_provenance_validator.reconstruct_source_location(xlsx_chunk)
        assert reconstruction.citation_badge == "[Worksheet: Salaries, Row 10:25]"
        assert reconstruction.provenance_metadata["sheet_name"] == "Salaries"
        assert reconstruction.provenance_metadata["row_range"] == "Row 10:25"

    def test_convenience_helper_functions(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify standalone helper functions mirror class methods."""
        boundary = build_boundary_context(doc_type=DocumentType.PDF, total_pages=5)

        outcome = validate_chunk_provenance(sample_chunk, context=boundary, strict=True)
        assert outcome.is_valid is True

        reconstruction = reconstruct_chunk_source_location(sample_chunk, boundary=boundary)
        assert reconstruction.citation_badge == "[Page 1, Section: 1. Overview]"


# ==============================================================================
# 6. PERSISTENCE & SERIALIZATION PRESERVATION (ROUND-TRIP)
# ==============================================================================

class TestProvenancePreservation:
    """Tests verifying coordinate preservation across DB records and Qdrant payloads."""

    def test_roundtrip_postgres_db_record(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify ChunkLocator survives transformation to/from PostgreSQL document_chunks record."""
        original_locator = ChunkLocator(
            page_number=4,
            section_heading="4. Deployment & Infrastructure",
            sheet_name=None,
            row_range=None,
            char_start=1200,
            char_end=2400,
        )
        chunk = sample_chunk.model_copy(update={"locator": original_locator})

        # To DB dict representation
        db_record = chunk.to_db_record()
        assert db_record["page_number"] == 4
        assert db_record["section_heading"] == "4. Deployment & Infrastructure"
        assert db_record["char_start"] == 1200
        assert db_record["char_end"] == 2400

        # Reconstruct from DB record
        reconstructed = ChunkLocator.from_db_record(db_record)
        assert reconstructed.page_number == original_locator.page_number
        assert reconstructed.section_heading == original_locator.section_heading
        assert reconstructed.sheet_name == original_locator.sheet_name
        assert reconstructed.row_range == original_locator.row_range
        assert reconstructed.char_start == original_locator.char_start
        assert reconstructed.char_end == original_locator.char_end

    def test_roundtrip_qdrant_vector_payload(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify ChunkLocator survives transformation to/from Qdrant source_location payload."""
        original_locator = ChunkLocator(
            sheet_name="Executive_Summary",
            row_range="Row 2:50",
            section_heading="Worksheet: Executive_Summary",
            char_start=0,
            char_end=500,
        )
        chunk = sample_chunk.model_copy(update={"locator": original_locator})

        # To Qdrant vector payload
        payload = chunk.to_qdrant_payload()
        assert "source_location" in payload
        assert payload["source_location"]["sheet_name"] == "Executive_Summary"
        assert payload["source_location"]["row_range"] == "Row 2:50"

        # Reconstruct from Qdrant payload
        reconstructed = ChunkLocator.from_qdrant_payload(payload)
        assert reconstructed.sheet_name == original_locator.sheet_name
        assert reconstructed.row_range == original_locator.row_range
        assert reconstructed.section_heading == original_locator.section_heading
        assert reconstructed.char_start == original_locator.char_start
        assert reconstructed.char_end == original_locator.char_end

    def test_verify_provenance_preservation_helper(self, sample_chunk: DocumentChunkInput) -> None:
        """Verify validator helper asserts bidirectional preservation across both representations."""
        validator = ChunkProvenanceValidator()
        assert validator.verify_provenance_preservation(sample_chunk) is True


# ==============================================================================
# 7. INTEGRATION WITH CHUNKING ENGINE
# ==============================================================================

class TestChunkingEngineProvenanceIntegration:
    """Tests verifying ChunkingEngine automatically enforces provenance validation."""

    @pytest.mark.anyio
    async def test_chunking_engine_validates_chunks_automatically(self) -> None:
        """Verify standard chunk_document call completes successfully with valid provenance."""
        engine = ChunkingEngine()
        context = IngestionContext(
            tenant_id="tenant-enterprise-vn-001",
            document_id="doc-guideline-2026-uuid",
            classification=DocumentClassification.INTERNAL,
        )

        para = ParagraphElement(
            text="Autonomous multi-hop retrieval decomposes queries and accumulates evidence across documents.",
            page_number=1,
            section_heading="Agentic RAG",
        )
        extraction_res = ExtractionResult(
            document_id="doc-guideline-2026-uuid",
            document_type=DocumentType.PDF,
            mime_type=DocumentMimeType.PDF.value,
            elements=[para],
            metadata=DocumentMetadata(
                document_id="doc-guideline-2026-uuid",
                tenant_id="tenant-enterprise-vn-001",
                filename="agentic_rag.pdf",
                file_hash_sha256="c" * 64,
                file_size_bytes=2048,
                mime_type=DocumentMimeType.PDF.value,
                document_type=DocumentType.PDF,
                total_pages=1,
            ),
        )
        extraction_res.compute_canonical_offsets()

        chunks = await engine.chunk_document(extraction_res, context)
        assert len(chunks) == 1
        assert chunks[0].locator.page_number == 1
        assert chunks[0].locator.section_heading == "Agentic RAG"

    @pytest.mark.anyio
    async def test_chunking_engine_rejects_corrupted_coordinates_during_validation(self) -> None:
        """Verify that if a chunk with invalid coordinates is produced, engine rejects it."""
        from backend.ingestion.chunking.base import ChunkingStrategy

        class DefectiveChunker(ChunkingStrategy):
            """Dummy chunker that deliberately produces invalid coordinates."""
            @property
            def strategy_type(self) -> ChunkingStrategyType:
                return ChunkingStrategyType.FIXED_SLIDING_WINDOW

            async def chunk(self, extraction_result, context, config):
                return [
                    DocumentChunkInput(
                        chunk_id="defective-chunk-uuid",
                        tenant_id=context.tenant_id,
                        document_id=context.document_id,
                        chunk_index=0,
                        content="Defective chunk content",
                        content_hash="d" * 64,
                        token_count=5,
                        locator=ChunkLocator(page_number=999),  # Exceeds total_pages=1
                        acl=context.to_acl_payload(),
                    )
                ]

        engine = ChunkingEngine()
        engine.register_strategy(DefectiveChunker())

        context = IngestionContext(
            tenant_id="tenant-enterprise-vn-001",
            document_id="doc-test-fail-uuid",
            classification=DocumentClassification.INTERNAL,
        )

        extraction_res = ExtractionResult(
            document_id="doc-test-fail-uuid",
            document_type=DocumentType.PDF,
            mime_type=DocumentMimeType.PDF.value,
            elements=[ParagraphElement(text="Content", page_number=1)],
            metadata=DocumentMetadata(
                document_id="doc-test-fail-uuid",
                tenant_id="tenant-enterprise-vn-001",
                filename="test.pdf",
                file_hash_sha256="e" * 64,
                file_size_bytes=1024,
                mime_type=DocumentMimeType.PDF.value,
                document_type=DocumentType.PDF,
                total_pages=1,
            ),
        )

        config = ChunkingConfig(strategy=ChunkingStrategyType.FIXED_SLIDING_WINDOW)

        with pytest.raises(ProvenanceValidationError) as exc_info:
            await engine.chunk_document(extraction_res, context, config=config)

        err = exc_info.value
        assert err.field == "page_number"
        assert err.details["error_code"] == ProvenanceErrorCode.PAGE_OUT_OF_BOUNDS.value
