"""Unit tests for Task 06: Cross-Encoder Reranker with Resilient Fallback."""

import asyncio
import time
import uuid
import pytest

from backend.retrieval.config import RetrievalSettings
from backend.retrieval.reranking.cross_encoder import (
    CrossEncoderModelManager,
    CrossEncoderReranker,
    sigmoid,
)
from backend.retrieval.types import (
    RetrievedChunk,
    RetrievedChunkSource,
)


class MockCrossEncoder:
    """Mock CrossEncoder producing predetermined logits or delays."""

    def __init__(
        self,
        scores_map: dict[str, float] | None = None,
        delay_seconds: float = 0.0,
        should_fail: bool = False,
    ) -> None:
        self.scores_map = scores_map or {}
        self.delay_seconds = delay_seconds
        self.should_fail = should_fail

    def predict(self, sentence_pairs: list[tuple[str, str]], **kwargs) -> list[float]:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        if self.should_fail:
            raise RuntimeError("CUDA Out of Memory during cross-encoder forward pass")

        results = []
        for query, text in sentence_pairs:
            # Match score from map or default based on length
            score = self.scores_map.get(text, 1.0)
            results.append(score)
        return results


def _make_candidate(content: str, rrf_score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        content=content,
        source=RetrievedChunkSource.FUSED,
        rrf_score=rrf_score,
    )


class TestSigmoidFunction:
    """Validate sigmoid mathematical properties."""

    def test_sigmoid_values(self) -> None:
        assert sigmoid(0.0) == 0.5
        assert pytest.approx(sigmoid(2.0), abs=1e-4) == 0.8808
        assert pytest.approx(sigmoid(-2.0), abs=1e-4) == 0.1192
        assert sigmoid(50.0) == 1.0
        assert sigmoid(-50.0) == 0.0


class TestCrossEncoderReranker:
    """Validate cross-encoder reranking, threshold filtering, and resilient fallback."""

    def test_singleton_model_manager(self) -> None:
        mgr1 = CrossEncoderModelManager.get_instance()
        mgr2 = CrossEncoderModelManager.get_instance()
        assert mgr1 is mgr2

    def test_successful_rerank_and_threshold_filtering(self) -> None:
        c1 = _make_candidate("Highly relevant passage", rrf_score=0.015)
        c2 = _make_candidate("Moderately relevant passage", rrf_score=0.012)
        c3 = _make_candidate("Irrelevant passage", rrf_score=0.018)

        mock_scores = {
            "Highly relevant passage": 3.0,       # sigmoid(3.0) ~ 0.9526
            "Moderately relevant passage": 0.0,   # sigmoid(0.0) = 0.5000
            "Irrelevant passage": -2.5,           # sigmoid(-2.5) ~ 0.0758 (filtered by 0.35)
        }
        mock_model = MockCrossEncoder(scores_map=mock_scores)
        reranker = CrossEncoderReranker(
            settings=RetrievalSettings(reranker_min_score_threshold=0.35, final_top_k=5),
            model=mock_model,
        )

        results = reranker.rerank_sync("relevant query", [c1, c2, c3])

        # c3 should be filtered out (< 0.35 threshold)
        assert len(results) == 2
        # c1 should be #1 despite c3 having higher initial RRF score
        assert results[0].content == "Highly relevant passage"
        assert results[0].rerank_score is not None
        assert results[0].rerank_score > 0.90

        # c2 should be #2
        assert results[1].content == "Moderately relevant passage"
        assert pytest.approx(results[1].rerank_score, abs=1e-3) == 0.50

    def test_resilient_fallback_on_timeout(self) -> None:
        async def _run() -> None:
            c1 = _make_candidate("Doc 1", rrf_score=0.020)
            c2 = _make_candidate("Doc 2", rrf_score=0.015)
            c3 = _make_candidate("Doc 3", rrf_score=0.010)

            # Delay 0.3s when timeout is configured to 0.1s
            mock_model = MockCrossEncoder(delay_seconds=0.3)
            reranker = CrossEncoderReranker(
                settings=RetrievalSettings(reranker_timeout_seconds=0.1, final_top_k=2),
                model=mock_model,
            )

            # Must NOT throw exception; must fall back to RRF rank order
            results = await reranker.rerank("test query", [c2, c1, c3])
            assert len(results) == 2
            # Should be ordered by RRF score: c1 (0.020), then c2 (0.015)
            assert results[0].content == "Doc 1"
            assert results[1].content == "Doc 2"

        asyncio.run(_run())

    def test_resilient_fallback_on_inference_error(self) -> None:
        async def _run() -> None:
            c1 = _make_candidate("Doc A", rrf_score=0.025)
            c2 = _make_candidate("Doc B", rrf_score=0.018)

            mock_model = MockCrossEncoder(should_fail=True)
            reranker = CrossEncoderReranker(
                settings=RetrievalSettings(final_top_k=2),
                model=mock_model,
            )

            # Must NOT throw exception; must fall back to RRF order
            results = await reranker.rerank("test query", [c2, c1])
            assert len(results) == 2
            assert results[0].content == "Doc A"
            assert results[1].content == "Doc B"

        asyncio.run(_run())

    def test_empty_candidates_returns_empty(self) -> None:
        reranker = CrossEncoderReranker()
        assert reranker.rerank_sync("query", []) == []
