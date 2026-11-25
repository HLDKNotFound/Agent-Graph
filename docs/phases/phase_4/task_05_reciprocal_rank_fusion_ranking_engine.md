# Task 05: Reciprocal Rank Fusion (RRF) Ranking Engine – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 25, 2026  
**Environment**: Conda `ml-env` (Python 3.12, NumPy, Pydantic v2)  
**Phase**: Phase 4 – Permission-Aware Hybrid Search & Cross-Encoder Reranking  
**Subsystem**: [`backend/retrieval/fusion`](file:///home/mercury/AgentGraph/backend/retrieval/fusion)

---

## 1. Executive Summary & Objective

The objective of **Task 05** was to build the production **Reciprocal Rank Fusion (RRF) Ranking Engine** (`ReciprocalRankFusionEngine`). In Stage 2 of the hybrid retrieval lifecycle, this engine merges dense semantic candidate passages from Qdrant and sparse lexical BM25 candidate passages from PostgreSQL into a unified, deduplicated list ordered by combined reciprocal rank scores.

### Strict Scope Boundaries Adhered To
1. **Spec-Compliant Mathematical Formulation**:
   $$RRF\_Score(d) = \sum_{m \in \{Dense, Sparse\}} \frac{w_m}{k + rank_m(d)}$$
   Configured with standard enterprise defaults: smoothing constant $k = 60$, dense weight $w_{Dense} = 0.7$, and sparse weight $w_{Sparse} = 0.3$.
2. **Chunk Deduplication & Multi-Source Identification**: Passages appearing in both candidate pools have their reciprocal rank scores summed and their source tag promoted to `RetrievedChunkSource.FUSED`.
3. **Lineage Coordinate Preservation**: Merges metadata and locator coordinates (`page_number`, `section_heading`, `sheet_name`, `row_range`) without data loss, ensuring subsequent citation mapping has complete provenance.
4. **Deterministic Tie-Breaking**: Breaks identical RRF scores by dense cosine similarity score followed by stable chunk UUID sorting.
5. **Candidate Ceiling Truncation**: Truncates fused output to top $K_1 = 40$ passages ready for Stage 3 Cross-Encoder reranking.

---

## 2. Directory Structure & Subsystem Organization

```
backend/retrieval/fusion/
├── __init__.py                               # Fusion package exports
└── rrf.py                                    # ReciprocalRankFusionEngine implementation
```

---

## 3. Core Implementation Details

### 3.1. RRF Fusion Algorithm
Located in [`backend/retrieval/fusion/rrf.py`](file:///home/mercury/AgentGraph/backend/retrieval/fusion/rrf.py):
```python
class ReciprocalRankFusionEngine:
    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        k: int | None = None,
        dense_weight: float | None = None,
        sparse_weight: float | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.k = k if k is not None else self.settings.rrf_k
        self.dense_weight = dense_weight if dense_weight is not None else self.settings.rrf_dense_weight
        self.sparse_weight = sparse_weight if sparse_weight is not None else self.settings.rrf_sparse_weight

    def fuse(
        self,
        dense_candidates: Sequence[RetrievedChunk],
        sparse_candidates: Sequence[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        ...
```

### 3.2. Deduplication & Score Accumulation
- Iterates through dense candidates, recording score contribution $w_{Dense} / (k + rank_{Dense})$.
- Iterates through sparse candidates, accumulating score contribution $w_{Sparse} / (k + rank_{Sparse})$ when matching `chunk_id` is found.
- Locators and metadata are merged safely using `_merge_locators()`.

---

## 4. Verification & Unit Tests

Test Suite: [`tests/retrieval/test_rrf_fusion.py`](file:///home/mercury/AgentGraph/tests/retrieval/test_rrf_fusion.py)

```
tests/retrieval/test_rrf_fusion.py ....                                  [100%]
============================== 4 passed in 0.11s ===============================
```

### Key Verification Scenarios Passed:
1. **Mathematical Accuracy**: Validated fused scores against manual analytical calculations:
   - Chunk 1 (Dense #1, Sparse #2): $0.7/61 + 0.3/62 = 0.0163141$ ($\pm 10^{-6}$).
   - Chunk 2 (Dense #2 only): $0.7/62 = 0.0112903$.
   - Chunk 3 (Sparse #1 only): $0.3/61 = 0.0049180$.
2. **Provenance Preservation**: Confirmed that when Dense provides page number and Sparse provides section heading, the fused chunk preserves both coordinates.
3. **Candidate Truncation**: Validated input list of 50 candidates is correctly truncated to top 15 when requested.
4. **Boundary Cases**: Confirmed empty dense, empty sparse, and mutually empty lists are handled cleanly with zero errors.
