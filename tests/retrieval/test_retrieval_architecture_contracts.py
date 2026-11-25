"""Unit tests for Task 01: Retrieval Architecture & Zero-Trust Security Contracts."""

import uuid
import pytest
from pydantic import ValidationError

from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.exceptions import (
    DenseRetrievalError,
    EmptyRetrievalResultWarning,
    RerankerInferenceError,
    RerankerTimeoutError,
    RetrievalError,
    RetrievalSecurityViolationError,
    SparseRetrievalError,
)
from backend.retrieval.types import (
    ChunkLocatorCoordinates,
    RetrievalContext,
    RetrievedChunk,
    RetrievedChunkSource,
)


class TestRetrievalSettings:
    """Validate dynamic configuration and zero hardcoding principles."""

    def test_default_settings(self) -> None:
        settings = get_retrieval_settings()
        assert settings.dense_top_k == 40
        assert settings.sparse_top_k == 40
        assert settings.fused_top_k == 40
        assert settings.final_top_k == 8
        assert settings.rrf_k == 60
        assert settings.rrf_dense_weight == 0.7
        assert settings.rrf_sparse_weight == 0.3
        assert settings.reranker_min_score_threshold == 0.35
        assert settings.reranker_timeout_seconds == 1.5
        assert settings.qdrant_hnsw_ef == 64

    def test_custom_settings_override(self) -> None:
        custom = RetrievalSettings(
            dense_top_k=50,
            sparse_top_k=30,
            rrf_dense_weight=0.8,
            rrf_sparse_weight=0.2,
            reranker_timeout_seconds=2.0,
        )
        assert custom.dense_top_k == 50
        assert custom.sparse_top_k == 30
        assert custom.rrf_dense_weight == 0.8
        assert custom.rrf_sparse_weight == 0.2
        assert custom.reranker_timeout_seconds == 2.0

    def test_invalid_settings_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalSettings(dense_top_k=-1)
        with pytest.raises(ValidationError):
            RetrievalSettings(rrf_dense_weight=1.5)


class TestRetrievalContext:
    """Validate zero-trust multi-tenant security boundary."""

    def test_context_creation_and_immutability(self) -> None:
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        ctx = RetrievalContext(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=["ENGINEERING_LEAD", "EMPLOYEE"],
            departments=["ENGINEERING"],
            classifications=["PUBLIC", "INTERNAL", "CONFIDENTIAL"],
        )
        assert ctx.tenant_id == tenant_id
        assert ctx.user_id == user_id
        assert ctx.has_role("ENGINEERING_LEAD")
        assert ctx.has_role("employee")  # case-insensitive check
        assert not ctx.has_role("CEO")
        assert ctx.belongs_to_department("ENGINEERING")
        assert not ctx.belongs_to_department("HR")

        # Immutability
        with pytest.raises(ValidationError):
            ctx.roles = ["ADMIN"]

    def test_classification_clearance(self) -> None:
        ctx = RetrievalContext(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            classifications=["PUBLIC", "INTERNAL"],
        )
        assert ctx.can_access_classification("PUBLIC")
        assert ctx.can_access_classification("INTERNAL")
        assert not ctx.can_access_classification("CONFIDENTIAL")
        assert not ctx.can_access_classification("RESTRICTED")


class TestRetrievedChunkModel:
    """Validate candidate passage and locator coordinate contracts."""

    def test_retrieved_chunk_creation(self) -> None:
        chunk_id = uuid.uuid4()
        doc_id = uuid.uuid4()
        locator = ChunkLocatorCoordinates(
            page_number=3,
            section_heading="2.1 Architecture Overview",
            char_start=120,
            char_end=450,
        )
        chunk = RetrievedChunk(
            chunk_id=chunk_id,
            document_id=doc_id,
            content="This is the architecture section.",
            token_count=15,
            locator=locator,
            dense_score=0.88,
            dense_rank=1,
            source=RetrievedChunkSource.DENSE,
            classification="INTERNAL",
        )
        assert chunk.chunk_id == chunk_id
        assert chunk.document_id == doc_id
        assert chunk.dense_score == 0.88
        assert chunk.locator.page_number == 3
        assert chunk.locator.section_heading == "2.1 Architecture Overview"
        assert chunk.source == RetrievedChunkSource.DENSE

        # Immutability check
        with pytest.raises(ValidationError):
            chunk.dense_score = 0.99

    def test_tabular_locator_coordinates(self) -> None:
        locator = ChunkLocatorCoordinates(
            sheet_name="Q3_Salaries",
            row_range="Rows 14-25",
        )
        assert locator.sheet_name == "Q3_Salaries"
        assert locator.row_range == "Rows 14-25"
        assert locator.page_number is None


class TestDomainExceptions:
    """Validate typed exception hierarchy."""

    def test_exception_inheritance(self) -> None:
        sec_err = RetrievalSecurityViolationError("Unauthorized cross-tenant access", {"tenant": "test"})
        assert isinstance(sec_err, RetrievalError)
        assert sec_err.details == {"tenant": "test"}

        dense_err = DenseRetrievalError("Qdrant node unreachable")
        assert isinstance(dense_err, RetrievalError)

        sparse_err = SparseRetrievalError("PostgreSQL GIN index missing")
        assert isinstance(sparse_err, RetrievalError)

        rerank_timeout = RerankerTimeoutError("Cross encoder timeout exceeded 1.5s")
        assert isinstance(rerank_timeout, RetrievalError)

        rerank_inf = RerankerInferenceError("CUDA OOM during rerank")
        assert isinstance(rerank_inf, RetrievalError)
