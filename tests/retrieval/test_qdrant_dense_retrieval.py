"""Unit and integration tests for Task 02: Qdrant Dense Vector Retrieval with RBAC."""

import uuid
import pytest
from qdrant_client import QdrantClient, models

from backend.retrieval.config import RetrievalSettings
from backend.retrieval.dense.qdrant_search import QdrantDenseRetriever
from backend.retrieval.exceptions import (
    DenseRetrievalError,
    RetrievalSecurityViolationError,
)
from backend.retrieval.types import (
    RetrievalContext,
    RetrievedChunkSource,
)


@pytest.fixture
def in_memory_qdrant_client() -> QdrantClient:
    """Provide isolated in-memory Qdrant instance with pre-configured collection."""
    client = QdrantClient(":memory:")
    collection_name = "test_chunks"
    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=4,  # Small vector dimension for lightweight testing
            distance=models.Distance.COSINE,
        ),
    )
    return client


class TestQdrantDenseRetrieverFilterBuilding:
    """Validate zero-trust Stage-1 RBAC filter construction."""

    def test_filter_structure(self) -> None:
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        ctx = RetrievalContext(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=["HR_SPECIALIST", "EMPLOYEE"],
            departments=["HR"],
            classifications=["PUBLIC", "INTERNAL"],
        )

        retriever = QdrantDenseRetriever(
            settings=RetrievalSettings(qdrant_url=":memory:", qdrant_collection="test_chunks"),
        )
        q_filter = retriever.build_rbac_filter(ctx)

        # Check tenant isolation is in 'must'
        assert len(q_filter.must) == 2
        tenant_cond = q_filter.must[0]
        assert tenant_cond.key == "tenant_id"
        assert tenant_cond.match.value == str(tenant_id)

        # Check RBAC barrier is in 'must[1].should'
        rbac_barrier = q_filter.must[1]
        assert hasattr(rbac_barrier, "should")
        should_keys = [c.key for c in rbac_barrier.should if hasattr(c, "key")]
        assert "classification" in should_keys
        assert "allowed_roles" in should_keys
        assert "allowed_departments" in should_keys
        assert "allowed_users" in should_keys


class TestQdrantDenseRetrievalExecution:
    """Validate retrieval execution, tenant isolation, and RBAC enforcement against in-memory Qdrant."""

    def test_tenant_isolation_and_rbac_filtering(
        self,
        in_memory_qdrant_client: QdrantClient,
    ) -> None:
        async def _run() -> None:
            collection_name = "test_chunks"
            settings = RetrievalSettings(
                qdrant_url=":memory:",
                qdrant_collection=collection_name,
                dense_top_k=10,
            )
            retriever = QdrantDenseRetriever(settings=settings, client=in_memory_qdrant_client)

            tenant_a = uuid.uuid4()
            tenant_b = uuid.uuid4()
            hr_user = uuid.uuid4()
            sales_user = uuid.uuid4()

            # Insert Point 1: Tenant A, Public
            p1_id = str(uuid.uuid4())
            in_memory_qdrant_client.upsert(
                collection_name=collection_name,
                points=[
                    models.PointStruct(
                        id=p1_id,
                        vector=[1.0, 0.0, 0.0, 0.0],
                        payload={
                            "chunk_id": p1_id,
                            "document_id": str(uuid.uuid4()),
                            "tenant_id": str(tenant_a),
                            "content": "Company holidays policy (Public)",
                            "classification": "PUBLIC",
                            "allowed_roles": ["EMPLOYEE"],
                            "allowed_departments": ["ALL"],
                            "page_number": 1,
                            "section_heading": "1. Holidays",
                        },
                    ),
                    # Insert Point 2: Tenant A, Confidential HR (Restricted to HR_MANAGER)
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=[0.9, 0.1, 0.0, 0.0],
                        payload={
                            "chunk_id": str(uuid.uuid4()),
                            "document_id": str(uuid.uuid4()),
                            "tenant_id": str(tenant_a),
                            "content": "Executive compensation formula (Confidential)",
                            "classification": "CONFIDENTIAL",
                            "allowed_roles": ["HR_MANAGER"],
                            "allowed_departments": ["HR"],
                            "page_number": 5,
                            "section_heading": "5. Exec Salaries",
                        },
                    ),
                    # Insert Point 3: Tenant B, Public (Should never leak to Tenant A)
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=[1.0, 0.0, 0.0, 0.0],
                        payload={
                            "chunk_id": str(uuid.uuid4()),
                            "document_id": str(uuid.uuid4()),
                            "tenant_id": str(tenant_b),
                            "content": "Tenant B public memo",
                            "classification": "PUBLIC",
                            "allowed_roles": ["EMPLOYEE"],
                            "allowed_departments": ["ALL"],
                        },
                    ),
                ],
            )

            # Query vector: close to [1.0, 0.0, 0.0, 0.0]
            query_vec = [1.0, 0.0, 0.0, 0.0]

            # Scenario 1: Sales user in Tenant A (has EMPLOYEE role, cannot access CONFIDENTIAL HR)
            sales_ctx = RetrievalContext(
                tenant_id=tenant_a,
                user_id=sales_user,
                roles=["EMPLOYEE"],
                departments=["SALES"],
                classifications=["PUBLIC", "INTERNAL"],
            )
            sales_results = await retriever.search(query_vec, sales_ctx)
            assert len(sales_results) == 1
            assert sales_results[0].content == "Company holidays policy (Public)"
            assert sales_results[0].source == RetrievedChunkSource.DENSE
            assert sales_results[0].dense_rank == 1
            assert sales_results[0].locator.page_number == 1
            assert sales_results[0].locator.section_heading == "1. Holidays"

            # Scenario 2: HR Manager in Tenant A (can access CONFIDENTIAL HR)
            hr_ctx = RetrievalContext(
                tenant_id=tenant_a,
                user_id=hr_user,
                roles=["HR_MANAGER", "EMPLOYEE"],
                departments=["HR"],
                classifications=["PUBLIC", "INTERNAL", "CONFIDENTIAL"],
            )
            hr_results = await retriever.search(query_vec, hr_ctx)
            assert len(hr_results) == 2
            # Tenant B doc is excluded for both
            for r in hr_results:
                assert "Tenant B" not in r.content

        import asyncio
        asyncio.run(_run())

    def test_error_handling_on_qdrant_failure(self) -> None:
        # Mock client that raises
        class FailingClient:
            def search(self, *args, **kwargs):
                raise ConnectionError("Qdrant node disconnected")

        retriever = QdrantDenseRetriever(
            settings=RetrievalSettings(qdrant_url=":memory:"),
            client=FailingClient(),  # type: ignore
        )
        ctx = RetrievalContext(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        with pytest.raises(DenseRetrievalError) as exc_info:
            retriever.search_sync([0.1, 0.2, 0.3, 0.4], ctx)
        assert "Qdrant dense vector search failed" in str(exc_info.value)
