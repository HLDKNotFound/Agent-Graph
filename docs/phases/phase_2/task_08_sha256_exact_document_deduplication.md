# Task 08: SHA-256 Exact Document Deduplication – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, `asyncpg` 0.32.0, `psycopg2-binary` 2.9.13, Pydantic v2, PostgreSQL 16+, Prisma ORM 5.22.0)  
**Phase**: Phase 2 / Stage 3 – Deduplication and Chunking (Task 08)  
**Subsystems**:
- [`backend/ingestion/deduplication/hasher.py`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/hasher.py)
- [`backend/ingestion/deduplication/models.py`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/models.py)
- [`backend/ingestion/deduplication/repository.py`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/repository.py)
- [`backend/ingestion/deduplication/service.py`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/service.py)
- [`backend/ingestion/deduplication/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/base.py)
- [`backend/ingestion/deduplication/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/__init__.py)
- [`backend/ingestion/exceptions.py`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py)
- [`backend/ingestion/config.py`](file:///home/mercury/AgentGraph/backend/ingestion/config.py)
- [`backend/ingestion/types/context.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/context.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)
- [`tests/test_document_deduplication.py`](file:///home/mercury/AgentGraph/tests/test_document_deduplication.py)
- [`tests/test_document_deduplication_db.py`](file:///home/mercury/AgentGraph/tests/test_document_deduplication_db.py)

---

## 1. Executive Summary & Objective

The primary objective of **Stage 3 (Deduplication and Chunking) – Task 08** was to implement production-grade, streaming SHA-256 exact document deduplication to prevent redundant documents from entering the ingestion pipeline.

The subsystem computes deterministic cryptographic hashes via bounded chunked reads directly from uploaded file bytes prior to document parsing, inspects the relational PostgreSQL repository under strict zero-trust tenant boundaries, respects document soft-deletion and versioning lifecycles, and guarantees race safety under concurrent upload collisions.

### Mandatory Requirements & Acceptance Criteria Satisfied

1. **Pre-Extraction Hashing**: Calculated from the original uploaded file bytes/stream before any content extraction or text parsing occurs.
2. **Memory-Bounded Streaming Reads**: Implemented [`StreamHasher`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/hasher.py#L18) reading chunks (default `64 KB`, configurable via `DEDUPLICATION_CHUNK_SIZE_BYTES`) so multi-gigabyte files never exhaust system memory.
3. **Seekable Stream Rewinding**: Automatically records and restores file stream positions (`seek(0)`) after hashing so subsequent file storage and extraction stages read from the beginning.
4. **Tenant-Scoped Duplicate Verification**: Evaluates content hashes strictly within the authenticated tenant boundary (`WHERE tenant_id = :tenant_id AND content_hash = :hash`).
5. **Cross-Tenant Non-Collision**: Identical files uploaded by distinct tenants are explicitly permitted to co-exist without sharing records or leaking metadata across tenant boundaries.
6. **Soft-Deletion Awareness**: Queries filter `WHERE deleted_at IS NULL`. Documents soft-deleted from a workspace allow re-uploading identical file hashes without collision.
7. **Clear Duplicate Handling Behavior**: Configured through [`DuplicateHandlingPolicy`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/models.py#L9):
   - `RETURN_EXISTING`: Returns structured summary [`ExistingDocumentSummary`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/models.py#L40) of the active matching document, short-circuiting downstream stages.
   - `REJECT`: Raises [`DuplicateDocumentError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L114) with diagnostic details (`tenant_id`, `content_hash`, `existing_document_id`).
   - `NEW_VERSION`: Generates an incremental version snapshot under `document_versions` in PostgreSQL.
8. **Independent from Parsing**: The hasher operates on raw bytes and binary streams with zero dependencies on document extractors (`PyMuPDF`, `python-docx`, `openpyxl`).
9. **Race-Safe Concurrency Control**: Backed by PostgreSQL 16+ partial unique index `documents_tenant_active_content_hash_key` (`ON documents (tenant_id, content_hash) WHERE deleted_at IS NULL`). Concurrent upload collisions are caught atomically via [`register_document_safely()`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/service.py#L210), preventing orphan duplicate active records.
10. **Subsystem Interface Compliance**: [`Sha256ExactDeduplicationStrategy`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/service.py#L231) implements the abstract [`DeduplicationStrategy`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/base.py#L50) interface.

---

## 2. Architecture & Subsystem Design

```
                     ┌────────────────────────────────────────────────┐
                     │ Raw Uploaded Document (Stream, Bytes, or Path) │
                     └───────────────────────┬────────────────────────┘
                                             │
                                             ▼
                     ┌────────────────────────────────────────────────┐
                     │ StreamHasher (Bounded 64KB Chunk Reads)        │
                     │ ├── Streaming hashlib.sha256 incremental update│
                     │ ├── Exact file size byte counting              │
                     │ └── Stream cursor rewind (seek(0))             │
                     └───────────────────────┬────────────────────────┘
                                             │
                                             ▼ (SHA-256 Hash + File Size)
                     ┌────────────────────────────────────────────────┐
                     │ DocumentDeduplicator / Strategy                │
                     │ ├── Enforce IngestionContext (tenant_id)       │
                     │ └── Resolve DuplicateHandlingPolicy            │
                     └───────────────────────┬────────────────────────┘
                                             │
                                             ▼
                     ┌────────────────────────────────────────────────┐
                     │ DocumentRepository (Postgres or In-Memory)     │
                     │ ├── Query: (tenant_id, content_hash, active)   │
                     │ └── Index: documents_tenant_active_content_... │
                     └───────────────────────┬────────────────────────┘
                                             │
                      ┌──────────────────────┴──────────────────────┐
                      │                                             │
         [Active Duplicate Found]                       [No Duplicate Detected]
                      │                                             │
                      ▼                                             ▼
        Check DuplicateHandlingPolicy                 DeduplicationResult
        ├── RETURN_EXISTING:                          ├── is_duplicate = False
        │   └── Return ExistingDocumentSummary        ├── action = PROCEED
        ├── REJECT:                                   └── Proceed to Storage &
        │   └── Raise DuplicateDocumentError              Document Extraction
        └── NEW_VERSION:
            └── Create DocumentVersion Snapshot
```

---

## 3. Subsystem Implementation Details

### 3.1. `backend/ingestion/deduplication/hasher.py`
- Implements `StreamHasher` with `hash_stream()`, `hash_file()`, `hash_bytes()`, and polymorphic `hash_content()`.
- Uses `chunk_size = 65536` (64 KB) by default, ensuring bounded $O(1)$ memory consumption.
- Performs position inspection and rewinds seekable streams:
  ```python
  if rewind_after and is_seekable:
      stream.seek(initial_pos)
  ```
- Validates non-empty files, raising `EmptyFileError` when 0 bytes are detected.

### 3.2. `backend/ingestion/deduplication/models.py`
- Enums:
  - `DuplicateHandlingPolicy`: `RETURN_EXISTING`, `REJECT`, `NEW_VERSION`.
  - `DuplicateAction`: `PROCEED`, `RETURN_EXISTING`, `REJECTED`, `NEW_VERSION_CREATED`.
- Models:
  - `ExistingDocumentSummary`: Immutable snapshot of an existing document record.
  - `DocumentCreateInput`: Type-safe parameters for atomic document creation.
  - `DocumentVersionCreateInput`: Type-safe parameters for version snapshot creation.

### 3.3. `backend/ingestion/deduplication/repository.py`
- `DocumentRepository` (ABC): Core persistence interface for document deduplication lookups.
- `PostgresDocumentRepository`:
  - Connects to PostgreSQL 16+ via `asyncpg` with zero credentials hardcoded (`DATABASE_URL`).
  - Executes index-optimized SQL queries:
    ```sql
    SELECT id, tenant_id, original_filename, content_hash, ...
    FROM documents
    WHERE tenant_id = $1::uuid AND content_hash = $2 AND deleted_at IS NULL
    LIMIT 1;
    ```
  - Catches `asyncpg.UniqueViolationError` on concurrent collisions and recovers the winning active document record.
- `InMemoryDocumentRepository`:
  - Thread-safe and asyncio-safe in-memory repository for unit tests.
  - Faithfully enforces the `(tenant_id, content_hash) WHERE deleted_at IS NULL` unique index and soft-deletion rules.

### 3.4. `backend/ingestion/deduplication/service.py`
- `DocumentDeduplicator`: Coordinates streaming hashing, repository lookups, and policy actions.
- `Sha256ExactDeduplicationStrategy`: Conforms to `DeduplicationStrategy` with pass-through for subsequent chunk-level MinHash LSH filtering.

---

## 4. Verification & Testing

### 4.1. Unit Test Suite (`tests/test_document_deduplication.py`)
17 unit tests verifying:
- Streaming SHA-256 hash match against `hashlib.sha256`.
- Chunked reads with varied buffer sizes (16 B to 64 KB).
- Seekable stream cursor rewind.
- 0-byte file rejection (`EmptyFileError`).
- Independence from document parsers.
- `RETURN_EXISTING`, `REJECT`, and `NEW_VERSION` duplicate handling policies.
- Multi-tenant boundary isolation (identical hashes permitted in distinct tenants).
- Soft-deletion aware deduplication (soft-deleted doc allows re-uploading identical hash).
- Race-safe concurrent duplicate uploads (20 simultaneous uploads yield exactly 1 active document).
- Strategy interface compliance.

### 4.2. Database Integration Test Suite (`tests/test_document_deduplication_db.py`)
4 database integration tests executed against live PostgreSQL 16+:
- Active duplicate detection using partial index `documents_tenant_active_content_hash_key`.
- Multi-tenant hash coexistence in PostgreSQL.
- Soft-delete and re-upload lifecycle in PostgreSQL.
- Concurrent insertion race recovery (`register_document_safely`).

### 4.3. Test Execution Results

```bash
# Run Task 08 test suites
conda run -n ml-env pytest tests/test_document_deduplication.py tests/test_document_deduplication_db.py
# Result: 21 passed in 1.29s

# Run full repository regression suite
conda run -n ml-env pytest
# Result: 202 passed in 21.42s (100% pass rate)

# Validate Prisma schema
npx prisma validate --schema backend/schema.prisma
# Result: Valid

# Run database verification
node backend/test_document_management.js
# Result: All 9 test steps passed
```
