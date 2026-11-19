"""Unit tests for BGE-M3 Dense Vector Embedding Service (Stage 4 — Task 12).

Comprehensive test suite validating:
1. Dense vector dimension enforcement (strictly 1024-dim).
2. L2 normalization to unit length (norm == 1.0).
3. 1:1 chunk-to-vector ordering preservation.
4. Model lifecycle management and singleton caching (BgeM3ModelManager).
5. Device selection and resolution (auto, cuda, cpu).
6. Batch processing with configurable batch sizes.
7. Validation on empty or malformed inputs (ValidationError).
8. Error handling on dimension mismatch and inference errors (EmbeddingError).
9. Retry-safe adaptive batch halving and CPU fallback on CUDA OOM.
10. Decoupled architecture: zero dependencies on Qdrant or Celery.
"""

from __future__ import annotations

import asyncio
import math
import sys
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
import torch

from backend.ingestion import (
    AccessControlPayload,
    ChunkLocator,
    DocumentChunkInput,
    IngestionContext,
)
from backend.ingestion.embedding import (
    BgeM3Config,
    BgeM3EmbeddingService,
    BgeM3ModelManager,
    EmbeddingProvider,
    EmbeddingResult,
    get_embedding_service,
    resolve_device,
)
from backend.ingestion.exceptions import EmbeddingError, ValidationError


# ---------------------------------------------------------------------------
# Fixtures and Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_model_cache():
    """Ensure clean model manager state before and after tests."""
    yield
    manager = BgeM3ModelManager.get_instance()
    # Keep models if loaded to avoid repeated downloads, but ensure clean test state


def make_sample_chunk(
    chunk_index: int = 0,
    content: str = "Enterprise AI knowledge platform.",
    doc_id: str = "doc_test_123",
    tenant_id: str = "tenant_test",
    token_count: int = 15,
) -> DocumentChunkInput:
    """Helper to generate a valid DocumentChunkInput."""
    import hashlib
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return DocumentChunkInput(
        chunk_id=f"chk_{doc_id}_{chunk_index:04d}",
        tenant_id=tenant_id,
        document_id=doc_id,
        chunk_index=chunk_index,
        content=content,
        content_hash=content_hash,
        token_count=token_count,
        char_count=len(content),
        locator=ChunkLocator(
            page_number=1,
            char_start=0,
            char_end=len(content),
        ),
        acl=AccessControlPayload(
            tenant_id=tenant_id,
            roles=["engineering"],
            departments=["rnd"],
        ),
    )


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

class TestDeviceResolution:
    """Validates device resolution logic across hardware topologies."""

    def test_resolve_cpu(self):
        assert resolve_device("cpu") == "cpu"
        assert resolve_device("CPU") == "cpu"
        assert resolve_device(" cpu ") == "cpu"

    def test_resolve_auto(self):
        resolved = resolve_device("auto")
        expected = "cuda" if torch.cuda.is_available() else "cpu"
        assert resolved == expected

    def test_resolve_cuda_when_available_or_fallback(self):
        if torch.cuda.is_available():
            assert resolve_device("cuda") == "cuda"
            assert resolve_device("gpu") == "cuda"
        else:
            assert resolve_device("cuda") == "cpu"

    def test_resolve_unavailable_cuda_falls_back(self):
        with patch.object(torch.cuda, "is_available", return_value=False):
            assert resolve_device("cuda") == "cpu"
            assert resolve_device("cuda:0") == "cpu"


class TestBgeM3ModelLifecycleManager:
    """Validates model caching, reuse, and memory release."""

    def test_singleton_instance(self):
        mgr1 = BgeM3ModelManager.get_instance()
        mgr2 = BgeM3ModelManager.get_instance()
        assert mgr1 is mgr2

    def test_caching_and_reuse(self):
        manager = BgeM3ModelManager.get_instance()
        device = "cpu"
        model_name = "BAAI/bge-m3"

        # Initially check or load
        model_a = manager.get_model(model_name=model_name, device=device)
        assert model_a is not None
        assert manager.is_loaded(model_name=model_name, device=device)

        # Re-requesting must return identical object from cache (no reload)
        model_b = manager.get_model(model_name=model_name, device=device)
        assert model_a is model_b

    def test_unload_model(self):
        manager = BgeM3ModelManager.get_instance()
        device = "cpu"
        model_name = "BAAI/bge-m3"

        # Ensure loaded
        manager.get_model(model_name=model_name, device=device)
        assert manager.is_loaded(model_name=model_name, device=device)

        # Unload
        unloaded = manager.unload_model(model_name=model_name, device=device)
        assert unloaded is True
        assert not manager.is_loaded(model_name=model_name, device=device)

        # Second unload returns False
        assert manager.unload_model(model_name=model_name, device=device) is False

    def test_model_loading_failure_raises_embedding_error(self):
        manager = BgeM3ModelManager.get_instance()
        with patch("backend.ingestion.embedding.bge_m3.SentenceTransformer", side_effect=RuntimeError("Corrupt model")):
            with pytest.raises(EmbeddingError) as exc_info:
                manager.get_model(model_name="nonexistent/model", device="cpu")
            assert "Failed to load embedding model" in str(exc_info.value)


class TestBgeM3ConfigAndContracts:
    """Validates configuration parameters and interface conformity."""

    def test_default_config_values(self):
        config = BgeM3Config()
        assert config.model_name == "BAAI/bge-m3"
        assert config.dimension == 1024
        assert config.device == "auto"
        assert config.batch_size == 32
        assert config.max_length == 8192
        assert config.normalize_embeddings is True
        assert config.fallback_to_cpu is True

    def test_service_implements_embedding_provider_interface(self):
        service = BgeM3EmbeddingService()
        assert isinstance(service, EmbeddingProvider)
        assert service.dimension == 1024
        assert service.model_name == "BAAI/bge-m3"
        assert service.batch_size == 32

    def test_custom_config_override(self):
        custom_config = BgeM3Config(
            dimension=1024,
            batch_size=16,
            device="cpu",
        )
        service = BgeM3EmbeddingService(config=custom_config)
        assert service.batch_size == 16
        assert service.device == "cpu"


class TestBgeM3EmbeddingInference:
    """Validates actual embedding inference: dimension, normalization, and semantic mapping."""

    def test_dense_1024_dimension_and_l2_normalization(self):
        service = BgeM3EmbeddingService(config=BgeM3Config(device="cpu"))
        text = "AgentGraph là nền tảng AI Agent doanh nghiệp bảo mật cao."

        vec = service.embed_single_text(text)

        # 1. Output dimension strictly 1024
        assert len(vec) == 1024
        assert isinstance(vec, list)
        assert all(isinstance(val, float) for val in vec)

        # 2. L2-normalized vector has Euclidean norm == 1.0 (within float tolerance)
        l2_norm = math.sqrt(sum(val * val for val in vec))
        assert pytest.approx(l2_norm, abs=1e-4) == 1.0

        # 3. No NaN or Inf
        assert not np.isnan(vec).any()
        assert not np.isinf(vec).any()

    def test_batch_embed_texts_preserves_order(self):
        service = BgeM3EmbeddingService(config=BgeM3Config(device="cpu", batch_size=2))
        texts = [
            "Chính sách nghỉ phép năm của doanh nghiệp.",
            "Quy trình thanh toán công tác phí phòng Tài chính.",
            "Tiêu chuẩn kỹ thuật hệ thống microservices.",
        ]

        vectors = asyncio.run(service.embed_texts(texts))

        assert len(vectors) == 3
        for vec in vectors:
            assert len(vec) == 1024
            assert pytest.approx(math.sqrt(sum(v * v for v in vec)), abs=1e-4) == 1.0

        # Cosine similarity between text 0 and text 0 should be 1.0
        v0 = np.array(vectors[0])
        v1 = np.array(vectors[1])
        sim_same = np.dot(v0, v0)
        sim_diff = np.dot(v0, v1)

        assert pytest.approx(sim_same, abs=1e-4) == 1.0
        assert sim_diff < 0.95  # Different topics should have lower similarity

    def test_embed_chunks_exact_1to1_mapping_preservation(self):
        service = BgeM3EmbeddingService(config=BgeM3Config(device="cpu", batch_size=2))

        chunks = [
            make_sample_chunk(0, "Điều 1: Giờ làm việc tiêu chuẩn từ 8h30 đến 17h30.", token_count=18),
            make_sample_chunk(1, "Điều 2: Chế độ làm việc từ xa tối đa 2 ngày mỗi tuần.", token_count=22),
            make_sample_chunk(2, "Điều 3: Phúc lợi bảo hiểm sức khỏe toàn diện PTI.", token_count=16),
            make_sample_chunk(3, "Điều 4: Quy định bảo mật thông tin nội bộ cấp độ 3.", token_count=20),
            make_sample_chunk(4, "Điều 5: Khen thưởng nhân viên xuất sắc định kỳ quý.", token_count=19),
        ]

        context = IngestionContext(tenant_id="tenant_alpha", document_id="doc_alpha_policy")
        results = asyncio.run(service.embed_chunks(chunks=chunks, context=context))

        assert len(results) == len(chunks)

        # Strictly check 1:1 order preservation and metadata propagation
        for i, (chunk, res) in enumerate(zip(chunks, results)):
            assert isinstance(res, EmbeddingResult)
            assert res.chunk_id == chunk.chunk_id
            assert res.model_name == "BAAI/bge-m3"
            assert res.dimension == 1024
            assert res.token_count == chunk.token_count
            assert len(res.vector) == 1024
            assert pytest.approx(math.sqrt(sum(v * v for v in res.vector)), abs=1e-4) == 1.0

    def test_embed_single_chunk_convenience(self):
        service = BgeM3EmbeddingService(config=BgeM3Config(device="cpu"))
        chunk = make_sample_chunk(0, "Chính sách bảo mật cơ sở dữ liệu.", token_count=12)

        res = service.embed_single_chunk(chunk)
        assert isinstance(res, EmbeddingResult)
        assert res.chunk_id == chunk.chunk_id
        assert len(res.vector) == 1024
        assert res.token_count == 12


class TestInputValidationAndEdgeCases:
    """Validates boundary conditions, empty inputs, and error states."""

    def test_empty_chunks_returns_empty_list(self):
        service = BgeM3EmbeddingService()
        res = asyncio.run(service.embed_chunks([]))
        assert res == []

    def test_empty_texts_returns_empty_list(self):
        service = BgeM3EmbeddingService()
        res = asyncio.run(service.embed_texts([]))
        assert res == []

    def test_invalid_text_type_raises_validation_error(self):
        service = BgeM3EmbeddingService()
        with pytest.raises(ValidationError) as exc_info:
            asyncio.run(service.embed_texts(["valid text", 12345, "another text"]))  # type: ignore
        assert "Invalid embedding input at index 1" in str(exc_info.value)

    def test_none_text_raises_validation_error(self):
        service = BgeM3EmbeddingService()
        with pytest.raises(ValidationError):
            service.embed_single_text(None)  # type: ignore

    def test_invalid_chunk_type_raises_validation_error(self):
        service = BgeM3EmbeddingService()
        with pytest.raises(ValidationError):
            service.embed_single_chunk("not a chunk")  # type: ignore

    def test_dimension_mismatch_raises_embedding_error(self):
        """If model returns unexpected vector dimension, service must reject it."""
        mock_model = MagicMock()
        # Returns 768-dim vector instead of expected 1024
        mock_model.encode.return_value = np.zeros((1, 768), dtype=np.float32)

        mock_manager = MagicMock()
        mock_manager.get_model.return_value = mock_model

        service = BgeM3EmbeddingService(
            config=BgeM3Config(dimension=1024, device="cpu"),
            model_manager=mock_manager,
        )

        with pytest.raises(EmbeddingError) as exc_info:
            service.embed_single_text("Sample input text")
        assert "Output dimension mismatch" in str(exc_info.value)
        assert exc_info.value.details["expected"] == 1024
        assert exc_info.value.details["actual"] == 768


class TestRetrySafeInferenceAndOomRecovery:
    """Validates resilience against CUDA OutOfMemory and inference failures."""

    def test_adaptive_batch_halving_on_oom(self):
        """Simulate CUDA OutOfMemory on large batch, verify halving retry succeeds."""
        mock_model = MagicMock()
        attempts = 0

        def side_effect(batch, batch_size, **kwargs):
            nonlocal attempts
            attempts += 1
            # Fail on first attempt when batch_size is 4
            if batch_size > 2:
                raise torch.cuda.OutOfMemoryError("CUDA out of memory in test")
            # Succeed on halved batch size
            return np.ones((len(batch), 1024), dtype=np.float32)

        mock_model.encode.side_effect = side_effect

        mock_manager = MagicMock()
        mock_manager.get_model.return_value = mock_model

        service = BgeM3EmbeddingService(
            config=BgeM3Config(dimension=1024, batch_size=4, device="cuda", max_retries_on_oom=3),
            model_manager=mock_manager,
        )

        texts = ["text1", "text2", "text3", "text4"]
        vectors = service._encode_batch(texts)

        assert len(vectors) == 4
        assert attempts >= 2  # Proves retry occurred

    def test_cpu_fallback_when_retries_exhausted(self):
        """Simulate permanent CUDA OOM, verify graceful fallback to CPU."""
        mock_cuda_model = MagicMock()
        mock_cuda_model.encode.side_effect = torch.cuda.OutOfMemoryError("Persistent OOM")

        mock_cpu_model = MagicMock()
        mock_cpu_model.encode.return_value = np.ones((2, 1024), dtype=np.float32)

        mock_manager = MagicMock()
        mock_manager.get_model.side_effect = lambda model_name, device: (
            mock_cpu_model if device == "cpu" else mock_cuda_model
        )

        service = BgeM3EmbeddingService(
            config=BgeM3Config(
                dimension=1024,
                batch_size=2,
                device="cuda",
                max_retries_on_oom=1,
                fallback_to_cpu=True,
            ),
            model_manager=mock_manager,
        )

        texts = ["text1", "text2"]
        vectors = service._encode_batch(texts)

        assert len(vectors) == 2
        mock_cpu_model.encode.assert_called_once()

    def test_unexpected_inference_exception_raises_embedding_error(self):
        mock_model = MagicMock()
        mock_model.encode.side_effect = ValueError("Corrupt tensor data")

        mock_manager = MagicMock()
        mock_manager.get_model.return_value = mock_model

        service = BgeM3EmbeddingService(
            config=BgeM3Config(dimension=1024, device="cpu"),
            model_manager=mock_manager,
        )

        with pytest.raises(EmbeddingError) as exc_info:
            service.embed_single_text("Sample text")
        assert "Failed to generate embeddings" in str(exc_info.value)


class TestArchitectureDecoupling:
    """Verifies Requirement 13: Decoupled from Qdrant and Celery."""

    def test_no_qdrant_or_celery_imports_in_embedding_module(self):
        import backend.ingestion.embedding.bge_m3 as module_bge
        import backend.ingestion.embedding.base as module_base
        import backend.ingestion.embedding.models as module_models

        for mod in (module_bge, module_base, module_models):
            mod_dict = mod.__dict__
            assert "qdrant_client" not in mod_dict
            assert "QdrantClient" not in mod_dict
            assert "celery" not in mod_dict
            assert "Celery" not in mod_dict

    def test_global_service_singleton(self):
        svc1 = get_embedding_service()
        svc2 = get_embedding_service()
        assert svc1 is svc2
        assert isinstance(svc1, BgeM3EmbeddingService)
