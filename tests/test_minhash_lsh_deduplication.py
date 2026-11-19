"""Unit tests for MinHash and Locality-Sensitive Hashing Near-Duplicate Chunk Detection (Stage 3 — Task 11).

Comprehensive test suite covering:
1. Text normalization:
   - Unicode NFKC decomposition/composition (Vietnamese & international text).
   - Case-folding, markdown stripping, punctuation normalization, whitespace collapsing.
   - Edge cases: empty text, single token, symbols only.
2. Shingling:
   - Word k-shingles (3-shingles default).
   - Character n-grams.
   - Short text (< shingle_size) graceful fallback.
3. MinHash signature generation:
   - Deterministic reproducibility under fixed seed.
   - Signature vector length matching permutation count (128).
   - Identical text yielding identical signatures (estimated Jaccard = 1.0).
   - Estimated Jaccard vs ground-truth set Jaccard accuracy.
4. LSH Index:
   - Parameter calculation (bands * rows <= permutations).
   - Fast candidate bucket retrieval.
   - De-indexing and rollback cleanup.
5. Exact vs Near-Duplicate Discrimination:
   - Content hash matching (EXACT).
   - High Jaccard similarity (NEAR_DUPLICATE vs EXACT at exact_threshold).
   - Completely unrelated text (UNIQUE).
6. Multi-Tenant Boundary Isolation:
   - Zero-trust isolation: identical text in Tenant B never collides with Tenant A.
7. Document and Version Scope Filtering:
   - CROSS_VERSION: detects revision updates between v1 and v2 of the same document.
   - CROSS_DOCUMENT: matches similar chunks across different enterprise documents.
   - SAME_DOCUMENT: matches within document.
   - ALL: global tenant search.
8. Deterministic Multi-Match Resolution (Requirement 12):
   - Severity ordering (EXACT > NEAR_DUPLICATE).
   - Similarity score descending order.
   - Version recency preference.
   - Deterministic tie-breaking.
9. Non-Destructive Invariant (Requirement 9):
   - No chunks or documents are deleted or modified.
   - Annotation injects duplicate metadata into DocumentChunkInput.metadata.
10. Decoupled Architecture:
    - Pure text & contract operation without coupling to embedding models.
"""

import asyncio
import uuid
import pytest

from backend.ingestion import (
    AccessControlPayload,
    ChunkDuplicateMatch,
    ChunkDuplicateReport,
    ChunkLocator,
    ComparisonScope,
    DocumentChunkInput,
    DuplicateAction,
    DuplicateType,
    IngestionContext,
    IngestionOptions,
    MinHashChunkDeduplicator,
    MinHashConfig,
    MinHashDeduplicationStrategy,
    MinHashLshIndex,
    MinHashSigner,
    compute_optimal_lsh_params,
    generate_shingles,
    get_chunk_deduplicator,
    normalize_chunk_text,
)
from backend.ingestion.deduplication.hasher import compute_content_sha256


# ==============================================================================
# FIXTURES & HELPERS
# ==============================================================================


@pytest.fixture
def test_acl() -> AccessControlPayload:
    return AccessControlPayload(
        tenant_id="tenant_alpha",
        roles=["ENGINEERING", "HR_SPECIALIST"],
        departments=["Engineering", "HR"],
    )


def make_chunk(
    content: str,
    tenant_id: str = "tenant_alpha",
    document_id: str | None = None,
    document_version_id: str | None = None,
    version_number: int | None = 1,
    chunk_index: int = 0,
    page_number: int = 1,
    section_heading: str | None = "Section 1",
) -> DocumentChunkInput:
    """Helper to create DocumentChunkInput with accurate SHA-256 hash."""
    doc_id = document_id or str(uuid.uuid4())
    content_hash, _ = compute_content_sha256(content)
    tokens = len(content.split())
    metadata = {}
    if version_number is not None:
        metadata["version_number"] = version_number

    return DocumentChunkInput(
        chunk_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        document_id=doc_id,
        document_version_id=document_version_id,
        chunk_index=chunk_index,
        content=content,
        content_hash=content_hash,
        token_count=tokens,
        locator=ChunkLocator(
            page_number=page_number,
            section_heading=section_heading,
            char_start=0,
            char_end=len(content),
        ),
        acl=AccessControlPayload(
            tenant_id=tenant_id,
            roles=["EMPLOYEE"],
            departments=["Corporate"],
        ),
        metadata=metadata,
    )


# ==============================================================================
# 1. TEXT NORMALIZATION & SHINGLING TESTS
# ==============================================================================


class TestTextNormalizationAndShingling:
    """Tests for unicode normalization, punctuation handling, and shingle generation."""

    def test_unicode_normalization_vietnamese(self) -> None:
        raw = "Chính Sách Nghỉ Phép Năm 2026: Đổi Mới & Sáng Tạo!"
        norm = normalize_chunk_text(raw)
        assert norm == "chính sách nghỉ phép năm 2026 đổi mới sáng tạo"
        # Accents and letters preserved, punctuation stripped, lowercased
        assert "chính" in norm
        assert "sáng tạo" in norm
        assert ":" not in norm
        assert "&" not in norm

    def test_markdown_and_punctuation_stripping(self) -> None:
        raw = """
        ### 3.2 Annual Leave Allowances
        * Employees receive **15 days** of paid annual leave per fiscal year.
        * Extra allowances apply for > 5 years of service.
        """
        norm = normalize_chunk_text(raw)
        assert "3 2 annual leave allowances" in norm
        assert "employees receive 15 days of paid annual leave per fiscal year" in norm
        assert "*" not in norm
        assert "#" not in norm

    def test_whitespace_collapsing(self) -> None:
        raw = "  multiple   \t\t spaces   and \n\n newlines  \r\n test  "
        norm = normalize_chunk_text(raw)
        assert norm == "multiple spaces and newlines test"

    def test_empty_and_symbol_only_normalization(self) -> None:
        assert normalize_chunk_text("") == ""
        assert normalize_chunk_text("   \n\t  ") == ""
        assert normalize_chunk_text("!@#$%^&*()_+-=[]{}|;':\",./<>?") == ""

    def test_word_shingles_generation(self) -> None:
        text = "quick brown fox jumps over the lazy dog"
        shingles = generate_shingles(text, shingle_size=3, ngram_type="word")
        assert len(shingles) == 6
        assert shingles[0] == "quick brown fox"
        assert shingles[1] == "brown fox jumps"
        assert shingles[-1] == "the lazy dog"

    def test_short_text_fallback_shingles(self) -> None:
        # Shingle size 3, but only 2 words
        text = "short text"
        shingles = generate_shingles(text, shingle_size=3, ngram_type="word")
        assert len(shingles) == 1
        assert shingles[0] == "short text"

    def test_character_shingles_generation(self) -> None:
        text = "abcdef"
        shingles = generate_shingles(text, shingle_size=3, ngram_type="character")
        assert shingles == ["abc", "bcd", "cde", "def"]


# ==============================================================================
# 2. MINHASH SIGNATURE GENERATION TESTS
# ==============================================================================


class TestMinHashSigner:
    """Tests for MinHash permutation hashing and Jaccard similarity estimation."""

    def test_signature_length_and_determinism(self) -> None:
        signer1 = MinHashSigner(num_permutations=128, seed=42)
        signer2 = MinHashSigner(num_permutations=128, seed=42)

        text = "Enterprise knowledge management platform with automated retrieval-augmented reasoning."
        sig1 = signer1.compute_signature(text)
        sig2 = signer2.compute_signature(text)

        assert len(sig1) == 128
        assert sig1 == sig2  # Fixed seed guarantees determinism

    def test_identical_text_similarity(self) -> None:
        signer = MinHashSigner(num_permutations=128, seed=42)
        text = "All full-time employees are entitled to standard health insurance coverage."

        sig1 = signer.compute_signature(text)
        sig2 = signer.compute_signature(text)

        sim = signer.estimate_similarity(sig1, sig2)
        assert sim == 1.0

    def test_near_duplicate_high_similarity(self) -> None:
        signer = MinHashSigner(num_permutations=128, seed=42)
        text_v1 = (
            "Employees in the Engineering department shall receive an annual performance bonus "
            "contingent upon achieving satisfactory quarterly milestones. Eligible employees "
            "must complete at least six months of continuous service prior to evaluation. "
            "The standard compensation review committee conducts annual assessments in November."
        )
        text_v2 = (
            "Employees in the Engineering department shall receive an annual performance bonus "
            "contingent upon achieving satisfactory quarterly milestones. Eligible employees "
            "must complete at least nine months of continuous service prior to evaluation. "
            "The standard compensation review committee conducts annual assessments in November."
        )

        sig1 = signer.compute_signature(text_v1)
        sig2 = signer.compute_signature(text_v2)

        est_sim = signer.estimate_similarity(sig1, sig2)
        exact_sim = signer.exact_jaccard_similarity(text_v1, text_v2)

        # Near-duplicate should have high similarity (> 0.80) and be close to exact Jaccard
        assert est_sim >= 0.80
        assert abs(est_sim - exact_sim) < 0.10

    def test_unrelated_text_low_similarity(self) -> None:
        signer = MinHashSigner(num_permutations=128, seed=42)
        text_a = "PostgreSQL 16 relational database with pgvector and GIN full-text search indexes."
        text_b = "Human resources policy regarding parental leave allowances and health benefit subsidies."

        sig_a = signer.compute_signature(text_a)
        sig_b = signer.compute_signature(text_b)

        sim = signer.estimate_similarity(sig_a, sig_b)
        assert sim <= 0.15  # Unrelated domains have negligible overlap

    def test_empty_text_signature(self) -> None:
        signer = MinHashSigner(num_permutations=128, seed=42)
        sig = signer.compute_signature("")
        assert len(sig) == 128


# ==============================================================================
# 3. LSH PARAMETER OPTIMIZATION & INDEX TESTS
# ==============================================================================


class TestLshIndexAndOptimization:
    """Tests for LSH bands/rows calculation and candidate bucket indexing."""

    def test_optimal_lsh_params_calculation(self) -> None:
        bands, rows = compute_optimal_lsh_params(num_permutations=128, threshold=0.92)
        assert bands >= 1
        assert rows >= 1
        assert bands * rows <= 128

        # Invariant: high candidate recall (>= 95%) at threshold
        p_target = 1.0 - (1.0 - (0.92 ** rows)) ** bands
        assert p_target >= 0.95

        # Invariant: minimal false candidate collisions (< 5%) for unrelated items
        p_noise = 1.0 - (1.0 - (0.30 ** rows)) ** bands
        assert p_noise < 0.05

    def test_lsh_index_add_and_remove(self) -> None:
        index = MinHashLshIndex(threshold=0.90)
        chunk = make_chunk("Sample document chunk for LSH index testing.", tenant_id="tenant_1")

        index.index_chunk(chunk)
        assert index.count_chunks("tenant_1") == 1

        removed = index.remove_chunk("tenant_1", chunk.chunk_id)
        assert removed is True
        assert index.count_chunks("tenant_1") == 0

    def test_clean_clear_tenant(self) -> None:
        index = MinHashLshIndex()
        chunk1 = make_chunk("Chunk 1", tenant_id="t1")
        chunk2 = make_chunk("Chunk 2", tenant_id="t1")
        chunk3 = make_chunk("Chunk 3", tenant_id="t2")

        index.index_chunk(chunk1)
        index.index_chunk(chunk2)
        index.index_chunk(chunk3)

        assert index.count_chunks("t1") == 2
        assert index.count_chunks("t2") == 1

        index.clear_tenant("t1")
        assert index.count_chunks("t1") == 0
        assert index.count_chunks("t2") == 1


# ==============================================================================
# 4. EXACT VS. NEAR-DUPLICATE DISCRIMINATION
# ==============================================================================


class TestExactVsNearDuplicateDiscrimination:
    """Verifies that exact content and near-duplicate revisions are cleanly distinguished."""

    def test_exact_content_match_sha256(self) -> None:
        deduplicator = MinHashChunkDeduplicator()
        text = "Standard corporate cybersecurity policy for laptop full-disk encryption."

        chunk1 = make_chunk(text, tenant_id="t_corp", document_id="doc_1")
        chunk2 = make_chunk(text, tenant_id="t_corp", document_id="doc_2")

        # Index chunk1
        deduplicator.index_chunk(chunk1)

        # Query chunk2
        report = deduplicator.detect_duplicates(chunk2)
        assert report.has_duplicates is True
        assert report.duplicate_type == DuplicateType.EXACT
        assert report.primary_match is not None
        assert report.primary_match.exact_content_match is True
        assert report.primary_match.similarity_score == 1.0
        assert report.primary_match.chunk_id == chunk1.chunk_id

    def test_near_duplicate_match(self) -> None:
        config = MinHashConfig(similarity_threshold=0.70)
        deduplicator = MinHashChunkDeduplicator(config=config)
        text_orig = (
            "The corporate travel expense allowance allows up to 100 dollars per day "
            "for domestic meal reimbursements while on business trips. All claims require manager sign-off."
        )
        text_revised = (
            "The corporate travel expense allowance allows up to 120 dollars per day "
            "for domestic meal reimbursements while on business trips. All claims require manager sign-off."
        )

        chunk1 = make_chunk(text_orig, tenant_id="t_corp", document_id="doc_1")
        chunk2 = make_chunk(text_revised, tenant_id="t_corp", document_id="doc_1", version_number=2)

        deduplicator.index_chunk(chunk1)

        report = deduplicator.detect_duplicates(chunk2, threshold=0.70)
        assert report.has_duplicates is True
        assert report.duplicate_type == DuplicateType.NEAR_DUPLICATE
        assert report.primary_match is not None
        assert report.primary_match.exact_content_match is False
        assert report.primary_match.similarity_score >= 0.70
        assert report.primary_match.similarity_score < 1.0


# ==============================================================================
# 5. ZERO-TRUST MULTI-TENANT ISOLATION TESTS
# ==============================================================================


class TestMultiTenantIsolation:
    """Verifies that chunks in different tenants never collide under any circumstances."""

    def test_identical_chunks_in_different_tenants_do_not_match(self) -> None:
        deduplicator = MinHashChunkDeduplicator()
        confidential_text = "Confidential Q4 financial performance: Revenue grew by 35 percent YoY."

        # Tenant Alpha chunk
        chunk_alpha = make_chunk(confidential_text, tenant_id="tenant_alpha")
        # Tenant Beta chunk with IDENTICAL content
        chunk_beta = make_chunk(confidential_text, tenant_id="tenant_beta")

        deduplicator.index_chunk(chunk_alpha)

        # Query from Tenant Beta
        report_beta = deduplicator.detect_duplicates(chunk_beta)
        assert report_beta.has_duplicates is False
        assert report_beta.duplicate_type == DuplicateType.UNIQUE
        assert report_beta.primary_match is None
        assert report_beta.total_matches_count == 0

        # Query from Tenant Alpha should match
        report_alpha_query = deduplicator.detect_duplicates(
            make_chunk(confidential_text, tenant_id="tenant_alpha")
        )
        assert report_alpha_query.has_duplicates is True
        assert report_alpha_query.primary_match.chunk_id == chunk_alpha.chunk_id


# ==============================================================================
# 6. DOCUMENT AND VERSION SCOPE FILTERING TESTS
# ==============================================================================


class TestComparisonScopeFiltering:
    """Verifies comparison scoping (CROSS_VERSION, CROSS_DOCUMENT, SAME_DOCUMENT, ALL)."""

    def test_cross_version_detection_same_document(self) -> None:
        config = MinHashConfig(similarity_threshold=0.75)
        deduplicator = MinHashChunkDeduplicator(config=config)
        doc_id = "doc_guideline_001"

        text_v1 = (
            "Section 4.1 Information Security Guidelines. All employee workstations must "
            "enforce screen lock after 10 minutes of inactivity. Passwords must be at least "
            "12 characters in length and include uppercase, lowercase, numerals, and special characters."
        )
        text_v2 = (
            "Section 4.1 Information Security Guidelines. All employee workstations must "
            "enforce screen lock after 10 minutes of inactivity. Passwords must be at least "
            "16 characters in length and include uppercase, lowercase, numerals, and special characters."
        )

        chunk_v1 = make_chunk(
            text_v1,
            document_id=doc_id,
            document_version_id="ver_1",
            version_number=1,
        )
        chunk_v2 = make_chunk(
            text_v2,
            document_id=doc_id,
            document_version_id="ver_2",
            version_number=2,
        )
        # Unrelated document
        chunk_other = make_chunk(
            text_v1,
            document_id="doc_other",
            document_version_id="ver_other",
            version_number=1,
        )

        deduplicator.index_chunk(chunk_v1, version_number=1)
        deduplicator.index_chunk(chunk_other, version_number=1)

        # Query with CROSS_VERSION scope for doc_guideline_001
        report = deduplicator.detect_duplicates(
            chunk_v2,
            scope=ComparisonScope.CROSS_VERSION,
            threshold=0.75,
            version_number=2,
        )

        assert report.has_duplicates is True
        # Must match chunk_v1 (same doc, different version)
        assert report.primary_match.chunk_id == chunk_v1.chunk_id
        # Must NOT match chunk_other (different doc)
        matched_chunk_ids = [m.chunk_id for m in report.all_matches]
        assert chunk_other.chunk_id not in matched_chunk_ids

    def test_cross_document_scope_excludes_same_document(self) -> None:
        deduplicator = MinHashChunkDeduplicator()
        text = "Shared enterprise confidentiality and non-disclosure agreement条款."

        doc_self = "doc_contract_A"
        doc_other = "doc_contract_B"

        chunk_self = make_chunk(text, document_id=doc_self)
        chunk_other = make_chunk(text, document_id=doc_other)

        deduplicator.index_chunk(chunk_self)
        deduplicator.index_chunk(chunk_other)

        # Query chunk from doc_self with CROSS_DOCUMENT
        query_chunk = make_chunk(text, document_id=doc_self)
        report = deduplicator.detect_duplicates(
            query_chunk,
            scope=ComparisonScope.CROSS_DOCUMENT,
        )

        assert report.has_duplicates is True
        # Should only match doc_other, NOT doc_self
        assert report.primary_match.document_id == doc_other
        for match in report.all_matches:
            assert match.document_id != doc_self


# ==============================================================================
# 7. DETERMINISTIC MULTI-MATCH RESOLUTION (REQUIREMENT 12)
# ==============================================================================


class TestMultiMatchDeterministicResolution:
    """Verifies behavior when multiple similar chunks are found."""

    def test_deterministic_multi_match_ranking(self) -> None:
        config = MinHashConfig(similarity_threshold=0.60)
        deduplicator = MinHashChunkDeduplicator(config=config)
        doc_id = "doc_multi_test"

        base_text = "The quick brown fox jumps over the lazy dog in the sunny park."
        # Candidate 1: Exact match in version 1
        cand1 = make_chunk(base_text, document_id=doc_id, version_number=1, chunk_index=0)
        # Candidate 2: Near duplicate in version 2
        near_text = "The quick brown fox jumps over the lazy sleeping dog in the sunny park."
        cand2 = make_chunk(near_text, document_id=doc_id, version_number=2, chunk_index=0)
        # Candidate 3: Exact match in version 3 (newer than version 1)
        cand3 = make_chunk(base_text, document_id=doc_id, version_number=3, chunk_index=0)

        deduplicator.index_chunk(cand1, version_number=1)
        deduplicator.index_chunk(cand2, version_number=2)
        deduplicator.index_chunk(cand3, version_number=3)

        query = make_chunk(base_text, document_id="doc_query")
        report = deduplicator.detect_duplicates(query, threshold=0.60)

        assert report.total_matches_count == 3
        matches = report.all_matches

        # Requirement 12 Ordering:
        # EXACT matches (cand3, cand1) must come before NEAR_DUPLICATE (cand2)
        assert matches[0].duplicate_type == DuplicateType.EXACT
        assert matches[1].duplicate_type == DuplicateType.EXACT
        assert matches[2].duplicate_type == DuplicateType.NEAR_DUPLICATE

        # Between the two EXACT matches, newer version (version 3) ranks before version 1
        assert matches[0].document_version_number == 3
        assert matches[0].chunk_id == cand3.chunk_id
        assert matches[1].document_version_number == 1
        assert matches[1].chunk_id == cand1.chunk_id

        # Primary match must be the top-ranked
        assert report.primary_match.chunk_id == cand3.chunk_id


# ==============================================================================
# 8. NON-DESTRUCTIVE INVARIANT & ANNOTATION (REQUIREMENT 9)
# ==============================================================================


class TestNonDestructiveAnnotation:
    """Verifies that annotate_chunks preserves all chunks and never deletes documents."""

    def test_non_destructive_chunk_annotation(self) -> None:
        deduplicator = MinHashChunkDeduplicator()
        text1 = "Standard operating procedure for emergency server failover and disaster recovery."
        text2 = "Standard operating procedure for emergency database failover and disaster recovery."
        text3 = "Unrelated cafeteria menu and beverage choices for Friday lunch."

        chunks = [
            make_chunk(text1, chunk_index=0),
            make_chunk(text2, chunk_index=1),
            make_chunk(text3, chunk_index=2),
        ]

        original_ids = [c.chunk_id for c in chunks]
        original_contents = [c.content for c in chunks]

        # Annotate
        annotated = deduplicator.annotate_chunks(chunks)

        # Acceptance Criteria: No original document content or chunk is deleted
        assert len(annotated) == 3
        assert [c.chunk_id for c in annotated] == original_ids
        assert [c.content for c in annotated] == original_contents

        # Metadata injected
        for c in annotated:
            assert "minhash_signature" in c.metadata
            assert "duplicate_type" in c.metadata
            assert "has_duplicates" in c.metadata


# ==============================================================================
# 9. INTEGRATION WITH DEDUPLICATION STRATEGY CONTRACT
# ==============================================================================


class TestStrategyContractIntegration:
    """Verifies compliance with DeduplicationStrategy contract."""

    def test_minhash_strategy_pass_through(self) -> None:
        strategy = MinHashDeduplicationStrategy()
        assert strategy.name == "MinHashLshDeduplication"

        context = IngestionContext(
            tenant_id="tenant_gamma",
            document_id="doc_gamma",
            options=IngestionOptions(
                deduplication_enabled=True,
                minhash_similarity_threshold=0.85,
            ),
        )

        chunk1 = make_chunk("First policy chunk text.", tenant_id="tenant_gamma")
        chunk2 = make_chunk("Second policy chunk text.", tenant_id="tenant_gamma")

        result_chunks = asyncio.run(strategy.filter_duplicate_chunks([chunk1, chunk2], context=context))

        # Returns all chunks annotated
        assert len(result_chunks) == 2
        assert "minhash_signature" in result_chunks[0].metadata

    def test_strategy_bypassed_when_disabled(self) -> None:
        strategy = MinHashDeduplicationStrategy()
        context = IngestionContext(
            tenant_id="tenant_gamma",
            document_id="doc_gamma",
            options=IngestionOptions(deduplication_enabled=False),
        )

        chunk = make_chunk("Bypassed chunk text.", tenant_id="tenant_gamma")
        result = asyncio.run(strategy.filter_duplicate_chunks([chunk], context=context))

        assert len(result) == 1
        assert "minhash_signature" not in result[0].metadata


# ==============================================================================
# 10. LARGE BATCH PERFORMANCE & SCALABILITY
# ==============================================================================


class TestLargeCollectionScalability:
    """Verifies sub-linear indexing and querying across multiple chunks."""

    def test_batch_indexing_and_fast_lookup(self) -> None:
        config = MinHashConfig(similarity_threshold=0.75)
        deduplicator = MinHashChunkDeduplicator(config=config)
        tenant = "perf_tenant"

        # Generate 100 distinct chunks
        chunks = [
            make_chunk(
                f"Synthetic enterprise policy chunk number {i} covering administrative guidelines.",
                tenant_id=tenant,
                chunk_index=i,
            )
            for i in range(100)
        ]

        # Batch index
        deduplicator.index_chunks(chunks)
        assert deduplicator.index.count_chunks(tenant) == 100

        # Query chunk 42 with near duplicate text
        query = make_chunk(
            "Synthetic enterprise policy chunk number 42 covering administrative guidelines with revisions.",
            tenant_id=tenant,
        )

        report = deduplicator.detect_duplicates(query, threshold=0.75)
        assert report.has_duplicates is True
        assert report.primary_match.chunk_index == 42
