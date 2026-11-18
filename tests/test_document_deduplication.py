"""Unit tests for SHA-256 Exact Document Deduplication, Streaming Hash I/O, and Policies.

Verifies:
1. Streaming SHA-256 calculation chunk-by-chunk (memory efficiency).
2. Rewind-after functionality on seekable streams.
3. Hash calculation independent from document parsing.
4. Empty file handling (0-byte file detection).
5. Exact active duplicate detection within tenant scope.
6. Zero-trust tenant boundary isolation (identical hash permitted across distinct tenants).
7. Soft-deletion aware deduplication (soft-deleted doc allows re-uploading identical hash).
8. Duplicate handling policies (RETURN_EXISTING, REJECT, NEW_VERSION).
9. Race-safe concurrency simulation (concurrent duplicate uploads handled without duplicate active records).
10. Sha256ExactDeduplicationStrategy interface compliance.
"""

import asyncio
import hashlib
import io
import tempfile
import uuid
from pathlib import Path

import pytest

from backend.ingestion.config import IngestionSettings
from backend.ingestion.deduplication import (
    DeduplicationResult,
    DeduplicationStrategy,
    DocumentCreateInput,
    DocumentDeduplicator,
    DocumentVersionCreateInput,
    DuplicateAction,
    DuplicateHandlingPolicy,
    InMemoryDocumentRepository,
    Sha256ExactDeduplicationStrategy,
    StreamHasher,
    compute_bytes_sha256,
    compute_content_sha256,
    compute_file_sha256,
    compute_stream_sha256,
)
from backend.ingestion.exceptions import (
    DuplicateDocumentError,
    EmptyFileError,
    ValidationError,
)
from backend.ingestion.types.context import IngestionContext, IngestionOptions


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

def make_test_context(
    tenant_id: str | None = None,
    document_id: str | None = None,
    policy: DuplicateHandlingPolicy | str = DuplicateHandlingPolicy.RETURN_EXISTING,
) -> IngestionContext:
    """Generate isolated IngestionContext for testing."""
    return IngestionContext(
        tenant_id=tenant_id or f"tenant_{uuid.uuid4().hex[:8]}",
        document_id=document_id or f"doc_{uuid.uuid4().hex[:12]}",
        options=IngestionOptions(
            deduplication_enabled=True,
            duplicate_handling_policy=policy if isinstance(policy, str) else policy.value,
        ),
    )


# ===========================================================================
# 1. STREAMING HASH COMPUTATION & MEMORY EFFICIENCY
# ===========================================================================

class TestStreamingSha256Hasher:
    """Verify streaming SHA-256 computation, memory safety, and stream rewind."""

    def test_hash_bytes_identical_to_standard_hashlib(self) -> None:
        payload = b"Enterprise confidential policy document body content for testing."
        expected_hash = hashlib.sha256(payload).hexdigest()
        expected_size = len(payload)

        calculated_hash, calculated_size = compute_bytes_sha256(payload)
        assert calculated_hash == expected_hash
        assert calculated_size == expected_size

    def test_hash_stream_with_small_chunks(self) -> None:
        # 100 KB payload read in tiny 1 KB chunks
        payload = b"A" * 1024 * 100
        expected_hash = hashlib.sha256(payload).hexdigest()

        stream = io.BytesIO(payload)
        hasher = StreamHasher(chunk_size=1024)
        calculated_hash, total_bytes = hasher.hash_stream(stream, chunk_size=1024)

        assert calculated_hash == expected_hash
        assert total_bytes == len(payload)

    def test_stream_rewind_after_hashing(self) -> None:
        payload = b"Stream content that must be rewound for downstream extractor."
        stream = io.BytesIO(payload)

        # Hash with rewind_after=True
        hasher = StreamHasher()
        h, size = hasher.hash_stream(stream, rewind_after=True)
        assert size == len(payload)

        # Verify cursor position is reset to 0
        assert stream.tell() == 0
        read_back = stream.read()
        assert read_back == payload

    def test_stream_without_rewind(self) -> None:
        payload = b"Non-rewound stream content."
        stream = io.BytesIO(payload)

        hasher = StreamHasher()
        h, size = hasher.hash_stream(stream, rewind_after=False)
        assert stream.tell() == len(payload)

    def test_hash_file_on_disk(self) -> None:
        payload = b"Sample document stored on filesystem disk."
        expected_hash = hashlib.sha256(payload).hexdigest()

        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(payload)
            temp_path = Path(f.name)

        try:
            calculated_hash, size = compute_file_sha256(temp_path, chunk_size=16)
            assert calculated_hash == expected_hash
            assert size == len(payload)
        finally:
            temp_path.unlink(missing_ok=True)

    def test_hash_file_nonexistent_raises(self) -> None:
        missing_path = Path("/tmp/nonexistent_document_file_12345.bin")
        with pytest.raises(ValidationError, match="File not found"):
            compute_file_sha256(missing_path)

    def test_empty_content_rejection(self) -> None:
        hasher = StreamHasher()

        # Empty bytes
        with pytest.raises(EmptyFileError, match="Cannot compute hash on empty"):
            hasher.hash_bytes(b"", reject_empty=True)

        # Empty stream
        with pytest.raises(EmptyFileError, match="Cannot compute hash on empty"):
            hasher.hash_stream(io.BytesIO(b""), reject_empty=True)

    def test_polymorphic_hash_content_dispatcher(self) -> None:
        payload = b"Polymorphic payload testing."
        expected_hash = hashlib.sha256(payload).hexdigest()

        # 1. From bytes
        h1, s1 = compute_content_sha256(payload)
        assert h1 == expected_hash

        # 2. From stream
        h2, s2 = compute_content_sha256(io.BytesIO(payload))
        assert h2 == expected_hash

        # 3. From temp file Path
        with tempfile.NamedTemporaryFile("wb", delete=False) as tf:
            tf.write(payload)
            t_path = Path(tf.name)
        try:
            h3, s3 = compute_content_sha256(t_path)
            assert h3 == expected_hash
        finally:
            t_path.unlink(missing_ok=True)

    def test_hash_calculation_independent_from_parsing(self) -> None:
        # Any binary sequence (even random or non-document bytes) is hashable
        random_bytes = bytes([i % 256 for i in range(2048)])
        expected = hashlib.sha256(random_bytes).hexdigest()

        h, size = compute_content_sha256(random_bytes)
        assert h == expected
        assert size == 2048


# ===========================================================================
# 2. EXACT DUPLICATE DETECTION & TENANT ISOLATION
# ===========================================================================

class TestDocumentDeduplicationLogic:
    """Verify deduplication checks, policies, tenant isolation, and soft delete."""

    def test_no_duplicate_returns_proceed(self) -> None:
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            deduplicator = DocumentDeduplicator(repository=repo)

            context = make_test_context()
            payload = b"Unique document content that has never been ingested before."

            result = await deduplicator.deduplicate_content(
                content=payload,
                filename="unique_doc.pdf",
                context=context,
            )

            assert result.is_duplicate is False
            assert result.action == DuplicateAction.PROCEED
            assert result.similarity_score == 0.0
            assert result.content_hash == hashlib.sha256(payload).hexdigest()
            assert result.file_size_bytes == len(payload)
            assert result.existing_document_id is None

        asyncio.run(_run())

    def test_duplicate_return_existing_policy(self) -> None:
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            deduplicator = DocumentDeduplicator(repository=repo)

            context = make_test_context()
            payload = b"Standard Employee Code of Conduct 2026."
            content_hash = hashlib.sha256(payload).hexdigest()

            # Pre-seed initial document in the repository
            initial_doc = await repo.register_document(
                DocumentCreateInput(
                    tenant_id=context.tenant_id,
                    original_filename="Employee_Code_of_Conduct.pdf",
                    normalized_filename="employee_code_of_conduct.pdf",
                    storage_uri="s3://docs/tenant1/doc1/v1/code.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                    classification="INTERNAL",
                )
            )

            # Check duplicate with default policy (RETURN_EXISTING)
            result = await deduplicator.deduplicate_content(
                content=payload,
                filename="Employee_Code_of_Conduct_Copy.pdf",
                context=context,
                policy=DuplicateHandlingPolicy.RETURN_EXISTING,
            )

            assert result.is_duplicate is True
            assert result.action == DuplicateAction.RETURN_EXISTING
            assert result.similarity_score == 1.0
            assert result.existing_document_id == initial_doc.document_id
            assert result.existing_document is not None
            assert result.existing_document.original_filename == "Employee_Code_of_Conduct.pdf"
            assert result.existing_document.tenant_id == context.tenant_id

        asyncio.run(_run())

    def test_duplicate_reject_policy_raises(self) -> None:
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            deduplicator = DocumentDeduplicator(repository=repo)

            context = make_test_context(policy=DuplicateHandlingPolicy.REJECT)
            payload = b"Proprietary algorithm specification document."
            content_hash = hashlib.sha256(payload).hexdigest()

            # Seed document
            await repo.register_document(
                DocumentCreateInput(
                    tenant_id=context.tenant_id,
                    original_filename="algo_spec.docx",
                    normalized_filename="algo_spec.docx",
                    storage_uri="s3://docs/algo.docx",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )
            )

            # Reject policy with raise_on_duplicate=True raises DuplicateDocumentError
            with pytest.raises(DuplicateDocumentError) as exc_info:
                await deduplicator.deduplicate_content(
                    content=payload,
                    filename="duplicate_algo.docx",
                    context=context,
                    policy=DuplicateHandlingPolicy.REJECT,
                    raise_on_duplicate=True,
                )

            assert exc_info.value.tenant_id == context.tenant_id
            assert exc_info.value.content_hash == content_hash

            # Reject policy with raise_on_duplicate=False returns result with REJECTED action
            res = await deduplicator.deduplicate_content(
                content=payload,
                filename="duplicate_algo.docx",
                context=context,
                policy=DuplicateHandlingPolicy.REJECT,
                raise_on_duplicate=False,
            )
            assert res.is_duplicate is True
            assert res.action == DuplicateAction.REJECTED

        asyncio.run(_run())

    def test_duplicate_new_version_policy(self) -> None:
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            deduplicator = DocumentDeduplicator(repository=repo)

            context = make_test_context(policy=DuplicateHandlingPolicy.NEW_VERSION)
            payload = b"Travel Expense Policy 2026 Revision."
            content_hash = hashlib.sha256(payload).hexdigest()

            # Seed document v1
            doc1 = await repo.register_document(
                DocumentCreateInput(
                    tenant_id=context.tenant_id,
                    original_filename="Travel_Policy_v1.pdf",
                    normalized_filename="travel_policy_v1.pdf",
                    storage_uri="s3://docs/travel_v1.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )
            )

            # Execute check with NEW_VERSION policy
            res = await deduplicator.deduplicate_content(
                content=payload,
                filename="Travel_Policy_v2.pdf",
                context=context,
                policy=DuplicateHandlingPolicy.NEW_VERSION,
            )

            assert res.is_duplicate is True
            assert res.action == DuplicateAction.NEW_VERSION_CREATED
            assert res.existing_document is not None
            assert res.existing_document.document_id == doc1.document_id
            assert res.existing_document.version_number == 1  # Incremented to 1

        asyncio.run(_run())

    def test_multi_tenant_isolation_identical_hash_permitted(self) -> None:
        """Requirement 8: Do not treat identical files uploaded by different tenants as shared."""
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            deduplicator = DocumentDeduplicator(repository=repo)

            tenant_a = "tenant_enterprise_alpha"
            tenant_b = "tenant_enterprise_beta"

            payload = b"Universal Public Compliance Standard ISO-27001."
            content_hash = hashlib.sha256(payload).hexdigest()

            # Tenant A uploads file
            doc_a = await repo.register_document(
                DocumentCreateInput(
                    tenant_id=tenant_a,
                    original_filename="ISO27001_Alpha.pdf",
                    normalized_filename="iso27001_alpha.pdf",
                    storage_uri="s3://tenant-a/iso.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )
            )

            # Tenant B uploads the EXACT SAME FILE
            context_b = make_test_context(tenant_id=tenant_b)
            result_b = await deduplicator.deduplicate_content(
                content=payload,
                filename="ISO27001_Beta.pdf",
                context=context_b,
            )

            # Tenant B must NOT see Tenant A's document as duplicate
            assert result_b.is_duplicate is False
            assert result_b.action == DuplicateAction.PROCEED
            assert result_b.existing_document_id is None

            # Tenant B registers its own document
            doc_b = await repo.register_document(
                DocumentCreateInput(
                    tenant_id=tenant_b,
                    original_filename="ISO27001_Beta.pdf",
                    normalized_filename="iso27001_beta.pdf",
                    storage_uri="s3://tenant-b/iso.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )
            )

            assert doc_a.document_id != doc_b.document_id
            assert doc_a.tenant_id == tenant_a
            assert doc_b.tenant_id == tenant_b
            assert doc_a.content_hash == doc_b.content_hash

            # Tenant A duplicate check sees doc_a, not doc_b
            context_a = make_test_context(tenant_id=tenant_a)
            res_a = await deduplicator.deduplicate_content(
                content=payload,
                filename="ISO27001_Alpha_Again.pdf",
                context=context_a,
            )
            assert res_a.is_duplicate is True
            assert res_a.existing_document_id == doc_a.document_id

        asyncio.run(_run())

    def test_soft_deletion_aware_deduplication(self) -> None:
        """Requirement 4: Soft-deleted document allows re-uploading identical file content."""
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            deduplicator = DocumentDeduplicator(repository=repo)

            context = make_test_context()
            payload = b"Retirement Pension Benefits Plan 2026."
            content_hash = hashlib.sha256(payload).hexdigest()

            # Step 1: Ingest active document
            doc = await repo.register_document(
                DocumentCreateInput(
                    tenant_id=context.tenant_id,
                    original_filename="pension_plan.pdf",
                    normalized_filename="pension_plan.pdf",
                    storage_uri="s3://docs/pension.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )
            )

            # Step 2: Duplicate check reports duplicate
            res1 = await deduplicator.deduplicate_content(
                content=payload,
                filename="pension_plan.pdf",
                context=context,
            )
            assert res1.is_duplicate is True
            assert res1.existing_document_id == doc.document_id

            # Step 3: Soft-delete original document
            deleted = await repo.soft_delete_document(context.tenant_id, doc.document_id)
            assert deleted is True

            # Step 4: Re-check deduplication -> Must be FALSE (allowed to re-upload)
            res2 = await deduplicator.deduplicate_content(
                content=payload,
                filename="pension_plan_reupload.pdf",
                context=context,
            )
            assert res2.is_duplicate is False
            assert res2.action == DuplicateAction.PROCEED

            # Step 5: Re-uploading identical content creates new active document record
            new_doc = await repo.register_document(
                DocumentCreateInput(
                    tenant_id=context.tenant_id,
                    original_filename="pension_plan_reupload.pdf",
                    normalized_filename="pension_plan_reupload.pdf",
                    storage_uri="s3://docs/pension_reupload.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )
            )
            assert new_doc.document_id != doc.document_id

            # Step 6: Third duplicate check collides with new active document
            res3 = await deduplicator.deduplicate_content(
                content=payload,
                filename="pension_plan_reupload.pdf",
                context=context,
            )
            assert res3.is_duplicate is True
            assert res3.existing_document_id == new_doc.document_id

        asyncio.run(_run())


# ===========================================================================
# 3. RACE-SAFETY & CONCURRENCY
# ===========================================================================

class TestConcurrentUploadRaceSafety:
    """Requirement 5 & 10: Prevent concurrent uploads from creating duplicate active records."""

    def test_concurrent_identical_uploads_yield_single_record(self) -> None:
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            deduplicator = DocumentDeduplicator(repository=repo)

            tenant_id = f"tenant_concurrent_{uuid.uuid4().hex[:6]}"
            payload = b"Concurrent upload benchmark test payload bytes."
            content_hash = hashlib.sha256(payload).hexdigest()

            # Simulate 20 concurrent upload requests for the same file in the same tenant
            async def upload_worker(worker_id: int):
                ctx = make_test_context(tenant_id=tenant_id)
                res = await deduplicator.deduplicate_content(
                    content=payload,
                    filename=f"concurrent_{worker_id}.pdf",
                    context=ctx,
                    policy=DuplicateHandlingPolicy.RETURN_EXISTING,
                )
                if not res.is_duplicate:
                    doc_input = DocumentCreateInput(
                        tenant_id=tenant_id,
                        original_filename=f"concurrent_{worker_id}.pdf",
                        normalized_filename=f"concurrent_{worker_id}.pdf",
                        storage_uri=f"s3://concurrent/{worker_id}.pdf",
                        file_size_bytes=len(payload),
                        content_hash=content_hash,
                    )
                    summary, is_new = await deduplicator.register_document_safely(doc_input)
                    return summary.document_id
                return res.existing_document_id

            # Run 20 concurrent coroutines simultaneously
            results = await asyncio.gather(*(upload_worker(i) for i in range(20)))

            # All 20 workers must have resolved to the EXACT SAME document ID
            unique_doc_ids = set(results)
            assert len(unique_doc_ids) == 1, f"Expected 1 unique document, got {unique_doc_ids}"

            # In-memory repository must contain exactly 1 active document
            count = await repo.count_active_documents(tenant_id)
            assert count == 1

        asyncio.run(_run())


# ===========================================================================
# 4. SUBSYSTEM STRATEGY INTERFACE COMPLIANCE
# ===========================================================================

class TestSha256ExactDeduplicationStrategyCompliance:
    """Verify Sha256ExactDeduplicationStrategy conforms to DeduplicationStrategy contract."""

    def test_strategy_implements_interface(self) -> None:
        async def _run() -> None:
            repo = InMemoryDocumentRepository()
            strategy: DeduplicationStrategy = Sha256ExactDeduplicationStrategy(repository=repo)

            assert strategy.name == "Sha256ExactDeduplication"

            context = make_test_context()
            payload = b"Subsystem strategy compliance test payload."
            h, _ = compute_bytes_sha256(payload)

            # Check document duplicate via interface
            res = await strategy.check_document_duplicate(file_hash_sha256=h, context=context)
            assert isinstance(res, DeduplicationResult)
            assert res.is_duplicate is False

            # Pre-seed document
            await repo.register_document(
                DocumentCreateInput(
                    tenant_id=context.tenant_id,
                    original_filename="seeded.pdf",
                    normalized_filename="seeded.pdf",
                    storage_uri="s3://seeded.pdf",
                    file_size_bytes=len(payload),
                    content_hash=h,
                )
            )

            res2 = await strategy.check_document_duplicate(file_hash_sha256=h, context=context)
            assert res2.is_duplicate is True
            assert res2.action == DuplicateAction.RETURN_EXISTING

            # Filter chunks passes through
            chunks = []
            filtered = await strategy.filter_duplicate_chunks(chunks, context)
            assert filtered == chunks

        asyncio.run(_run())
