# Enterprise Knowledge Agent – Remaining Technical Limitations & Architectural Trade-offs

**Phase**: 1 – Database Infrastructure & Foundations  
**Task**: 10 – Database Security, Performance Optimization & Final Validation  
**Date**: November 18, 2026  

---

## 1. Executive Summary

This document transparently records the remaining technical limitations, architectural trade-offs, and operational boundaries of the database infrastructure implemented across Tasks 01–10.

Identifying these limitations upfront enables downstream engineering teams (Phase 2 Ingestion, Phase 3 Retrieval, Phase 4 Agentic Reasoning) to design guardrails and avoid architectural anti-patterns.

---

## 2. Technical Limitations & Mitigation Strategies

### 2.1. Prisma ORM Domain-Specific Language (DSL) Expressiveness
- **Limitation**: Prisma Schema Language (PSL) natively supports standard relational constructs (tables, columns, foreign keys, simple composite indexes), but cannot natively define:
  1. PostgreSQL Row-Level Security (RLS) policies.
  2. Functional expression indexes (e.g., `USING GIN (to_tsvector('simple', content))`).
  3. Table-level check constraints (`CHECK ("file_size_bytes" >= 0)`).
  4. PostgreSQL PL/pgSQL database triggers.
- **Architectural Trade-off**: All advanced database features are managed through raw SQL migrations (`backend/prisma/migrations/`) rather than Prisma schema annotations.
- **Mitigation & Standard**: The dual-directory migration standard (`backend/prisma/migrations` and `backend/migrations`) and Prisma deployment pipeline ensure 100% reproducibility. Schema updates must never rely on `prisma db push`.

---

### 2.2. Connection Pooling & Transaction-Scoped RLS Context
- **Limitation**: When using connection poolers like PgBouncer in `transaction` pooling mode, session-level state is forbidden. Executing `SET app.current_tenant_id = ...` at the session level can pollute subsequent client connections sharing that socket.
- **Mitigation & Standard**: The application MUST exclusively use `SET LOCAL app.current_tenant_id = ...` within interactive Prisma transactions (`prisma.$transaction(async (tx) => ...)`). When the transaction commits or aborts, PostgreSQL automatically restores the parameter. Direct untransactioned queries by the application role default to zero rows (Zero-Trust fail-safe).

---

### 2.3. GIN Full-Text Search Write Amplification on Large Ingestion Batches
- **Limitation**: While B-Tree indexes scale linearly with row count, Generalized Inverted Indexes (GIN) dissect textual content into individual lexemes and update postings lists. For documents containing 50,000+ words across hundreds of chunks, synchronous GIN updates introduce non-trivial I/O write amplification.
- **Mitigation & Standard**:
  1. `document_chunks_content_fts_idx` was eliminated in Task 10, halving GIN write overhead.
  2. PostgreSQL's `gin_pending_list_limit` (default 4MB) leverages fast updates, flushing postings lists asynchronously.
  3. Bulk ingestion workers in Phase 2 should chunk and insert in batches of 100–500 chunks per transaction to minimize lock contention.

---

### 2.4. Hybrid Storage Boundary: PostgreSQL Relational vs. Qdrant Dense Vectors
- **Limitation**: The platform intentionally separates sparse lexical retrieval (PostgreSQL GIN) from dense semantic retrieval (Qdrant HNSW). Because Qdrant and PostgreSQL are separate distributed systems, atomic two-phase commits across both systems are not natively supported.
- **Architectural Trade-off**: If a chunk is inserted into PostgreSQL but Qdrant fails during embedding generation, an orphan relational chunk could exist without a vector point.
- **Mitigation & Standard**:
  1. `DocumentChunk` includes `vector_storage_id` and `embedding_id` tracking fields.
  2. Ingestion pipeline (Celery tasks) enforces soft-state: chunk status remains `PENDING` until both PostgreSQL and Qdrant confirm ingestion, rolling back or retrying asynchronously via idempotency keys.

---

### 2.5. Multi-Hop Hierarchical Department Cycles
- **Limitation**: The database check constraint `departments_parent_not_self_check` strictly prevents direct self-parenting (`parent_id != id`). However, indirect cyclical dependencies spanning 3+ levels (e.g. Department A -> Department B -> Department C -> Department A) cannot be validated by simple SQL check constraints without recursive queries.
- **Mitigation & Standard**: The API layer and Department Management service must execute a topological sort or recursive common table expression (CTE) validation before committing department re-parenting operations.

---

### 2.6. Cross-Tenant Reporting & Aggregations under Strict RLS
- **Limitation**: Under normal application role execution (`agentgraph_app`), cross-tenant joins or aggregation queries (e.g., global platform usage, cross-tenant token billing) are strictly blocked by RLS policies.
- **Mitigation & Standard**: Platform-wide reporting, billing, and system telemetry background jobs must explicitly establish an administrative context using `SET LOCAL app.bypass_rls = 'on';` inside an isolated transaction, or run under a dedicated read-only telemetry service role.
