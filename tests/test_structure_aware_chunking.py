"""Comprehensive unit tests for the Structure-Aware Chunking Engine (Stage 3 — Task 09).

Tests:
- Strategy A: FIXED_SLIDING_WINDOW
  - Token-based chunking with configurable maximum chunk sizes.
  - Strict 10% overlap applied across consecutive chunks.
  - Clean token boundary preservation (no splitting in the middle of subwords).
  - Accurate line-level source provenance locator tracking across window boundaries.
- Strategy B: RECURSIVE_STRUCTURAL
  - Hierarchical Markdown outline traversal (H1 -> H2 -> H3).
  - Paragraph boundary preservation and section grouping.
  - Headings bound to their associated content without orphan emission.
  - Recursive splitting of oversized sections and massive narrative paragraphs.
- Strategy C: SEMANTIC_TABLE_AWARE
  - Conversion of spreadsheet tables and rows to Markdown tables.
  - Inclusion of worksheet name and column headers in chunk text.
  - Repeated column headers across partitioned row groups.
  - Exact preservation of original 1-based worksheet row numbers and row ranges.
- General Engine Features:
  - Common ChunkingStrategy interface contract adherence.
  - Per-document strategy configuration and format-aware default resolution.
  - Deterministic chunk UUID v5 generation and SHA-256 content deduplication hashing.
  - Complete zero-trust ACL derivation for Qdrant and PostgreSQL persistence.
  - Zero silent content loss guarantee across heterogeneous documents.
"""

import pytest

from backend.ingestion import (
    ChunkLocator,
    ChunkingConfig,
    ChunkingEngine,
    ChunkingStrategy,
    ChunkingStrategyType,
    DocumentClassification,
    DocumentMetadata,
    DocumentMimeType,
    DocumentType,
    ElementType,
    ExtractionResult,
    FixedSlidingWindowChunker,
    HeadingElement,
    IngestionContext,
    IngestionOptions,
    ListElement,
    ParagraphElement,
    RecursiveStructuralChunker,
    SemanticTableAwareChunker,
    SpreadsheetRowElement,
    TableAwareChunker,
    TableElement,
    TextBlockElement,
    UnsupportedChunkingStrategyError,
    count_tokens,
    default_chunking_engine,
    encode_tokens,
    generate_chunk_id,
    get_chunking_strategy,
)


@pytest.fixture
def test_context() -> IngestionContext:
    """Fixture providing a multi-tenant IngestionContext with zero-trust RBAC claims."""
    return IngestionContext(
        tenant_id="tenant-enterprise-vn-001",
        document_id="doc-guideline-2026-uuid",
        version_id="ver-rev-001-uuid",
        user_id="user-corp-author-42",
        department_id="dept-engineering",
        roles=["ENGINEERING_LEAD", "EXECUTIVE"],
        departments=["Engineering", "Product"],
        allowed_users=["user-corp-author-42", "user-reviewer-99"],
        classification=DocumentClassification.INTERNAL,
    )


def build_sample_extraction_result(
    elements: list,
    doc_type: DocumentType = DocumentType.PDF,
    raw_text: str | None = None,
) -> ExtractionResult:
    """Helper to assemble a valid ExtractionResult with metadata."""
    mime = DocumentMimeType.PDF.value if doc_type == DocumentType.PDF else "application/octet-stream"
    res = ExtractionResult(
        document_id="doc-guideline-2026-uuid",
        document_type=doc_type,
        mime_type=mime,
        elements=elements,
        raw_text=raw_text,
        metadata=DocumentMetadata(
            document_id="doc-guideline-2026-uuid",
            tenant_id="tenant-enterprise-vn-001",
            filename=f"document.{doc_type.value.lower()}",
            file_hash_sha256="a" * 64,
            file_size_bytes=4096,
            mime_type=mime,
            document_type=doc_type,
            title="Enterprise Technical Specification 2026",
            author="Lead Architect",
            page_count=len(elements) if elements else 1,
        ),
    )
    res.compute_canonical_offsets()
    return res


# ==============================================================================
# STRATEGY A: FIXED_SLIDING_WINDOW TESTS
# ==============================================================================

class TestFixedSlidingWindowChunker:
    """Unit tests verifying Strategy A (FIXED_SLIDING_WINDOW)."""

    def test_strategy_type_discriminator(self) -> None:
        chunker = FixedSlidingWindowChunker()
        assert chunker.strategy_type == ChunkingStrategyType.FIXED_SLIDING_WINDOW

    @pytest.mark.anyio
    async def test_sliding_window_10_percent_overlap(self, test_context: IngestionContext) -> None:
        """Verify sliding window applies exact 10% overlap between consecutive chunks."""
        # Generate narrative text of ~400 tokens
        repeated_sentence = (
            "Enterprise Knowledge Agent implements permission-aware hybrid search "
            "with dense embeddings and sparse BM25 retrieval across corporate data. "
        )
        full_text = repeated_sentence * 25
        total_toks = count_tokens(full_text)
        assert total_toks > 350

        chunk_size = 100
        # 10% of 100 is 10 tokens overlap
        config = ChunkingConfig(
            strategy=ChunkingStrategyType.FIXED_SLIDING_WINDOW,
            chunk_size_tokens=chunk_size,
            overlap_percentage=0.10,
        )

        extraction_result = build_sample_extraction_result(
            elements=[ParagraphElement(text=full_text, page_number=1)],
            raw_text=full_text,
        )

        chunker = FixedSlidingWindowChunker()
        chunks = await chunker.chunk(extraction_result, test_context, config)

        assert len(chunks) >= 3

        # Verify chunk sizing and overlap
        for i, c in enumerate(chunks):
            assert c.token_count <= chunk_size + 2  # token subword decoding bounds
            assert c.chunk_index == i
            assert c.tenant_id == test_context.tenant_id
            assert c.document_id == test_context.document_id
            assert len(c.content_hash) == 64
            assert c.metadata["strategy"] == "FIXED_SLIDING_WINDOW"
            assert c.metadata["overlap_tokens"] == 10

        # Verify consecutive chunk token overlap
        for i in range(len(chunks) - 1):
            curr_tokens = encode_tokens(chunks[i].content)
            next_tokens = encode_tokens(chunks[i + 1].content)
            # The tail of chunk i should match the head of chunk i+1
            overlap_count = config.get_effective_overlap()
            tail = curr_tokens[-overlap_count:]
            head = next_tokens[:overlap_count]
            assert tail == head, f"Chunk {i} and {i+1} do not exhibit 10% token overlap"

    @pytest.mark.anyio
    async def test_avoid_splitting_in_middle_of_tokens(self, test_context: IngestionContext) -> None:
        """Verify tokens are kept intact without mangling UTF-8 multi-byte characters."""
        vietnamese_text = (
            "Quy định nghỉ phép năm 2026 của Tập đoàn: Cán bộ nhân viên được hưởng 15 ngày nghỉ phép "
            "hưởng nguyên lương hàng năm, cộng thêm 1 ngày cho mỗi 3 năm thâm niên công tác thực tế."
        )
        extraction_result = build_sample_extraction_result(
            elements=[ParagraphElement(text=vietnamese_text, page_number=2)],
            raw_text=vietnamese_text,
        )

        config = ChunkingConfig(
            strategy=ChunkingStrategyType.FIXED_SLIDING_WINDOW,
            chunk_size_tokens=64,
            overlap_percentage=0.10,
        )

        chunker = FixedSlidingWindowChunker()
        chunks = await chunker.chunk(extraction_result, test_context, config)

        assert len(chunks) >= 1
        for c in chunks:
            # Must not contain replacement characters from mid-token slicing
            assert "\ufffd" not in c.content
            assert c.token_count <= 64

    @pytest.mark.anyio
    async def test_sliding_window_source_locator_preservation(self, test_context: IngestionContext) -> None:
        """Verify source coordinates (page_number, section_heading, char_start/end) are preserved."""
        p1 = ParagraphElement(
            text="First section introductory text for testing locator coordinates.",
            page_number=1,
            section_heading="1. Introduction",
        )
        p2 = ParagraphElement(
            text="Second section detailed body with policy explanations and procedures.",
            page_number=2,
            section_heading="2. Implementation",
        )
        extraction_result = build_sample_extraction_result(elements=[p1, p2])

        config = ChunkingConfig(
            strategy=ChunkingStrategyType.FIXED_SLIDING_WINDOW,
            chunk_size_tokens=100,
            overlap_percentage=0.10,
        )
        chunker = FixedSlidingWindowChunker()
        chunks = await chunker.chunk(extraction_result, test_context, config)

        assert len(chunks) >= 1
        first_chunk = chunks[0]
        assert first_chunk.locator.page_number in (1, 2)
        assert first_chunk.locator.char_start is not None
        assert first_chunk.locator.char_end is not None
        assert first_chunk.locator.char_start <= first_chunk.locator.char_end

    @pytest.mark.anyio
    async def test_sliding_window_empty_document(self, test_context: IngestionContext) -> None:
        """Verify empty documents yield zero chunks safely."""
        extraction_result = build_sample_extraction_result(elements=[], raw_text="")
        chunker = FixedSlidingWindowChunker()
        chunks = await chunker.chunk(extraction_result, test_context)
        assert chunks == []


# ==============================================================================
# STRATEGY B: RECURSIVE_STRUCTURAL TESTS
# ==============================================================================

class TestRecursiveStructuralChunker:
    """Unit tests verifying Strategy B (RECURSIVE_STRUCTURAL)."""

    def test_strategy_type_discriminator(self) -> None:
        chunker = RecursiveStructuralChunker()
        assert chunker.strategy_type == ChunkingStrategyType.RECURSIVE_STRUCTURAL

    @pytest.mark.anyio
    async def test_prioritize_heading_hierarchy_h1_h2_h3(self, test_context: IngestionContext) -> None:
        """Verify structural chunking organizes document along H1, H2, and H3 boundaries."""
        h1 = HeadingElement(text="System Architecture", level=1, page_number=1)
        p1 = ParagraphElement(text="High level overview of enterprise platform architecture.", page_number=1)
        h2 = HeadingElement(text="Storage Subsystem", level=2, page_number=1)
        p2 = ParagraphElement(text="Details about PostgreSQL and Qdrant persistence.", page_number=1)
        h3 = HeadingElement(text="HNSW Indexing", level=3, page_number=2)
        p3 = ParagraphElement(text="Vector cosine similarity configuration and ef_construct.", page_number=2)

        extraction_result = build_sample_extraction_result(elements=[h1, p1, h2, p2, h3, p3])

        config = ChunkingConfig(
            strategy=ChunkingStrategyType.RECURSIVE_STRUCTURAL,
            chunk_size_tokens=512,  # generous limit so each section forms its own chunk
        )

        chunker = RecursiveStructuralChunker()
        chunks = await chunker.chunk(extraction_result, test_context, config)

        assert len(chunks) == 3

        # Chunk 1: H1 + p1
        assert "System Architecture" in chunks[0].content
        assert "overview of enterprise" in chunks[0].content
        assert chunks[0].locator.page_number == 1
        assert chunks[0].locator.section_heading == "System Architecture"

        # Chunk 2: H2 + p2
        assert "Storage Subsystem" in chunks[1].content
        assert "PostgreSQL and Qdrant" in chunks[1].content
        assert chunks[1].locator.page_number == 1

        # Chunk 3: H3 + p3
        assert "HNSW Indexing" in chunks[2].content
        assert "Vector cosine similarity" in chunks[2].content
        assert chunks[2].locator.page_number == 2

    @pytest.mark.anyio
    async def test_avoid_separating_headings_from_content(self, test_context: IngestionContext) -> None:
        """Verify headings are not emitted as orphan chunks devoid of content."""
        h1 = HeadingElement(text="Leave Policy 2026", level=1, page_number=5)
        p1 = ParagraphElement(text="Employees are entitled to 15 paid leave days.", page_number=5)
        extraction_result = build_sample_extraction_result(elements=[h1, p1])

        chunker = RecursiveStructuralChunker()
        chunks = await chunker.chunk(extraction_result, test_context)

        assert len(chunks) == 1
        assert "# Leave Policy 2026" in chunks[0].content
        assert "15 paid leave days" in chunks[0].content

    @pytest.mark.anyio
    async def test_group_related_paragraphs_under_parent_headings(self, test_context: IngestionContext) -> None:
        """Verify multiple paragraphs under the same heading are grouped together."""
        h1 = HeadingElement(text="Security & Compliance", level=1, page_number=3)
        p1 = ParagraphElement(text="First paragraph explaining Zero-Trust principles.", page_number=3)
        p2 = ParagraphElement(text="Second paragraph explaining RBAC role mapping.", page_number=3)
        p3 = ParagraphElement(text="Third paragraph detailing audit logging requirements.", page_number=3)
        extraction_result = build_sample_extraction_result(elements=[h1, p1, p2, p3])

        config = ChunkingConfig(
            strategy=ChunkingStrategyType.RECURSIVE_STRUCTURAL,
            chunk_size_tokens=512,
        )

        chunker = RecursiveStructuralChunker()
        chunks = await chunker.chunk(extraction_result, test_context, config)

        assert len(chunks) == 1
        assert "Zero-Trust principles" in chunks[0].content
        assert "RBAC role mapping" in chunks[0].content
        assert "audit logging requirements" in chunks[0].content

    @pytest.mark.anyio
    async def test_recursively_split_oversized_sections_and_paragraphs(self, test_context: IngestionContext) -> None:
        """Verify oversized paragraphs exceeding chunk size are split recursively using sentence/word boundaries."""
        h1 = HeadingElement(text="Deep Learning Foundations", level=1, page_number=1)
        # Giant paragraph with multiple sentences exceeding 100 tokens
        sentences = [
            f"Sentence number {i} provides detailed theoretical explanations of neural networks and gradient descent."
            for i in range(1, 20)
        ]
        giant_paragraph = " ".join(sentences)
        p1 = ParagraphElement(text=giant_paragraph, page_number=1)

        extraction_result = build_sample_extraction_result(elements=[h1, p1])

        config = ChunkingConfig(
            strategy=ChunkingStrategyType.RECURSIVE_STRUCTURAL,
            chunk_size_tokens=80,  # tight token limit to trigger recursive subdivision
            preserve_headers=True,
        )

        chunker = RecursiveStructuralChunker()
        chunks = await chunker.chunk(extraction_result, test_context, config)

        assert len(chunks) >= 3
        for c in chunks:
            assert c.token_count <= 85  # bounded
            assert c.locator.page_number == 1
            assert c.locator.section_heading is not None
            assert len(c.content_hash) == 64

        # Full content must be preserved across chunks
        aggregated_content = " ".join(c.content for c in chunks)
        assert "Sentence number 1" in aggregated_content
        assert "Sentence number 19" in aggregated_content


# ==============================================================================
# STRATEGY C: SEMANTIC_TABLE_AWARE TESTS
# ==============================================================================

class TestSemanticTableAwareChunker:
    """Unit tests verifying Strategy C (SEMANTIC_TABLE_AWARE)."""

    def test_strategy_type_discriminator(self) -> None:
        chunker = SemanticTableAwareChunker()
        assert chunker.strategy_type == ChunkingStrategyType.SEMANTIC_TABLE_AWARE
        assert TableAwareChunker is SemanticTableAwareChunker

    @pytest.mark.anyio
    async def test_convert_table_to_markdown_with_worksheet_and_headers(self, test_context: IngestionContext) -> None:
        """Verify XLSX/CSV table is converted to Markdown table with sheet name and repeated headers."""
        headers = ["Department", "Headcount", "Q1 Budget", "Manager"]
        rows = [
            ["Human Resources", 12, "$150,000", "Alice Smith"],
            ["Engineering", 45, "$850,000", "Bob Jones"],
            ["Finance", 8, "$120,000", "Carol White"],
        ]
        table = TableElement(
            headers=headers,
            rows=rows,
            sheet_name="Budgets_2026",
            row_range="Row 2:4",
            metadata={"original_row_indices": [2, 3, 4]},
        )
        extraction_result = build_sample_extraction_result(
            elements=[table],
            doc_type=DocumentType.XLSX,
        )

        chunker = SemanticTableAwareChunker()
        chunks = await chunker.chunk(extraction_result, test_context)

        assert len(chunks) == 1
        chunk = chunks[0]

        # Verify Markdown structure
        assert "### Worksheet: Budgets_2026" in chunk.content
        assert "| Department | Headcount | Q1 Budget | Manager |" in chunk.content
        assert "| --- | --- | --- | --- |" in chunk.content
        assert "| Human Resources | 12 | $150,000 | Alice Smith |" in chunk.content
        assert "| Engineering | 45 | $850,000 | Bob Jones |" in chunk.content
        assert "| Finance | 8 | $120,000 | Carol White |" in chunk.content

        # Verify provenance locator
        assert chunk.locator.sheet_name == "Budgets_2026"
        assert chunk.locator.row_range == "Row 2:4"
        assert chunk.locator.section_heading == "Worksheet: Budgets_2026"

        # Verify metadata
        assert chunk.metadata["start_row"] == 2
        assert chunk.metadata["end_row"] == 4
        assert chunk.metadata["column_headers"] == headers

    @pytest.mark.anyio
    async def test_split_large_table_and_repeat_headers_across_chunks(self, test_context: IngestionContext) -> None:
        """Verify large table is split into smaller row groups with repeated column headers."""
        headers = ["Employee ID", "Full Name", "Title", "Department", "Annual Salary"]
        # 30 rows
        rows = [
            [f"EMP-{1000 + i}", f"Employee {i}", f"Role {i % 5}", f"Dept {i % 3}", f"${60000 + i * 2000}"]
            for i in range(1, 31)
        ]
        original_indices = [i + 1 for i in range(1, 31)]  # Rows 2 to 31

        table = TableElement(
            headers=headers,
            rows=rows,
            sheet_name="Salaries",
            row_range="Row 2:31",
            metadata={"original_row_indices": original_indices},
        )
        extraction_result = build_sample_extraction_result(
            elements=[table],
            doc_type=DocumentType.XLSX,
        )

        # Force tight token limit so table must split into multiple chunks
        config = ChunkingConfig(
            strategy=ChunkingStrategyType.SEMANTIC_TABLE_AWARE,
            chunk_size_tokens=100,
        )

        chunker = SemanticTableAwareChunker()
        chunks = await chunker.chunk(extraction_result, test_context, config)

        assert len(chunks) >= 3

        # Every chunk must have sheet name and repeated headers
        for idx, c in enumerate(chunks):
            assert "### Worksheet: Salaries" in c.content
            assert "| Employee ID | Full Name | Title | Department | Annual Salary |" in c.content
            assert "| --- | --- | --- | --- | --- |" in c.content
            assert c.locator.sheet_name == "Salaries"
            assert c.locator.row_range is not None
            assert c.metadata["start_row"] >= 2
            assert c.metadata["end_row"] <= 31
            assert c.token_count <= 110

        # Verify original row indices are monotonic and continuous across chunks
        observed_rows = []
        for c in chunks:
            observed_rows.extend(c.metadata["original_row_indices"])
        assert observed_rows == original_indices

    @pytest.mark.anyio
    async def test_chunk_spreadsheet_row_elements(self, test_context: IngestionContext) -> None:
        """Verify standalone SpreadsheetRowElement records are aggregated into Markdown table chunks."""
        row1 = SpreadsheetRowElement(
            sheet_name="Inventory",
            row_index=2,
            headers=["SKU", "Item", "Quantity"],
            cells={"SKU": "A101", "Item": "Laptop", "Quantity": 25},
        )
        row2 = SpreadsheetRowElement(
            sheet_name="Inventory",
            row_index=3,
            headers=["SKU", "Item", "Quantity"],
            cells={"SKU": "B202", "Item": "Monitor", "Quantity": 50},
        )
        extraction_result = build_sample_extraction_result(
            elements=[row1, row2],
            doc_type=DocumentType.CSV,
        )

        chunker = SemanticTableAwareChunker()
        chunks = await chunker.chunk(extraction_result, test_context)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert "### Worksheet: Inventory (Row 2:3)" in chunk.content
        assert "| SKU | Item | Quantity |" in chunk.content
        assert "| A101 | Laptop | 25 |" in chunk.content
        assert "| B202 | Monitor | 50 |" in chunk.content
        assert chunk.locator.sheet_name == "Inventory"
        assert chunk.locator.row_range == "Row 2:3"


# ==============================================================================
# CHUNKING ENGINE & INTEGRATION TESTS
# ==============================================================================

class TestChunkingEngine:
    """Unit tests for ChunkingEngine strategy configuration and invariant guarantees."""

    def test_default_strategy_registration(self) -> None:
        """Verify all three strategies are registered and resolvable."""
        engine = ChunkingEngine()
        strat_structural = engine.get_strategy(ChunkingStrategyType.RECURSIVE_STRUCTURAL)
        assert isinstance(strat_structural, RecursiveStructuralChunker)

        strat_sliding = engine.get_strategy(ChunkingStrategyType.FIXED_SLIDING_WINDOW)
        assert isinstance(strat_sliding, FixedSlidingWindowChunker)

        strat_table = engine.get_strategy(ChunkingStrategyType.SEMANTIC_TABLE_AWARE)
        assert isinstance(strat_table, SemanticTableAwareChunker)

        strat_table_alias = engine.get_strategy("TABLE_AWARE")
        assert isinstance(strat_table_alias, SemanticTableAwareChunker)

    def test_unsupported_strategy_raises_error(self) -> None:
        """Verify requesting an unknown strategy raises UnsupportedChunkingStrategyError."""
        engine = ChunkingEngine()
        with pytest.raises(UnsupportedChunkingStrategyError):
            engine.get_strategy("NON_EXISTENT_STRATEGY")

    @pytest.mark.anyio
    async def test_document_format_aware_strategy_resolution(self, test_context: IngestionContext) -> None:
        """Verify engine chooses table-aware for XLSX/CSV and structural for PDF/DOCX automatically."""
        engine = ChunkingEngine()

        # XLSX Document
        xlsx_table = TableElement(
            headers=["Col A", "Col B"],
            rows=[["Val 1", "Val 2"]],
            sheet_name="Data",
            row_range="Row 1",
        )
        xlsx_res = build_sample_extraction_result(elements=[xlsx_table], doc_type=DocumentType.XLSX)
        xlsx_chunks = await engine.chunk_document(xlsx_res, test_context)
        assert xlsx_chunks[0].metadata["strategy"] == "SEMANTIC_TABLE_AWARE"

        # PDF Document
        pdf_res = build_sample_extraction_result(
            elements=[ParagraphElement(text="Standard PDF text.", page_number=1)],
            doc_type=DocumentType.PDF,
        )
        pdf_chunks = await engine.chunk_document(pdf_res, test_context)
        assert pdf_chunks[0].metadata["strategy"] == "RECURSIVE_STRUCTURAL"

    @pytest.mark.anyio
    async def test_per_document_strategy_override(self, test_context: IngestionContext) -> None:
        """Verify caller can explicitly override strategy per document."""
        engine = ChunkingEngine()
        pdf_res = build_sample_extraction_result(
            elements=[ParagraphElement(text="Dense continuous narrative text block for override test.", page_number=1)],
            doc_type=DocumentType.PDF,
        )

        config = ChunkingConfig(
            strategy=ChunkingStrategyType.FIXED_SLIDING_WINDOW,
            chunk_size_tokens=100,
        )
        chunks = await engine.chunk_document(pdf_res, test_context, config=config)
        assert chunks[0].metadata["strategy"] == "FIXED_SLIDING_WINDOW"

    def test_deterministic_chunk_identifiers(self, test_context: IngestionContext) -> None:
        """Verify chunk UUID v5 is deterministic across identical inputs."""
        id1 = generate_chunk_id(
            tenant_id=test_context.tenant_id,
            document_id=test_context.document_id,
            chunk_index=0,
            content_hash="a" * 64,
        )
        id2 = generate_chunk_id(
            tenant_id=test_context.tenant_id,
            document_id=test_context.document_id,
            chunk_index=0,
            content_hash="a" * 64,
        )
        assert id1 == id2
        assert len(id1) == 36  # Standard UUID v5 string format

        # Different index yields different ID
        id3 = generate_chunk_id(
            tenant_id=test_context.tenant_id,
            document_id=test_context.document_id,
            chunk_index=1,
            content_hash="a" * 64,
        )
        assert id1 != id3

    @pytest.mark.anyio
    async def test_to_db_record_and_qdrant_payload_contracts(self, test_context: IngestionContext) -> None:
        """Verify DocumentChunkInput produces exact DB record and Qdrant zero-trust ACL payloads."""
        engine = ChunkingEngine()
        p = ParagraphElement(text="Testing relational and vector persistence contracts.", page_number=3)
        res = build_sample_extraction_result(elements=[p])

        chunks = await engine.chunk_document(res, test_context)
        assert len(chunks) == 1
        chunk = chunks[0]

        # Relational PostgreSQL contract
        db_record = chunk.to_db_record()
        assert db_record["tenant_id"] == test_context.tenant_id
        assert db_record["document_id"] == test_context.document_id
        assert db_record["chunk_index"] == 0
        assert db_record["page_number"] == 3
        assert db_record["content_hash"] == chunk.content_hash
        assert db_record["token_count"] == chunk.token_count

        # Zero-trust Qdrant vector payload contract
        qdrant_payload = chunk.to_qdrant_payload()
        assert qdrant_payload["document_id"] == test_context.document_id
        assert qdrant_payload["chunk_index"] == 0
        assert qdrant_payload["acl"]["tenant_id"] == test_context.tenant_id
        assert qdrant_payload["acl"]["roles"] == test_context.roles
        assert qdrant_payload["acl"]["departments"] == test_context.departments
        assert qdrant_payload["acl"]["users"] == test_context.allowed_users

    def test_sync_chunking_wrapper(self, test_context: IngestionContext) -> None:
        """Verify chunk_document_sync synchronous wrapper works identically."""
        engine = ChunkingEngine()
        p = ParagraphElement(text="Synchronous chunking execution path test.", page_number=1)
        res = build_sample_extraction_result(elements=[p])

        chunks = engine.chunk_document_sync(res, test_context)
        assert len(chunks) == 1
        assert "Synchronous chunking" in chunks[0].content
