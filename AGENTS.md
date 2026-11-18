# Enterprise Knowledge Agent – Agent Operational Guidelines

## System Overview & Architecture Context
This repository hosts the **Enterprise Knowledge Agent**, a production-grade, permission-aware advanced Retrieval-Augmented Generation (RAG) platform. The system is designed to ingest heterogeneous enterprise documents (PDF native/scanned OCR, DOCX, XLSX, CSV), perform hybrid search (Dense semantic + Sparse BM25 via Reciprocal Rank Fusion), execute multi-hop iterative reasoning via LangGraph, enforce zero-trust role-based access control (RBAC), generate answers with line-level verifiable citation badges (`[1]`), and evaluate factual consistency in real-time.

Always init the environment in "conda activate ml-env".

---

## 1. Zero Hardcoding Principle (Mandatory)
All code, configurations, and scripts within this codebase MUST strictly adhere to the dynamic configuration standard:

1. **Environment Variables**: All external service URLs, database connection strings, credentials, API keys, and port numbers must be resolved through environment variables using Pydantic Settings (`pydantic-settings`) in Python or `process.env` in TypeScript:
   - Database: `DATABASE_URL`, `DIRECT_URL`
   - Vector Store: `QDRANT_HOST`, `QDRANT_PORT`, `QDRANT_API_KEY`, `QDRANT_COLLECTION`
   - Cache & Task Queue: `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`
   - Object Storage: `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`, `S3_REGION`
   - LLM & Embeddings: `LLM_PROVIDER`, `LLM_API_KEY`, `EMBEDDING_MODEL_NAME`, `RERANKER_MODEL_NAME`
   - Telemetry: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
2. **No Hardcoded Tenancy or Paths**: Never hardcode tenant identifiers (e.g., `"tenant_enterprise_vn"`), user IDs, or local absolute file paths (`/tmp/...`, `/home/...`). Always derive tenant context from authenticated JWT sessions and use path-agnostic utilities (`pathlib.Path` in Python, `path.resolve` in Node).
3. **Template & Fallback Management**: Always provide a clean `.env.example` file describing all required and optional environment keys with descriptive comments and safe placeholder names.

---

## 2. Core Architectural Modules & Implementation Standards

### Module 1: Document Ingestion & Lineage Tracking
- **Multi-Format Extraction**: Support PDF (native via `pdfplumber`/`PyMuPDF`, scanned via OCR `Tesseract`/`RapidOCR`), Word (`python-docx`), and Excel (`openpyxl`/`pandas`).
- **Traceability Coordinates**: Every document chunk must persist precise locator coordinates: `page_number`, `section_heading`, `sheet_name`, `row_range`, `char_start`, and `char_end`.
- **Deduplication**: Enforce SHA-256 file hashing before parsing to eliminate exact duplicates, and MinHash LSH chunk indexing to catch near-duplicate cross-document sections.
- **Atomic Operations**: Document ingestion must be atomic. Failed parses must trigger clean rollback and soft-deletion without leaving orphan vectors in Qdrant.

### Module 2 & 3: Embedding, Vector DB & Hybrid Search
- **Embedding Abstraction**: Use a model-agnostic `EmbeddingProvider` interface (defaulting to `BAAI/bge-m3` 1024-dim dense representation).
- **Qdrant Index Configuration**: HNSW index configuration must use `m = 16`, `ef_construct = 128`, and runtime query `ef = 64` with cosine distance metric.
- **Sparse BM25 Search**: Execute sparse lexical search against PostgreSQL Full-Text Search (GIN index on `document_chunks(to_tsvector('simple', content))`) or integrated sparse vector payloads.
- **Reciprocal Rank Fusion (RRF)**: Fuse dense and sparse candidate lists using the formula:
  $$RRF\_Score(d) = \sum_{m \in \{Dense, Sparse\}} \frac{w_m}{k + rank_m(d)}$$
  Default parameters: $k = 60$, $w_{Dense} = 0.7$, $w_{Sparse} = 0.3$.

### Module 4 & 5: Query Understanding & Cross-Encoder Reranking
- **Intent Classification Layer (First-Stage Routing Gate)**: Every incoming user message must first pass through a dedicated Intent Classification layer ("Router Layer") before hitting retrieval or reasoning engines. This layer determines whether RAG execution is required and dictates the retrieval depth (single-hop vs. multi-hop iterative):
  1. `FACTUAL_RETRIEVAL`: Seeks specific factual policy, rule, or information within internal docs (e.g., *"Chính sách nghỉ phép là gì?"*).
     - **RAG Required**: **YES** (Single-hop Hybrid Search: Dense + Sparse BM25 + Cross-Encoder Reranking).
  2. `MULTI_DOC_COMPARISON`: Comparative questions spanning multiple departments, entities, or documents (e.g., *"So sánh HR và Sales"*).
     - **RAG Required**: **YES** (Multi-hop Iterative LangGraph DAG with query decomposition, evidence aggregation, and loop detection up to $\text{MaxHops}=3$).
  3. `TABLE_LOOKUP`: Lookup or aggregation targeting structured/tabular matrices (e.g., *"Lương của Engineering là bao nhiêu?"*).
     - **RAG Required**: **YES** (Table-Aware Retrieval filtering by sheet/row/column locator coordinates, structured chunks, and markdown table rendering).
  4. `GENERAL_CHAT`: Polite conversational greetings, acknowledgments, or conversational pleasantries (e.g., *"Xin chào"*, *"Cảm ơn bạn"*).
     - **RAG Required**: **NO** (Bypasses retrieval entirely; routed to direct LLM generation with conversational persona, saving vector DB latency and token cost).
  5. `OUT_OF_DOMAIN`: Inquiries outside corporate scope, internal handbook, or enterprise data (e.g., *"Thời tiết hôm nay thế nào?"*, *"Ai thắng giải Oscar?"*).
     - **RAG Required**: **NO (Internal Docs)** / **YES (External Web)**: Bypasses internal document vector store/database; routes to External Web Search Engine (Internet search) to retrieve live web information, attaching external web citations and a clear disclaimer distinguishing internet sources from corporate records. Zero internal tenant data or confidential tokens may be leaked to external search queries.
- **Coreference Resolution & Query Rewriting**: In multi-turn chat, resolve pronouns and ellipsis against previous turns before formulating search queries.
- **Reranking**: Candidate passages from RRF (top $K_1 = 40$) must be scored via a cross-encoder model (`BAAI/bge-reranker-v2-m3`). Top $K_2 = 5\text{ to }8$ chunks are selected with normalized relevance scores $\in [0.0, 1.0]$.
- **Graceful Fallback**: If the reranker service encounters a timeout or OOM error, the pipeline must smoothly fall back to the RRF rank order and log a warning to Langfuse.

### Module 6: Multi-Hop Retrieval & Agentic Reasoning
- **LangGraph Controller**: Implement iterative multi-hop reasoning as a directed acyclic graph (DAG).
- **Termination Guardrails**:
  - Hard cap of $\text{MaxHops} = 3$ iterations.
  - Cycle/loop detection: Cosine similarity $> 0.95$ with previous hop queries terminates the trajectory.
  - Evidence Sufficiency Gate: If accumulated evidence answers all components of the query, terminate early to conserve tokens and reduce latency.

### Module 7: Grounded Generation & Citation Standards
- **Verifiable Citations**: Every claim in the LLM response must be attributed with inline numerical markers `[1]`, `[2]` directly mapped to the source `document_id`, `chunk_id`, page, and section.
- **Deterministic Abstention**: When retrieved evidence is insufficient, contradictory, or absent under the user's authorized permissions, the model MUST NOT speculate. It must return a deterministic abstention message explaining what policy or document is missing.

### Module 8: Zero-Trust Security & RBAC Enforcement
- **Three-Tier Security Barrier**:
  1. *Stage 1 (Pre-Retrieval Filter)*: Qdrant vector queries MUST contain payload filters matching the user's `tenant_id`, active roles (`acl.roles`), and departments (`acl.departments`).
  2. *Stage 2 (Hybrid Search)*: Sparse queries must filter by `tenant_id` and document permission tables in PostgreSQL.
  3. *Stage 3 (Post-Retrieval Verification)*: Before chunks enter the LLM context, verify active permissions in PostgreSQL to prevent stale payload leaks.
- **Immutable Audit Logging**: Every query, retrieval, and access decision (`GRANTED`, `DENIED`, `PARTIALLY_GRANTED`) must be logged to the `audit_logs` table and retained for 365 days.

### Module 9 & 10: Hallucination Detection & Observability
- **NLI Verification**: Answer claims must be verified against source chunks using a Natural Language Inference cross-encoder (`ENTAILMENT`, `NEUTRAL`, `CONTRADICTION`).
- **Quality SLA Targets**:
  - Faithfulness $\ge 0.90$
  - Citation Accuracy $\ge 0.95$
  - Answer Relevancy $\ge 0.85$
  - Single-hop Retrieval Latency $P_{95} \le 1.5\text{s}$
- **Telemetry**: Instrument pipeline steps with Langfuse traces and Prometheus metrics (`rag_query_latency_seconds`, `rag_token_usage_total`, `rag_hallucination_refusal_total`).

---

## 3. Technology Stack & Coding Standards

### Backend (Python 3.11+ / FastAPI)
- Adhere strictly to **PEP 8** style guidelines, typed using modern Python type annotations (`typing` / `mypy` strict).
- Use **FastAPI** with async def handlers for high-throughput endpoints.
- Define request and response contracts using **Pydantic v2** (`BaseModel`, `Field`, `ConfigDict`).
- Manage heavy async tasks (OCR, large document chunking, batch embeddings) via **Celery** workers with Redis.

### Frontend (Next.js 14+ / TypeScript)
- Use **Next.js 14 App Router** (`app/` directory) with TypeScript strict mode enabled.
- Styling: Use modern, accessible Vanilla CSS / CSS Modules with clear design tokens, or Tailwind CSS configured without ad-hoc arbitrary values.
- Implement the 3-panel responsive layout:
  1. Left: Workspace & Department Document Catalog
  2. Center: Verifiable Conversation Console with Citation Badges
  3. Right: Source Evidence Inspector (PDF.js / Document Viewer with highlighted page & paragraph)
- Maintain full keyboard accessibility (`⌘K` command palette, `Enter` to send, `Esc` to close).

### Database & Persistence (PostgreSQL 16+ & Prisma)
- Primary ORM schema is maintained in `backend/schema.prisma` with `prisma-client-js` and PostgreSQL extensions (`vector`).
- Always run `npx prisma format` when updating `schema.prisma`.
- Ensure migrations preserve pgvector HNSW indexes and Full-Text Search GIN indexes.

---

## 4. Evaluation & Benchmark Rules
- All major retrieval or prompt adjustments must be validated against the **300-Sample Golden Benchmark** across its 7 categories:
  1. Simple Factual (25%)
  2. Semantic Retrieval (15%)
  3. Multi-Document Reasoning (20%)
  4. Multi-Hop Questions (15%)
  5. Numerical / Table QA (10%)
  6. Unanswerable Questions (10%)
  7. Permission-Sensitive Questions (5%)
- Continuous Integration:
  - PR Smoke Benchmark: 30 dev samples, runtime $\le 3\text{ minutes}$.
  - Nightly Benchmark: Full 300 test samples measuring Recall@5/10, MRR@10, nDCG@10, and Faithfulness.
- Never use the blind Test split (50%) for hyperparameter tuning or prompt iteration.
