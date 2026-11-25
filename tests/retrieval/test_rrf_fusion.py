"""Unit tests for Task 05: Reciprocal Rank Fusion (RRF) Ranking Engine."""

import uuid
import pytest

from backend.retrieval.config import RetrievalSettings
from backend.retrieval.fusion.rrf import ReciprocalRankFusionEngine
from backend.retrieval.types import (
    ChunkLocatorCoordinates,
    RetrievedChunk,
    RetrievedChunkSource,
)


def _make_chunk(
    chunk_id: uuid.UUID,
    content: str,
    source: RetrievedChunkSource,
    dense_score: float | None = None,
    sparse_score: float | None = None,
    dense_rank: int | None = None,
    sparse_rank: int | None = None,
    locator: ChunkLocatorCoordinates | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        content=content,
        source=source,
        dense_score=dense_score,
        sparse_score=sparse_score,
        dense_rank=dense_rank,
        sparse_rank=sparse_rank,
        locator=locator or ChunkLocatorCoordinates(),
    )


class TestReciprocalRankFusion:
    """Validate mathematical formulation, deduplication, and ranking of RRF."""

    def test_rrf_exact_mathematical_fusion(self) -> None:
        c1_id = uuid.uuid4()
        c2_id = uuid.uuid4()
        c3_id = uuid.uuid4()

        # Dense: c1 (rank 1), c2 (rank 2)
        dense_list = [
            _make_chunk(c1_id, "Chunk 1 Content", RetrievedChunkSource.DENSE, dense_score=0.92, dense_rank=1),
            _make_chunk(c2_id, "Chunk 2 Content", RetrievedChunkSource.DENSE, dense_score=0.85, dense_rank=2),
        ]

        # Sparse: c1 (rank 2), c3 (rank 1)
        sparse_list = [
            _make_chunk(c3_id, "Chunk 3 Content", RetrievedChunkSource.SPARSE, sparse_score=0.75, sparse_rank=1),
            _make_chunk(c1_id, "Chunk 1 Content", RetrievedChunkSource.SPARSE, sparse_score=0.60, sparse_rank=2),
        ]

        engine = ReciprocalRankFusionEngine(k=60, dense_weight=0.7, sparse_weight=0.3)
        fused = engine.fuse(dense_list, sparse_list)

        assert len(fused) == 3

        # Expected scores:
        # c1: (0.7 / (60 + 1)) + (0.3 / (60 + 2)) = 0.7/61 + 0.3/62 = 0.0114754 + 0.0048387 = 0.0163141
        # c2: 0.7 / (60 + 2) = 0.7/62 = 0.0112903
        # c3: 0.3 / (60 + 1) = 0.3/61 = 0.0049180
        expected_c1 = (0.7 / 61.0) + (0.3 / 62.0)
        expected_c2 = 0.7 / 62.0
        expected_c3 = 0.3 / 61.0

        # c1 should be #1 (present in both)
        assert fused[0].chunk_id == c1_id
        assert pytest.approx(fused[0].rrf_score, abs=1e-6) == expected_c1
        assert fused[0].source == RetrievedChunkSource.FUSED
        assert fused[0].dense_rank == 1
        assert fused[0].sparse_rank == 2

        # c2 should be #2 (dense rank 2 has weight 0.7, higher than sparse rank 1 with weight 0.3)
        assert fused[1].chunk_id == c2_id
        assert pytest.approx(fused[1].rrf_score, abs=1e-6) == expected_c2
        assert fused[1].source == RetrievedChunkSource.DENSE

        # c3 should be #3 (sparse rank 1 with weight 0.3)
        assert fused[2].chunk_id == c3_id
        assert pytest.approx(fused[2].rrf_score, abs=1e-6) == expected_c3
        assert fused[2].source == RetrievedChunkSource.SPARSE

    def test_locator_merging_upon_fusion(self) -> None:
        chunk_id = uuid.uuid4()
        d_chunk = _make_chunk(
            chunk_id=chunk_id,
            content="Content",
            source=RetrievedChunkSource.DENSE,
            dense_rank=1,
            locator=ChunkLocatorCoordinates(page_number=5),
        )
        s_chunk = _make_chunk(
            chunk_id=chunk_id,
            content="Content",
            source=RetrievedChunkSource.SPARSE,
            sparse_rank=1,
            locator=ChunkLocatorCoordinates(section_heading="Section 3: Benefits"),
        )

        engine = ReciprocalRankFusionEngine()
        fused = engine.fuse([d_chunk], [s_chunk])

        assert len(fused) == 1
        res = fused[0]
        assert res.source == RetrievedChunkSource.FUSED
        assert res.locator.page_number == 5
        assert res.locator.section_heading == "Section 3: Benefits"

    def test_candidate_truncation(self) -> None:
        dense_list = [
            _make_chunk(uuid.uuid4(), f"Dense {i}", RetrievedChunkSource.DENSE, dense_rank=i)
            for i in range(1, 25)
        ]
        sparse_list = [
            _make_chunk(uuid.uuid4(), f"Sparse {i}", RetrievedChunkSource.SPARSE, sparse_rank=i)
            for i in range(1, 25)
        ]

        engine = ReciprocalRankFusionEngine()
        # Limit to top 15
        fused = engine.fuse(dense_list, sparse_list, top_k=15)
        assert len(fused) == 15

    def test_empty_candidates_handling(self) -> None:
        engine = ReciprocalRankFusionEngine()

        # Both empty
        assert engine.fuse([], []) == []

        # Only dense
        c_id = uuid.uuid4()
        d_chunk = _make_chunk(c_id, "Only Dense", RetrievedChunkSource.DENSE, dense_rank=1)
        res_d = engine.fuse([d_chunk], [])
        assert len(res_d) == 1
        assert res_d[0].chunk_id == c_id
        assert res_d[0].source == RetrievedChunkSource.DENSE

        # Only sparse
        s_chunk = _make_chunk(c_id, "Only Sparse", RetrievedChunkSource.SPARSE, sparse_rank=1)
        res_s = engine.fuse([], [s_chunk])
        assert len(res_s) == 1
        assert res_s[0].chunk_id == c_id
        assert res_s[0].source == RetrievedChunkSource.SPARSE
