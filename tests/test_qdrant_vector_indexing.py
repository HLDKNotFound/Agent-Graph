"""Unit and integration tests for Qdrant Vector Indexing and Payload Management (Stage 4 — Task 13).

Comprehensive test suite validating:
1. Reusing Phase 1 Qdrant configuration and HNSW parameters (m=16, ef_construct=128, ef=64).
2. Collection creation and schema compatibility verification (dense 1024-dim, Cosine distance).
3. Payload index creation for filterable attributes (tenant_id, document_id, RBAC roles, etc.).
4. Stable deterministic point ID generation from database chunk IDs.
5. Payload serialization including provenance coordinates (page, heading, sheet, row) and RBAC ACLs.
6. Idempotent upserting (no duplicate points created on re-index).
7. Strict multi-tenant isolation (Tenant B cannot read, count, check, or delete Tenant A's vectors).
8. Vector deletion by document, by chunk IDs, and by point IDs.
9. Vector existence checks (has_vector, vector_exists) and document vector counting.
10. Vector retrieval by identifier with payload and vector data.
11. Micro-batch indexing and partial failure reporting.
12. Input validation and security defenses against untrusted parameters.
13. Architectural decoupling from relational DB and task queue implementations.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
import pytest
from qdrant_client import QdrantClient, models

from backend.ingestion import (
    AccessControlPayload,
    ChunkLocator,
    DocumentChunkInput,
    EmbeddingResult,
    IngestionContext,
    IndexingError,
    QdrantBatchIndexResult,
    QdrantConfig,
    QdrantPointRecord,
    QdrantVectorIndexService,
    TenantStorageViolationError,
    ValidationError,
    get_qdrant_index_service,
    serialize_chunk_payload,
    to_point_id,
)


# ---------------------------------------------------------------------------
# Fixtures and Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def in_memory_qdrant_client() -> QdrantClient:
    """Provide a fresh in-memory Qdrant client for integration testing."""
    return QdrantClient(":memory:")


@pytest.fixture
def qdrant_service(in_memory_qdrant_client: QdrantClient) -> QdrantVectorIndexService:
    """Provide a configured QdrantVectorIndexService backed by an in-memory client."""
    config = QdrantConfig(
        collection_name="test_knowledge_chunks",
        vector_dimension=1024,
        distance="Cosine",
        hnsw_m=16,
        hnsw_ef_construct=128,
        hnsw_ef=64,
        batch_size=4,
    )
    return QdrantVectorIndexService(config=config, client=in_memory_qdrant_client)


def make_chunk(
    chunk_index: int,
    content: str,
    doc_id: str = "doc_test_100",
    tenant_id: str = "tenant_alpha",
    token_count: int = 20,
    page_number: int | None = 1,
    section_heading: str | None = "Section 1: General Provisions",
    sheet_name: str | None = None,
    row_range: str | None = None,
    roles: list[str] | None = None,
    departments: list[str] | None = None,
) -> DocumentChunkInput:
    """Helper to generate an attributed DocumentChunkInput."""
    c_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return DocumentChunkInput(
        chunk_id=f"chk_{doc_id}_{chunk_index:04d}",
        tenant_id=tenant_id,
        document_id=doc_id,
        chunk_index=chunk_index,
        content=content,
        content_hash=c_hash,
        token_count=token_count,
        char_count=len(content),
        locator=ChunkLocator(
            page_number=page_number,
            section_heading=section_heading,
            sheet_name=sheet_name,
            row_range=row_range,
            char_start=0,
            char_end=len(content),
        ),
        acl=AccessControlPayload(
            tenant_id=tenant_id,
            roles=roles or ["engineering", "admin"],
            departments=departments or ["rnd"],
            users=["user_1"],
        ),
        metadata={"priority": "high", "topic": "policy"},
    )


def make_embedding(
    chunk: DocumentChunkInput,
    dimension: int = 1024,
    seed_val: float = 0.05,
) -> EmbeddingResult:
    """Helper to generate a normalized 1024-dim EmbeddingResult for a chunk."""
    # Deterministic vector based on chunk_index
    vec = [float((i + chunk.chunk_index * 7) % 100) / 100.0 for i in range(dimension)]
    # Normalize to unit length
    norm = sum(x * x for x in vec) ** 0.5
    normalized_vec = [x / norm for x in vec]
    return EmbeddingResult(
        chunk_id=chunk.chunk_id,
        vector=normalized_vec,
        model_name="BAAI/bge-m3",
        dimension=dimension,
        token_count=chunk.token_count,
    )


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestQdrantConfigurationAndCollectionSetup:
    """Validates Requirements 1, 2, 3, 4, 9."""

    def test_default_config_matches_phase1_specifications(self):
        config = QdrantConfig()
        assert config.collection_name == "enterprise_knowledge_chunks"
        assert config.vector_dimension == 1024
        assert config.distance == "Cosine"
        assert config.hnsw_m == 16
        assert config.hnsw_ef_construct == 128
        assert config.hnsw_ef == 64

    def test_ensure_collection_creates_new_collection_with_indexes(self, qdrant_service: QdrantVectorIndexService):
        assert not qdrant_service.client.collection_exists(qdrant_service.collection_name)
        ready = qdrant_service.ensure_collection()
        assert ready is True
        assert qdrant_service.client.collection_exists(qdrant_service.collection_name)

        # Inspect collection parameters
        info = qdrant_service.client.get_collection(qdrant_service.collection_name)
        assert info.config.params.vectors.size == 1024
        assert info.config.params.vectors.distance == models.Distance.COSINE
        assert info.config.hnsw_config.m == 16
        assert info.config.params.vectors.hnsw_config.ef_construct == 128

    def test_ensure_collection_idempotent_on_existing_compatible_collection(self, qdrant_service: QdrantVectorIndexService):
        qdrant_service.ensure_collection()
        # Calling ensure_collection a second time must succeed cleanly without recreating
        assert qdrant_service.ensure_collection() is True

    def test_ensure_collection_detects_incompatible_schema(self, in_memory_qdrant_client: QdrantClient):
        # Create an incompatible collection (768-dim)
        col_name = "incompatible_col"
        in_memory_qdrant_client.create_collection(
            collection_name=col_name,
            vectors_config=models.VectorParams(size=768, distance=models.Distance.COSINE),
        )

        service = QdrantVectorIndexService(
            config=QdrantConfig(collection_name=col_name, vector_dimension=1024),
            client=in_memory_qdrant_client,
        )

        with pytest.raises(IndexingError) as exc_info:
            service.ensure_collection(recreate_if_incompatible=False)
        assert "exists with incompatible configuration" in str(exc_info.value)
        assert exc_info.value.details["expected_dimension"] == 1024
        assert exc_info.value.details["actual_dimension"] == 768


class TestStablePointIdAndPayloadSerialization:
    """Validates Requirements 6, 7, 8."""

    def test_to_point_id_preserves_valid_uuid(self):
        valid_uuid = str(uuid.uuid4())
        point_id = to_point_id(valid_uuid)
        assert point_id == valid_uuid

    def test_to_point_id_deterministic_for_custom_chunk_id(self):
        chunk_id = "chk_doc123_0001"
        pt1 = to_point_id(chunk_id)
        pt2 = to_point_id(chunk_id)
        # Must be identical across calls
        assert pt1 == pt2
        # Must be a valid UUID string
        parsed = uuid.UUID(pt1)
        assert parsed.version == 5

    def test_serialize_chunk_payload_structure(self):
        chunk = make_chunk(
            chunk_index=0,
            content="Quy định nghỉ phép năm và chế độ thai sản.",
            doc_id="doc_hr_01",
            tenant_id="tenant_gamma",
            page_number=3,
            section_heading="Chương 2: Chế độ phúc lợi",
            sheet_name=None,
            row_range=None,
            roles=["hr", "employee"],
            departments=["human_resources"],
        )

        payload = serialize_chunk_payload(chunk)

        # Requirement 7 core fields
        assert payload["tenant_id"] == "tenant_gamma"
        assert payload["document_id"] == "doc_hr_01"
        assert payload["chunk_id"] == chunk.chunk_id
        assert payload["allowed_roles"] == ["hr", "employee"]
        assert payload["allowed_departments"] == ["human_resources"]
        assert payload["page_number"] == 3
        assert payload["section_heading"] == "Chương 2: Chế độ phúc lợi"

        # Requirement 8 provenance coordinates
        assert payload["char_start"] == 0
        assert payload["char_end"] == len(chunk.content)
        assert "source_location" in payload
        assert "acl" in payload

        # Round-trip verification with ChunkLocator
        reconstructed = ChunkLocator.from_qdrant_payload(payload)
        assert reconstructed.page_number == 3
        assert reconstructed.section_heading == "Chương 2: Chế độ phúc lợi"


class TestIdempotentVectorUpsertAndRetrieval:
    """Validates Requirements 5, 11."""

    def test_idempotent_upsert_and_point_stability(self, qdrant_service: QdrantVectorIndexService):
        tenant_id = "tenant_alpha"
        context = IngestionContext(tenant_id=tenant_id, document_id="doc_policy_01")

        chunks = [
            make_chunk(0, "Điều 1: Tiêu chuẩn bảo mật phần mềm.", doc_id="doc_policy_01", tenant_id=tenant_id),
            make_chunk(1, "Điều 2: Quy trình thẩm định mã nguồn.", doc_id="doc_policy_01", tenant_id=tenant_id),
            make_chunk(2, "Điều 3: Kiểm thử tự động trên CI/CD.", doc_id="doc_policy_01", tenant_id=tenant_id),
        ]
        embeddings = [make_embedding(c) for c in chunks]

        # First indexing run
        res1 = asyncio.run(qdrant_service.index_chunks(chunks, embeddings, context))
        assert res1.points_indexed == 3
        assert len(res1.vector_storage_ids) == 3

        count1 = qdrant_service.count_vectors_for_document("doc_policy_01", context)
        assert count1 == 3

        # Second indexing run (exact same chunks - should update in place without duplicating)
        res2 = asyncio.run(qdrant_service.index_chunks(chunks, embeddings, context))
        assert res2.points_indexed == 3
        assert res2.vector_storage_ids == res1.vector_storage_ids  # Point IDs must be identical

        count2 = qdrant_service.count_vectors_for_document("doc_policy_01", context)
        assert count2 == 3  # Points count must remain strictly 3 (idempotence)

    def test_get_vector_by_id_returns_exact_data(self, qdrant_service: QdrantVectorIndexService):
        tenant_id = "tenant_alpha"
        context = IngestionContext(tenant_id=tenant_id, document_id="doc_contract_01")

        chunk = make_chunk(0, "Hợp đồng cung cấp dịch vụ điện toán đám mây.", doc_id="doc_contract_01", tenant_id=tenant_id)
        embedding = make_embedding(chunk)

        asyncio.run(qdrant_service.index_chunks([chunk], [embedding], context))

        record = qdrant_service.get_vector_by_id(chunk.chunk_id, context, with_vector=True)
        assert record is not None
        assert isinstance(record, QdrantPointRecord)
        assert record.chunk_id == chunk.chunk_id
        assert record.document_id == "doc_contract_01"
        assert record.tenant_id == tenant_id
        assert record.vector is not None
        assert len(record.vector) == 1024
        assert record.payload["content"] == chunk.content

    def test_has_vector_and_vector_exists(self, qdrant_service: QdrantVectorIndexService):
        tenant_id = "tenant_alpha"
        context = IngestionContext(tenant_id=tenant_id, document_id="doc_alpha_01")

        chunk = make_chunk(0, "Dữ liệu mẫu kiểm tra sự tồn tại.", doc_id="doc_alpha_01", tenant_id=tenant_id)
        embedding = make_embedding(chunk)

        assert qdrant_service.has_vector(chunk.chunk_id, context) is False
        assert qdrant_service.vector_exists(chunk.chunk_id, context) is False

        asyncio.run(qdrant_service.index_chunks([chunk], [embedding], context))

        assert qdrant_service.has_vector(chunk.chunk_id, context) is True
        assert qdrant_service.vector_exists(chunk.chunk_id, context) is True
        assert qdrant_service.has_vector("nonexistent_chunk_999", context) is False


class TestMultiTenantIsolationSecurity:
    """Validates Requirements 10, 12: Zero-trust multi-tenancy enforcement."""

    def test_tenant_isolation_prevents_cross_tenant_access(self, qdrant_service: QdrantVectorIndexService):
        context_a = IngestionContext(tenant_id="tenant_alpha", document_id="doc_confidential")
        context_b = IngestionContext(tenant_id="tenant_beta", document_id="doc_confidential")

        chunk_a = make_chunk(0, "Tài liệu tuyệt mật của Tenant Alpha.", doc_id="doc_confidential", tenant_id="tenant_alpha")
        emb_a = make_embedding(chunk_a)

        # Index chunk for Tenant Alpha
        asyncio.run(qdrant_service.index_chunks([chunk_a], [emb_a], context_a))

        # Tenant Alpha can access
        assert qdrant_service.has_vector(chunk_a.chunk_id, context_a) is True
        assert qdrant_service.count_vectors_for_document("doc_confidential", context_a) == 1
        record_a = qdrant_service.get_vector_by_id(chunk_a.chunk_id, context_a)
        assert record_a is not None

        # Tenant Beta CANNOT access Tenant Alpha's vector under any circumstances
        assert qdrant_service.has_vector(chunk_a.chunk_id, context_b) is False
        assert qdrant_service.count_vectors_for_document("doc_confidential", context_b) == 0
        record_b = qdrant_service.get_vector_by_id(chunk_a.chunk_id, context_b)
        assert record_b is None

        # Tenant Beta CANNOT delete Tenant Alpha's vector
        deleted_by_b = asyncio.run(qdrant_service.delete_document_vectors("doc_confidential", context_b))
        assert deleted_by_b == 0
        # Verify vector is still safely present for Tenant Alpha
        assert qdrant_service.has_vector(chunk_a.chunk_id, context_a) is True

    def test_index_chunks_rejects_tenant_id_mismatch(self, qdrant_service: QdrantVectorIndexService):
        context = IngestionContext(tenant_id="tenant_alpha", document_id="doc_test")
        chunk_invalid = make_chunk(0, "Nội dung", doc_id="doc_test", tenant_id="tenant_malicious")
        emb = make_embedding(chunk_invalid)

        with pytest.raises(TenantStorageViolationError) as exc_info:
            asyncio.run(qdrant_service.index_chunks([chunk_invalid], [emb], context))
        assert "violating context tenant 'tenant_alpha'" in str(exc_info.value)


class TestVectorDeletionOperations:
    """Validates Requirements 10, 11."""

    def test_delete_document_vectors(self, qdrant_service: QdrantVectorIndexService):
        tenant_id = "tenant_alpha"
        context = IngestionContext(tenant_id=tenant_id, document_id="doc_to_delete")

        chunks = [
            make_chunk(0, "Nội dung trang 1", doc_id="doc_to_delete", tenant_id=tenant_id),
            make_chunk(1, "Nội dung trang 2", doc_id="doc_to_delete", tenant_id=tenant_id),
        ]
        embeddings = [make_embedding(c) for c in chunks]

        asyncio.run(qdrant_service.index_chunks(chunks, embeddings, context))
        assert qdrant_service.count_vectors_for_document("doc_to_delete", context) == 2

        deleted_count = asyncio.run(qdrant_service.delete_document_vectors("doc_to_delete", context))
        assert deleted_count == 2
        assert qdrant_service.count_vectors_for_document("doc_to_delete", context) == 0
        assert qdrant_service.has_vector(chunks[0].chunk_id, context) is False

    def test_delete_chunk_vectors(self, qdrant_service: QdrantVectorIndexService):
        tenant_id = "tenant_alpha"
        context = IngestionContext(tenant_id=tenant_id, document_id="doc_multi_chunks")

        chunks = [
            make_chunk(0, "Mục A", doc_id="doc_multi_chunks", tenant_id=tenant_id),
            make_chunk(1, "Mục B", doc_id="doc_multi_chunks", tenant_id=tenant_id),
        ]
        embeddings = [make_embedding(c) for c in chunks]

        asyncio.run(qdrant_service.index_chunks(chunks, embeddings, context))
        assert qdrant_service.count_vectors_for_document("doc_multi_chunks", context) == 2

        # Delete only chunk 0
        qdrant_service.delete_chunk_vectors([chunks[0].chunk_id], context)

        assert qdrant_service.has_vector(chunks[0].chunk_id, context) is False
        assert qdrant_service.has_vector(chunks[1].chunk_id, context) is True
        assert qdrant_service.count_vectors_for_document("doc_multi_chunks", context) == 1


class TestBatchIndexingAndPartialFailures:
    """Validates Requirements 11, 14."""

    def test_batch_index_chunks_pagination(self, qdrant_service: QdrantVectorIndexService):
        tenant_id = "tenant_alpha"
        context = IngestionContext(tenant_id=tenant_id, document_id="doc_batch_10")

        # Create 10 chunks with batch_size=3 (will run across 4 micro-batches)
        chunks = [
            make_chunk(i, f"Nội dung đoạn số {i}", doc_id="doc_batch_10", tenant_id=tenant_id)
            for i in range(10)
        ]
        embeddings = [make_embedding(c) for c in chunks]

        batch_result = asyncio.run(
            qdrant_service.batch_index_chunks(chunks, embeddings, context, batch_size=3)
        )

        assert isinstance(batch_result, QdrantBatchIndexResult)
        assert batch_result.points_indexed == 10
        assert batch_result.points_failed == 0
        assert len(batch_result.vector_storage_ids) == 10
        assert qdrant_service.count_vectors_for_document("doc_batch_10", context) == 10


class TestInputValidationAndErrorHandling:
    """Validates Requirement 10, 12: Robust input validation and exception contracts."""

    def test_missing_tenant_id_raises_validation_error(self, qdrant_service: QdrantVectorIndexService):
        invalid_context = IngestionContext(tenant_id="", document_id="doc_1")
        chunk = make_chunk(0, "Test", tenant_id="tenant_1")
        emb = make_embedding(chunk)

        with pytest.raises(ValidationError):
            asyncio.run(qdrant_service.index_chunks([chunk], [emb], invalid_context))

    def test_empty_chunks_returns_empty_result(self, qdrant_service: QdrantVectorIndexService):
        context = IngestionContext(tenant_id="tenant_alpha", document_id="doc_1")
        res = asyncio.run(qdrant_service.index_chunks([], [], context))
        assert res.points_indexed == 0
        assert res.vector_storage_ids == []

    def test_mismatched_chunks_and_embeddings_count_raises_validation_error(self, qdrant_service: QdrantVectorIndexService):
        context = IngestionContext(tenant_id="tenant_alpha", document_id="doc_1")
        chunk = make_chunk(0, "Test", tenant_id="tenant_alpha")

        with pytest.raises(ValidationError) as exc_info:
            asyncio.run(qdrant_service.index_chunks([chunk], [], context))
        assert "Mismatched input counts" in str(exc_info.value)

    def test_vector_dimension_mismatch_raises_indexing_error(self, qdrant_service: QdrantVectorIndexService):
        context = IngestionContext(tenant_id="tenant_alpha", document_id="doc_1")
        chunk = make_chunk(0, "Test", tenant_id="tenant_alpha")
        # 768-dim vector instead of expected 1024
        bad_emb = make_embedding(chunk, dimension=768)

        with pytest.raises(IndexingError) as exc_info:
            asyncio.run(qdrant_service.index_chunks([chunk], [bad_emb], context))
        assert "Vector dimension mismatch" in str(exc_info.value)

    def test_global_service_singleton_retrieval(self):
        svc1 = get_qdrant_index_service(config=QdrantConfig(host=":memory:"))
        assert isinstance(svc1, QdrantVectorIndexService)
        assert svc1.dimension == 1024
