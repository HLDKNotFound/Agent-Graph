"""Unit tests for Task 04: Query Embedding Pipeline with Singleton Model Lifecycle."""

import asyncio
import math
import numpy as np
import pytest

from backend.ingestion.embedding.bge_m3 import BgeM3ModelManager
from backend.retrieval.config import RetrievalSettings
from backend.retrieval.dense.query_embedder import QueryEmbedder
from backend.retrieval.exceptions import DenseRetrievalError


class MockSentenceTransformer:
    """Mock SentenceTransformer generating deterministic 1024-dimensional normalized vectors."""

    def __init__(self, dim: int = 1024, should_fail: bool = False) -> None:
        self.dim = dim
        self.should_fail = should_fail
        self.max_seq_length = 8192

    def encode(
        self,
        sentences: str | list[str],
        convert_to_numpy: bool = True,
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
        batch_size: int = 1,
    ) -> np.ndarray:
        if self.should_fail:
            raise RuntimeError("CUDA Out of Memory during inference")

        if isinstance(sentences, str):
            # Generate deterministic vector based on text hash
            h = hash(sentences) % 10000 + 1
            raw = np.array([float((i + h) % 100) for i in range(self.dim)], dtype=np.float32)
            if normalize_embeddings:
                norm = np.linalg.norm(raw)
                if norm > 0:
                    raw = raw / norm
            return raw

        results = []
        for s in sentences:
            h = hash(s) % 10000 + 1
            raw = np.array([float((i + h) % 100) for i in range(self.dim)], dtype=np.float32)
            if normalize_embeddings:
                norm = np.linalg.norm(raw)
                if norm > 0:
                    raw = raw / norm
            results.append(raw)
        return np.array(results, dtype=np.float32)


class TestQueryEmbedder:
    """Validate query embedder behavior, normalization, and singleton lifecycle."""

    def test_singleton_model_manager(self) -> None:
        mgr1 = BgeM3ModelManager.get_instance()
        mgr2 = BgeM3ModelManager.get_instance()
        assert mgr1 is mgr2

    def test_embed_query_success_and_normalization(self) -> None:
        mock_model = MockSentenceTransformer(dim=1024)
        embedder = QueryEmbedder(model=mock_model)

        vector = embedder.embed_query_sync("Chính sách phúc lợi nhân viên")
        assert len(vector) == 1024
        assert all(isinstance(x, float) for x in vector)

        # Assert unit L2 normalization: sum(x^2) ~ 1.0
        l2_norm = math.sqrt(sum(x * x for x in vector))
        assert pytest.approx(l2_norm, abs=1e-5) == 1.0

    def test_async_embed_query(self) -> None:
        async def _run() -> None:
            mock_model = MockSentenceTransformer(dim=1024)
            embedder = QueryEmbedder(model=mock_model)

            vector = await embedder.embed_query("Quy chế tài chính 2026")
            assert len(vector) == 1024
            l2_norm = math.sqrt(sum(x * x for x in vector))
            assert pytest.approx(l2_norm, abs=1e-5) == 1.0

        asyncio.run(_run())

    def test_embed_batch(self) -> None:
        async def _run() -> None:
            mock_model = MockSentenceTransformer(dim=1024)
            embedder = QueryEmbedder(model=mock_model)

            queries = [
                "Chính sách bảo hiểm y tế",
                "Mức lương kỹ sư AI",
                "Thời gian thử việc",
            ]
            batch_vectors = await embedder.embed_batch(queries)
            assert len(batch_vectors) == 3
            for vec in batch_vectors:
                assert len(vec) == 1024
                l2_norm = math.sqrt(sum(x * x for x in vec))
                assert pytest.approx(l2_norm, abs=1e-5) == 1.0

        asyncio.run(_run())

    def test_empty_query_raises_value_error(self) -> None:
        mock_model = MockSentenceTransformer(dim=1024)
        embedder = QueryEmbedder(model=mock_model)

        with pytest.raises(ValueError):
            embedder.embed_query_sync("")

        with pytest.raises(ValueError):
            embedder.embed_query_sync("    ")

    def test_inference_error_wrapping(self) -> None:
        mock_model = MockSentenceTransformer(dim=1024, should_fail=True)
        embedder = QueryEmbedder(model=mock_model)

        with pytest.raises(DenseRetrievalError) as exc_info:
            embedder.embed_query_sync("Test query causing OOM")
        assert "Failed to generate dense query embedding" in str(exc_info.value)
