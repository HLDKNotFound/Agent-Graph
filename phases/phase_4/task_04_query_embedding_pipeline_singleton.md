# Task 04: Query Embedding Pipeline with Singleton Model Lifecycle – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 25, 2026  
**Environment**: Conda `ml-env` (Python 3.12, PyTorch 2.5+, Sentence-Transformers v3.4+, NumPy)  
**Phase**: Phase 4 – Permission-Aware Hybrid Search & Cross-Encoder Reranking  
**Subsystem**: [`backend/retrieval/dense`](file:///home/mercury/AgentGraph/backend/retrieval/dense)

---

## 1. Executive Summary & Objective

The objective of **Task 04** was to implement the high-throughput, low-latency **Online Query Embedding Service** (`QueryEmbedder`). The service transforms incoming raw/rewritten user queries into 1024-dimensional normalized dense vectors for Stage-1 semantic search against Qdrant. Crucially, the service reuses the shared `BgeM3ModelManager` singleton established in Phase 2, eliminating duplicate model weight allocations in VRAM/system RAM (~2.2 GB footprint savings).

### Strict Scope Boundaries Adhered To
1. **Singleton Model Lifecycle Integration**: Seamlessly shares cached `SentenceTransformer` weights with the document ingestion pipeline, maintaining double-checked locking and device resolution.
2. **Strict Vector Standards**:
   - Fixed dimension: 1024 float elements.
   - Exact L2 normalization: Unit Euclidean length ($\|v\|_2 = 1.0$) for cosine similarity calculations.
3. **Inference Optimizations**: Guarded by `torch.inference_mode()` with auto-device routing (`cuda` with fp16 acceleration when available, graceful fallback to `cpu`).
4. **Asynchronous Non-Blocking Execution**: Exposes `async def embed_query()` and `async def embed_batch()` via thread pools to guarantee zero event loop blocking.
5. **Zero Hardcoding Principle**: Resolves embedding model identifier (`BAAI/bge-m3`) and compute device dynamically from `RetrievalSettings`.

---

## 2. Directory Structure & Subsystem Organization

```
backend/retrieval/dense/
├── __init__.py                               # Package exports (QdrantDenseRetriever, QueryEmbedder)
├── qdrant_search.py                          # Vector similarity search with RBAC
└── query_embedder.py                         # Online dense query embedding service
```

---

## 3. Core Implementation Details

### 3.1. QueryEmbedder Architecture
Located in [`backend/retrieval/dense/query_embedder.py`](file:///home/mercury/AgentGraph/backend/retrieval/dense/query_embedder.py):
```python
class QueryEmbedder:
    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        model_manager: BgeM3ModelManager | None = None,
        model: Any | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.model_manager = model_manager or BgeM3ModelManager.get_instance()
        self._model = model
        self.device = resolve_device(self.settings.embedding_device)

    def embed_query_sync(self, text: str) -> list[float]:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Query text cannot be empty or purely whitespace")

        model = self._get_model()
        with torch.inference_mode():
            embedding = model.encode(
                clean_text,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        ...
        return [float(x) for x in embedding.tolist()]
```

### 3.2. Batch Support
- `embed_batch_sync(texts)` and `embed_batch(texts)` support multi-query embedding generation in a single model forward pass, enabling efficient multi-hop sub-query processing in Phase 5.

---

## 4. Verification & Unit Tests

Test Suite: [`tests/retrieval/test_query_embedder.py`](file:///home/mercury/AgentGraph/tests/retrieval/test_query_embedder.py)

```
tests/retrieval/test_query_embedder.py ......                            [100%]
============================== 6 passed in 23.50s ==============================
```

### Key Verification Scenarios Passed:
1. **Singleton Manager Consistency**: Verified `BgeM3ModelManager.get_instance()` returns the identical cached instance across invocations.
2. **Dimension & L2 Normalization**: Confirmed all embeddings strictly match 1024 dimensions and Euclidean norm $\sum x_i^2 = 1.0 \pm 10^{-5}$.
3. **Async Thread-Pool Execution**: Validated `await embed_query()` returns identical normalized representations asynchronously.
4. **Batch Encoding Integrity**: Confirmed batch processing produces consistent arrays of normalized vectors.
5. **Input Validation**: Verified empty or whitespace-only queries trigger explicit `ValueError`.
6. **Exception Trapping**: Verified model inference exceptions wrap cleanly into typed `DenseRetrievalError` exceptions.
