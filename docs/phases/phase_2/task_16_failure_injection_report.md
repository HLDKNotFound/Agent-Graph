# Task 16 — Failure Injection and Fault Tolerance Report

## 1. Scope & Objective

Requirement B of Task 16 mandates systematic failure injection testing across all major failure boundaries of the document ingestion workflow. 

The objective is to verify that:
1. No unhandled system crashes occur.
2. Incomplete or failed jobs transition safely to `DocumentIngestionStatus.FAILED` without leaving phantom `COMPLETED` records.
3. Partial vector points staged in Qdrant are cleanly purged by saga compensation logic.
4. **Zero persistent orphan vectors** remain in Qdrant after failure or reconciliation.
5. Ingestion retries resume from safe processing checkpoints rather than restarting completed stages.

---

## 2. Failure Injection Matrix & Verified Outcomes

The 10 mandatory failure injection scenarios were executed and verified in [tests/test_failure_injection.py](file:///home/mercury/AgentGraph/tests/test_failure_injection.py):

| Test ID | Failure Point Injected | Simulation Mechanism | Observed System Behavior | Orphan Vectors Left in Qdrant | Verification Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **FI-01** | **File Extraction Failure** | Corrupted binary stream with invalid header bytes. | Rejected at Stage 1 / Stage 3; `CorruptDocumentError` caught; document marked `FAILED`. | **0** | **PASSED** |
| **FI-02** | **OCR Processing Crash** | Mock OCR worker raising `RuntimeError` (SIGSEGV/OOM). | Trapped inside `DefaultIngestionOrchestrator`; state machine records `StageStatus.FAILED` for extraction. | **0** | **PASSED** |
| **FI-03** | **Chunk Generation Failure** | Chunker raising `IngestionError` (token overflow). | Pipeline aborts cleanly before embeddings or persistence; error diagnostics logged. | **0** | **PASSED** |
| **FI-04** | **Provenance Validation Failure** | Chunks with locator coordinates exceeding document bounds. | `ChunkProvenanceValidator` marks report invalid; `ValidationError` aborts pipeline before DB commit. | **0** | **PASSED** |
| **FI-05** | **Embedding Inference Failure** | Simulated GPU OOM / connection timeout on `embed_texts`. | Embedding stage fails; persistence is skipped; 0 vectors reach Qdrant. | **0** | **PASSED** |
| **FI-06** | **PostgreSQL Persistence Failure** | Simulated DB deadlock during `save_chunks_and_outbox`. | Atomic transaction rolls back chunks and outbox events; compensating vector cleanup triggers. | **0** | **PASSED** |
| **FI-07** | **Qdrant Indexing Network Failure** | Connection drop (`ConnectionError`) during batch upsert. | Outbox dispatcher attempts exponential retries; compensation manager purges partially indexed points. | **0** | **PASSED** |
| **FI-08** | **Worker Soft Timeout (TimeLimit)** | `SoftTimeLimitExceeded` raised during worker task. | Worker task catches timeout; returns structured `TaskStageResult` with `status=FAILURE` and clean diagnostics. | **0** | **PASSED** |
| **FI-09** | **Redis Broker Disconnection** | Redis client failure / unreachable broker URL. | `JobIdempotencyGuard` logs warning and falls back to thread-safe in-memory lock store without crashing. | **0** | **PASSED** |
| **FI-10** | **Retry Exhaustion & Dead-Lettering** | Retries exhausted on vector indexing outbox event. | Event transitions to `DEAD_LETTER`; saga compensation purges vector points from Qdrant. | **0** | **PASSED** |

---

## 3. Detailed Fault Mechanism Analysis

### 3.1 PostgreSQL Transaction Rollback (FI-06)
- **Challenge**: If relational persistence fails after vectors have been prepared, dual-write systems frequently leave orphan vectors in Qdrant.
- **Solution Verified**: The **Transactional Outbox Pattern** ensures database chunk records and vector outbox events are committed in the same PostgreSQL transaction block. If the transaction aborts, no outbox event is committed, and `CompensationManager.compensate_indexing_failure` purges any staged point UUIDs from Qdrant.

### 3.2 Network Partition during Vector Indexing (FI-07)
- **Challenge**: Transient network partitions to Qdrant cluster during batch upserts.
- **Solution Verified**: `OutboxDispatcher` applies exponential backoff retries (`retry_delay = 2 ** retry_count`). When retries are exhausted, the event transitions to `DEAD_LETTER`, and saga compensation purges any partial vectors using `delete_by_point_ids`.

### 3.3 Worker Crash and Interrupted Jobs (FI-08, FI-09)
- **Challenge**: A worker process killed abruptly (OOM or hard timeout) leaves a job stuck in `PROCESSING`.
- **Solution Verified**:
  - `JobIdempotencyGuard` locks expire automatically via TTL (default 600s).
  - `CrossStorageReconciler.audit_consistency` audits pending outbox events and resumes interrupted jobs from safe boundary checkpoints without repeating expensive extraction and embedding stages.
