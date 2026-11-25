# Task 02: Dense Semantic Vector Search with Qdrant RBAC Pre-Filtering – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 25, 2026  
**Environment**: Conda `ml-env` (Python 3.12, Qdrant Client v1.13+, PyTorch 2.5+, PostgreSQL 16+)  
**Phase**: Phase 4 – Permission-Aware Hybrid Search & Cross-Encoder Reranking  
**Subsystem**: [`backend/retrieval/dense`](file:///home/mercury/AgentGraph/backend/retrieval/dense)

---

## 1. Executive Summary & Objective

The objective of **Task 02** was to deliver the production-grade **Dense Semantic Vector Search Engine** (`QdrantDenseRetriever`) backed by Qdrant (v1.8+). The service executes high-speed dense vector similarity queries over 1024-dimensional normalized embeddings while enforcing zero-trust multi-tenant isolation and Stage-1 Role-Based Access Control (RBAC) pre-filtering at the HNSW exploration stage.

### Strict Scope Boundaries Adhered To
1. **Stage-1 Zero-Trust Payload Pre-Filtering**: Security checks are injected directly into Qdrant search expressions rather than filtered post-retrieval in memory. This eliminates unauthorized vector exploration and guarantees zero token or content leakage.
2. **Dual-Condition Security Gate**:
   - `must`: Hard tenancy isolation `tenant_id == context.tenant_id`.
   - `must.should` (RBAC Barrier): Universal `PUBLIC` clearance, user classification clearance levels (e.g. `INTERNAL`, `CONFIDENTIAL`), active RBAC roles (`allowed_roles` / `acl.roles`), department containment (`allowed_departments` / `acl.departments`), and direct user assignments (`allowed_users`).
3. **Dual API Compatibility**: Supports modern `query_points` (Qdrant v1.10+) with seamless fallback to `search`.
4. **Line-Level Locator Preservation**: Reconstructs physical coordinates (`page_number`, `section_heading`, `sheet_name`, `row_range`, character offsets) into `RetrievedChunk` instances.
5. **Decoupled Asynchronous API**: Exposes non-blocking `async def search()` using thread pool execution (`asyncio.to_thread`) to prevent blocking the FastAPI/LangGraph event loop.

---

## 2. Directory Structure & Subsystem Organization

```
backend/retrieval/dense/
├── __init__.py                               # Dense retrieval package exports
└── qdrant_search.py                          # QdrantDenseRetriever with RBAC pre-filtering
```

---

## 3. Core Implementation Details

### 3.1. Stage-1 Zero-Trust Boolean Filter Architecture
Located in [`backend/retrieval/dense/qdrant_search.py`](file:///home/mercury/AgentGraph/backend/retrieval/dense/qdrant_search.py):
```python
def build_rbac_filter(self, context: RetrievalContext) -> models.Filter:
    tenant_condition = models.FieldCondition(
        key="tenant_id",
        match=models.MatchValue(value=str(context.tenant_id)),
    )
    acl_should_clauses: list[models.Condition] = [
        models.FieldCondition(key="classification", match=models.MatchValue(value="PUBLIC")),
        models.FieldCondition(key="acl.classification", match=models.MatchValue(value="PUBLIC")),
    ]
    if context.classifications:
        acl_should_clauses.extend([...])
    if context.roles:
        acl_should_clauses.extend([...])
    if context.departments:
        acl_should_clauses.extend([...])
    if context.user_id:
        acl_should_clauses.append(models.FieldCondition(key="allowed_users", match=models.MatchValue(value=str(context.user_id))))

    rbac_barrier = models.Filter(should=acl_should_clauses)
    return models.Filter(must=[tenant_condition, rbac_barrier])
```

### 3.2. Passage Mapping & Metric Extraction
- HNSW runtime exploration: `search_params = models.SearchParams(hnsw_ef=64, exact=False)`.
- Candidate limit: $K_1 = 40$ (configurable via `RetrievalSettings.dense_top_k`).
- Attribution: Formulates `RetrievedChunk` with `source = RetrievedChunkSource.DENSE`, ordinal `dense_rank` ($1 \dots K_1$), and cosine similarity `dense_score`.

---

## 4. Verification & Unit Tests

Test Suite: [`tests/retrieval/test_qdrant_dense_retrieval.py`](file:///home/mercury/AgentGraph/tests/retrieval/test_qdrant_dense_retrieval.py)

```
tests/retrieval/test_qdrant_dense_retrieval.py ...                       [100%]
========================= 3 passed, 1 warning in 1.77s =========================
```

### Key Verification Scenarios Passed:
1. **Filter Structure Verification**: Validated boolean filter AST guaranteeing tenant isolation in `must` and RBAC clauses in `must[1].should`.
2. **Tenant Isolation Test**: Confirmed Tenant B documents are 100% blocked from Tenant A queries even when identical vectors and queries are executed.
3. **Role & Clearance Gate Test**: Verified Sales user only retrieves Public policy, while HR Manager retrieves both Public policy and Confidential HR compensation records.
4. **Network Resilience Test**: Verified client failure raises `DenseRetrievalError` with error details captured for telemetry.
