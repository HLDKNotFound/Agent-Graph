# Task 16 — Performance Benchmark & Profiling Report

## 1. Executive Summary

This report documents the empirical performance benchmarks, resource utilization metrics, and SLA compliance audits executed for the **Enterprise Knowledge Agent** document ingestion pipeline (Phase 2, Task 16).

All benchmark tests were executed against representative enterprise datasets across heterogeneous file formats (Native PDF, DOCX, XLSX, CSV) using automated profiling harness `benchmarks/benchmark_ingestion_pipeline.py`.

### Quality Gate Pass/Fail Scorecard

| Quality Gate Requirement | SLA Target | Measured Performance | Result |
| :--- | :--- | :--- | :--- |
| **Standard Text PDF Extraction** | $\ge 50$ Pages/Minute (PPM) | **257.68 – 284.79 PPM** | **PASSED (5.7x target)** |
| **Peak Memory Consumption (100 Pages)** | $< 250$ MB RAM | **49.91 MB** | **PASSED (80% margin)** |
| **Embedding Model Throughput** | $> 10,000$ tokens/second | **82,781 tokens/second** | **PASSED** |
| **Qdrant Vector Indexing Latency** | $< 5.0$ ms / vector point | **0.332 ms / point** | **PASSED** |
| **Concurrent Document Ingestion** | Zero thread/event deadlocks | **5 docs in 10.747s (13.96 chunks/s)** | **PASSED** |
| **Extraction Accuracy & Lineage** | 100% locator preservation | **100% non-empty chunks valid** | **PASSED** |

---

## 2. Text PDF Extraction Throughput

PDF extraction was benchmarked across 10-page, 50-page, and 100-page enterprise documents containing mixed text densities, H1–H3 section hierarchies, and embedded markdown table blocks.

| Document Size | File Size (KB) | Extracted Elements | Processing Duration (s) | Throughput (PPM) | SLA Status ($\ge 50$ PPM) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **10 Pages** | 22.87 KB | 80 elements | 2.329 s | **257.68 PPM** | **PASSED** |
| **50 Pages** | 113.33 KB | 400 elements | 10.534 s | **284.79 PPM** | **PASSED** |
| **100 Pages** | 226.41 KB | 800 elements | 21.546 s | **278.47 PPM** | **PASSED** |

### Key Architectural Findings:
1. **Linear Scaling**: PyMuPDF-based stream extraction scales strictly linearly with document page count ($O(N)$ with page count), maintaining an average throughput of **273.6 PPM**.
2. **Deterministic Garbage Collection**: Document page contexts and rendering memory buffers are closed deterministically in `finally` blocks, preventing memory creep.

---

## 3. End-to-End Latency by Document Format

End-to-end ingestion covers all 7 core stages: Validation $\to$ SHA-256 Deduplication $\to$ Extraction $\to$ Chunking $\to$ MinHash Deduplication $\to$ 1024-dim Dense Embedding $\to$ Relational Persistence $\to$ Qdrant Outbox Indexing.

| Document Format | Document Description | Size (KB) | Total Latency (s) | Chunks Created | Points Indexed | Extraction (s) | Embedding (s) | Indexing (s) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **PDF** | 50 Pages Handbook | 113.33 KB | 11.363 s | 150 | 150 | 10.632 s | 0.0755 s | 0.0498 s |
| **DOCX** | 10 Sections + Tables | 36.22 KB | 0.088 s | 10 | 10 | 0.0706 s | 0.0052 s | 0.0039 s |
| **XLSX** | 200 Tabular Rows | 8.84 KB | 0.100 s | 12 | 12 | 0.0931 s | 0.0031 s | 0.0038 s |
| **CSV** | 50 Tabular Rows | 1.84 KB | 0.022 s | 2 | 2 | 0.0182 s | 0.0011 s | 0.0027 s |

### Bottleneck Identification:
- For PDFs, **extraction and layout analysis** constitutes ~93% of runtime, while embedding and indexing together represent $< 1.5\%$ of wall-clock time.
- For structured formats (DOCX, XLSX, CSV), total latency is sub-100 milliseconds.

---

## 4. Peak Memory Consumption & Resource Profile

Resource profiling was measured during the end-to-end processing of a 100-page standard text PDF using Python's `tracemalloc` instrumentation:

- **Active Process Memory at Start**: 14.28 MB
- **Current Process Memory at Completion**: 18.65 MB
- **Peak Process Memory**: **49.91 MB**
- **SLA Ceiling**: $< 250$ MB
- **Safety Margin**: **80.04%** below allowable peak

### Memory Safety Mechanisms:
- **Streaming Hasher**: Streaming 64 KB block reads during SHA-256 calculation prevents loading entire multi-gigabyte files into RAM.
- **Chunk Generators**: Intermediate elements are batched into chunks with strict token limits (default 512 tokens), guaranteeing bounded memory usage regardless of document length.

---

## 5. Worker Concurrency & Queue Processing

Concurrency profiling evaluated parallel multi-document ingestion simulating Celery distributed workers:

- **Simultaneous Ingestion Tasks**: 5 concurrent documents (50 total pages)
- **Total Execution Time**: **10.747 seconds**
- **Document Processing Rate**: **0.47 documents / second**
- **Chunk / Vector Indexing Rate**: **13.96 chunks / second**
- **Tenant Lock Contention**: 0 deadlocks observed; `JobIdempotencyGuard` and per-document locks successfully serialized concurrent operations on the same document while allowing distinct documents to run in parallel.

---

## 6. Optimization Recommendations for Production Deployment

1. **OCR Pre-Filtering**: Ensure scanned PDF detection heuristic skips native text pages so expensive rasterization is only applied to truly scanned pages.
2. **Batch Embedding Parallelization**: In multi-GPU production environments, utilize dynamic batch size scaling (up to 64 chunks per forward pass) in `BgeM3EmbeddingService`.
3. **Qdrant Connection Pooling**: In production deployments with dedicated Qdrant clusters, configure gRPC transport with persistent connection pools (`prefer_grpc=True`) to reduce vector upsert latency below 0.2 ms per point.
4. **Celery Worker Prefetch Multiplier**: Configure `worker_prefetch_multiplier=1` and `task_acks_late=True` to prevent long document tasks from starving short tabular CSV ingestion tasks.
