# Task 03: Sparse Lexical BM25 Search via PostgreSQL GIN Index – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 25, 2026  
**Environment**: Conda `ml-env` (Python 3.12, asyncpg, PostgreSQL 16+, Pydantic v2)  
**Phase**: Phase 4 – Permission-Aware Hybrid Search & Cross-Encoder Reranking  
**Subsystem**: [`backend/retrieval/sparse`](file:///home/mercury/AgentGraph/backend/retrieval/sparse)

---

## 1. Executive Summary & Objective

The objective of **Task 03** was to build the production **Sparse Lexical BM25 Retrieval Engine** (`PostgresBM25Retriever`) leveraging PostgreSQL 16+ Generalized Inverted Indexes (GIN) on `document_chunks(to_tsvector('simple', content))`. This engine fulfills the exact lexical matching requirements of hybrid search (Stage 1), handling acronyms, policy IDs, employee codes, and strict legal keyword queries while maintaining strict zero-trust tenant and RBAC boundaries.

### Strict Scope Boundaries Adhered To
1. **Parameterized, SQL-Injection Safe Queries**: Completely eliminated string interpolation or unescaped query parameters, binding `$1` through `$6` safely through `asyncpg`.
2. **PostgreSQL Lexical Ranking**: Utilized `ts_rank_cd(to_tsvector('simple', content), websearch_to_tsquery('simple', $1), 32)` with normalization flag `32` ($rank / (rank + 1)$), producing smooth bounded scores $\in [0.0, 1.0)$.
3. **Multi-Tenancy & Zero-Trust RBAC**:
   - Hard tenancy check: `c.tenant_id = $2::uuid AND d.tenant_id = $2::uuid`.
   - Active document check: `d.deleted_at IS NULL`.
   - Access control barriers: universal `PUBLIC` classification, user clearance array matching (`d.classification = ANY($3::text[])`), and role/department metadata membership verification.
4. **Resilient Syntax Fallback**: If user input contains complex syntax invalidating `websearch_to_tsquery`, the engine automatically retries with `plainto_tsquery` without dropping the query.
5. **Connection Pooling**: Supported asynchronous connection pooling (`asyncpg.create_pool`) with connection reuse.

---

## 2. Directory Structure & Subsystem Organization

```
backend/retrieval/sparse/
├── __init__.py                               # Sparse retrieval package exports
└── pg_bm25_search.py                         # PostgresBM25Retriever implementation
```

---

## 3. Core Implementation Details

### 3.1. Parameterized SQL Query Formulation
Located in [`backend/retrieval/sparse/pg_bm25_search.py`](file:///home/mercury/AgentGraph/backend/retrieval/sparse/pg_bm25_search.py):
```sql
SELECT 
    c.id AS chunk_id,
    c.tenant_id,
    c.document_id,
    c.document_version_id,
    c.chunk_index,
    c.content,
    c.token_count,
    c.page_number,
    c.section_heading,
    c.sheet_name,
    c.row_range,
    c.char_start,
    c.char_end,
    c.metadata AS chunk_metadata,
    d.original_filename,
    d.classification,
    d.department_id,
    ts_rank_cd(to_tsvector('simple', c.content), websearch_to_tsquery('simple', $1), 32) AS relevance_score
FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.tenant_id = $2::uuid
  AND d.tenant_id = $2::uuid
  AND d.deleted_at IS NULL
  AND to_tsvector('simple', c.content) @@ websearch_to_tsquery('simple', $1)
  AND (
      d.classification::text = 'PUBLIC'
      OR d.classification::text = ANY($3::text[])
      OR (
          c.metadata->'allowed_roles' IS NOT NULL 
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(COALESCE(c.metadata->'allowed_roles', '[]'::jsonb)) r 
              WHERE r = ANY($4::text[])
          )
      )
      OR (
          c.metadata->'allowed_departments' IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(COALESCE(c.metadata->'allowed_departments', '[]'::jsonb)) dept
              WHERE dept = ANY($5::text[])
          )
      )
  )
ORDER BY relevance_score DESC, c.id ASC
LIMIT $6::int;
```

### 3.2. Passage Mapping
- Each database record maps to an immutable `RetrievedChunk` with `source = RetrievedChunkSource.SPARSE`.
- Lineage coordinates (`page_number`, `section_heading`, `sheet_name`, `row_range`, character offsets) are preserved for citation generation in Phase 5.
- The 1-based rank ($1 \dots K_1$) is recorded in `sparse_rank` to prepare for Reciprocal Rank Fusion (Task 05).

---

## 4. Verification & Unit Tests

Test Suite: [`tests/retrieval/test_pg_bm25_search.py`](file:///home/mercury/AgentGraph/tests/retrieval/test_pg_bm25_search.py)

```
tests/retrieval/test_pg_bm25_search.py .....                             [100%]
============================== 5 passed in 0.16s ===============================
```

### Key Verification Scenarios Passed:
1. **SQL Structure & Security AST**: Validated parameter isolation, absence of SQL string injection vulnerabilities, and proper GIN expression index binding.
2. **Attribute & Provenance Mapping**: Confirmed all locator coordinates and classification fields are mapped without data loss.
3. **Empty Query Short-Circuit**: Verified whitespace/empty queries return immediately without dispatching database network calls.
4. **Resilient Syntax Fallback**: Verified complex punctuation or boolean errors in `websearch_to_tsquery` smoothly trigger retry via `plainto_tsquery`.
5. **Database Failure Resilience**: Verified connection dropouts trigger structured `SparseRetrievalError` exceptions.
