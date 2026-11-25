# Task 01: Retrieval Architecture & Zero-Trust Security Contracts – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 25, 2026  
**Environment**: Conda `ml-env` (Python 3.12, Pydantic v2, PostgreSQL 16+, Qdrant v1.8+)  
**Phase**: Phase 4 – Permission-Aware Hybrid Search & Cross-Encoder Reranking  
**Subsystem**: [`backend/retrieval`](file:///home/mercury/AgentGraph/backend/retrieval)

---

## 1. Executive Summary & Objective

The objective of **Task 01** was to establish the architectural foundation, typed domain exceptions, dynamic configuration, and shared data contracts for the **Hybrid Search & Reranking Subsystem** (Phase 4). This subsystem acts as the retrieval backbone of the Enterprise Knowledge Agent, bridging the first-stage Intent Router Gate (Phase 3) with dense vector search in Qdrant, sparse lexical BM25 in PostgreSQL, Reciprocal Rank Fusion (RRF), and Cross-Encoder semantic reranking.

### Strict Scope Boundaries Adhered To
1. **Zero Premature Retrieval Logic**: Decoupled type contracts from heavy concrete search clients and model weights, reserving runtime engines for subsequent sequential tasks.
2. **Zero Hardcoding Principle**: Implemented `RetrievalSettings` (`pydantic_settings.BaseSettings`) to resolve all operational timeouts, top-K candidate limits ($K_1=40$, $K_2=8$), RRF fusion weights ($k=60$, $w_{Dense}=0.7$, $w_{Sparse}=0.3$), score thresholds ($\ge 0.35$), and database connection URIs dynamically from `.env`.
3. **Immutability Invariants**: Enforced strict immutability (`frozen=True`) on core data structures (`RetrievalContext`, `RetrievedChunk`, `ChunkLocatorCoordinates`).
4. **Three-Tier Security Integration**: Established `RetrievalContext` carrying tenant isolation markers, user identifiers, RBAC roles, department boundaries, and document classification clearance levels (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED).

---

## 2. Directory Structure & Subsystem Organization

```
backend/retrieval/
├── __init__.py                               # Package facade exporting types, exceptions, and settings
├── config.py                                 # RetrievalSettings dynamic Pydantic configuration
├── exceptions.py                             # Typed domain exception hierarchy
└── types/                                    # Pydantic v2 schemas and models
    ├── __init__.py                           # Export index for shared contracts
    ├── context.py                            # RetrievalContext zero-trust security boundary
    └── chunk.py                              # RetrievedChunk, RetrievedChunkSource, ChunkLocatorCoordinates
```

---

## 3. Core Models & Architectural Contracts

### 3.1. Zero-Trust Security Context (`RetrievalContext`)
Located in [`backend/retrieval/types/context.py`](file:///home/mercury/AgentGraph/backend/retrieval/types/context.py):
- `tenant_id`: Mandatory owning tenant UUID preventing cross-tenant vector and relational leakage.
- `user_id`: Authenticated user UUID for audit logging and user-level ACL checks.
- `roles`: Assigned RBAC role identifiers (e.g., `["EMPLOYEE", "HR_MANAGER"]`).
- `departments`: Authorized department scopes (e.g., `["ENGINEERING"]`).
- `classifications`: Clearance tiers (default: `["PUBLIC", "INTERNAL"]`).
- Methods: `can_access_classification(classification)`, `has_role(role)`, `belongs_to_department(department)`.

### 3.2. Passage Candidate & Locator Models (`RetrievedChunk`)
Located in [`backend/retrieval/types/chunk.py`](file:///home/mercury/AgentGraph/backend/retrieval/types/chunk.py):
- `chunk_id`, `document_id`, `document_version_id`: Canonical relational foreign keys.
- `content`: Textual passage payload.
- `locator`: `ChunkLocatorCoordinates` capturing `page_number`, `section_heading`, `sheet_name`, `row_range`, and character offsets for line-level citation badges.
- `dense_score`, `sparse_score`, `rrf_score`, `rerank_score`: Granular score tracking across each retrieval stage.
- `dense_rank`, `sparse_rank`: 1-based ranks feeding into Reciprocal Rank Fusion.
- `source`: `RetrievedChunkSource` enum (`DENSE`, `SPARSE`, `FUSED`).

### 3.3. Dynamic Configuration (`RetrievalSettings`)
Located in [`backend/retrieval/config.py`](file:///home/mercury/AgentGraph/backend/retrieval/config.py):
- `database_url`: PostgreSQL connection string.
- `qdrant_host`, `qdrant_port`, `qdrant_url`, `qdrant_collection`: Vector database targets.
- `qdrant_hnsw_ef`: Runtime exploration factor $ef = 64$.
- `embedding_model_name`: `BAAI/bge-m3` (1024-dim dense representation).
- `reranker_model_name`: `BAAI/bge-reranker-v2-m3`.
- `reranker_timeout_seconds`: $1.5\text{s}$ SLA ceiling before resilient fallback to RRF.
- `reranker_min_score_threshold`: $0.35$ normalized relevance floor.
- `dense_top_k`: $40$, `sparse_top_k`: $40$, `fused_top_k`: $40$, `final_top_k`: $8$.
- `rrf_k`: $60$, `rrf_dense_weight`: $0.7$, `rrf_sparse_weight`: $0.3$.

---

## 4. Verification & Unit Tests

Test Suite: [`tests/retrieval/test_retrieval_architecture_contracts.py`](file:///home/mercury/AgentGraph/tests/retrieval/test_retrieval_architecture_contracts.py)

```
tests/retrieval/test_retrieval_architecture_contracts.py ........        [100%]
============================== 8 passed in 0.18s ===============================
```

### Coverage Highlights:
- Dynamic settings instantiation, custom override, and boundary validation errors.
- Security context authorization, case-insensitive role resolution, classification clearance, and immutability invariants.
- Retrieved chunk creation, provenance coordinate preservation, and immutability.
- Typed domain exceptions inheritance from `RetrievalError`.
