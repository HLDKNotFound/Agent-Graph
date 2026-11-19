# Task 09: Structure-Aware Chunking Engine – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 19, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, `tiktoken` 0.8.0, Pydantic v2, PostgreSQL 16+, Prisma ORM 5.22.0)  
**Phase**: Phase 2 / Stage 3 – Deduplication and Chunking (Task 09)  
**Subsystems**:
- [`backend/ingestion/chunking/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/base.py)
- [`backend/ingestion/chunking/tokens.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/tokens.py)
- [`backend/ingestion/chunking/sliding_window.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/sliding_window.py)
- [`backend/ingestion/chunking/recursive.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/recursive.py)
- [`backend/ingestion/chunking/table_aware.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/table_aware.py)
- [`backend/ingestion/chunking/engine.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/engine.py)
- [`backend/ingestion/chunking/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/__init__.py)
- [`backend/ingestion/types/chunking.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/chunking.py)
- [`backend/ingestion/types/enums.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/enums.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)
- [`tests/test_structure_aware_chunking.py`](file:///home/mercury/AgentGraph/tests/test_structure_aware_chunking.py)

---

## 1. Executive Summary & Objective

The primary objective of **Stage 3 (Deduplication and Chunking) – Task 09** was to implement a configurable, production-grade **Structure-Aware Chunking Engine** capable of segmenting heterogeneous enterprise document elements (`PDF`, `DOCX`, `XLSX`, `CSV`, `TXT`, `MARKDOWN`) into retrieval-ready, token-bounded chunks while strictly preserving their structural context, provenance locator coordinates, and zero-trust security metadata.

The chunking engine decouples text segmentation from downstream embedding generation and vector database indexing, supporting three distinct strategies:
1. **`FIXED_SLIDING_WINDOW`**: Token-based sliding window with a mandatory 10% overlap between adjacent windows and subword token boundary preservation.
2. **`RECURSIVE_STRUCTURAL`**: Outline-preserving segmentation prioritizing Markdown heading hierarchy (`H1` $\to$ `H2` $\to$ `H3`), preserving paragraph boundaries, binding headings to associated content, and recursively splitting oversized sections.
3. **`SEMANTIC_TABLE_AWARE`**: Tabular segmentation converting spreadsheet matrices and extracted tables into Markdown tables, repeating worksheet identifiers and column headers across split chunks, and retaining original physical 1-based row numbers.

### Mandatory Requirements & Acceptance Criteria Satisfied

1. **Common Strategy Contract**: Standardized on [`ChunkingStrategy`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/base.py#L13) abstract base class with polymorphic `async def chunk()` and sync `chunk_sync()` execution paths.
2. **Strategy A (`FIXED_SLIDING_WINDOW`)**:
   - Token-based chunking leveraging `tiktoken` (`cl100k_base`) with zero network overhead.
   - Configurable maximum chunk token size (`chunk_size_tokens`).
   - Exact 10% token overlap applied across consecutive chunks (`overlap_tokens = int(round(chunk_size * 0.10))`).
   - Subword token preservation preventing mid-token cuts and avoiding broken multi-byte UTF-8 character sequences.
   - Exact line-level source provenance locator tracking (`page_number`, `section_heading`, `char_start`, `char_end`) mapped across window boundaries.
3. **Strategy B (`RECURSIVE_STRUCTURAL`)**:
   - Outlines structured according to Markdown heading hierarchy (`# H1` $\to$ `## H2` $\to$ `### H3`).
   - Paragraph boundaries preserved with related narrative units grouped under parent headings.
   - Headings are bound to associated content; headings are never emitted as orphan chunks devoid of content.
   - Oversized sections and massive paragraphs are recursively subdivided across structural separators (`\n\n` $\to$ `\n` $\to$ sentences $\to$ words $\to$ tokens).
4. **Strategy C (`SEMANTIC_TABLE_AWARE`)**:
   - Transforms extracted XLSX/CSV tables and individual [`SpreadsheetRowElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L398) records into standard GitHub Flavored Markdown tables.
   - Explicitly embeds worksheet names (e.g. `### Worksheet: Salaries (Row 2:25)`) and column headers into chunk content.
   - Retains original 1-based physical worksheet row numbers in chunk locators (`row_range`), metadata (`start_row`, `end_row`, `original_row_indices`), and content headers.
   - Splits large tables into retrieval-bounded row groups.
   - Repeats column headers on every subsequent table chunk when split.
5. **General Engine & Security Guarantees**:
   - Configurable per document via [`ChunkingConfig`](file:///home/mercury/AgentGraph/backend/ingestion/types/chunking.py#L12) or automated format-aware default heuristics (XLSX/CSV $\to$ table-aware; PDF/DOCX $\to$ recursive structural).
   - Generates deterministic, stable UUID v5 chunk identifiers derived from `(tenant_id, document_id, chunk_index, content_hash)`.
   - Populates SHA-256 content hashes for exact chunk deduplication.
   - Enforces zero-trust RBAC access control payloads matching PostgreSQL schema and Qdrant vector payload filters.
   - Guarantees zero silent content loss across all document formats.

---

## 2. Subsystem Architecture & Execution Flow

```
                               ┌────────────────────────────────────────────────────────┐
                               │   ExtractionResult (Elements, Canonical raw_text, ACL) │
                               └───────────────────────────┬────────────────────────────┘
                                                           │
                                                           ▼
                               ┌────────────────────────────────────────────────────────┐
                               │                     ChunkingEngine                     │
                               │ ├── Strategy Resolution (Explicit or Format Default)   │
                               │ └── Tenant / Document ID Invariant Enforcement         │
                               └───────────────┬───────────────────┬────────────────────┘
                                               │                   │
                     ┌─────────────────────────┴─────────┐         └────────────────────────┐
                     │                                   │                                  │
                     ▼                                   ▼                                  ▼
        ┌─────────────────────────┐         ┌─────────────────────────┐        ┌─────────────────────────┐
        │  FIXED_SLIDING_WINDOW   │         │  RECURSIVE_STRUCTURAL   │        │   SEMANTIC_TABLE_AWARE  │
        │ ├── Tokenize (cl100k)   │         │ ├── Structural Units    │        │ ├── Extract Table/Rows  │
        │ ├── 10% Window Overlap  │         │ ├── H1->H2->H3 Hierarchy│        │ ├── Markdown Table Gen  │
        │ ├── Subword Boundary    │         │ ├── Bind Headings+Body  │        │ ├── Partition Row Groups│
        │ └── Map Char Offsets    │         │ └── Recursive Splitter  │        │ └── Repeat Col Headers  │
        └────────────┬────────────┘         └────────────┬────────────┘        └────────────┬────────────┘
                     │                                   │                                  │
                     └─────────────────────────┬─────────┴──────────────────────────────────┘
                                               │
                                               ▼
                               ┌────────────────────────────────────────────────────────┐
                               │                  DocumentChunkInput                    │
                               │ ├── Deterministic UUID v5 (chunk_id)                   │
                               │ ├── SHA-256 content_hash (64 hex)                      │
                               │ ├── ChunkLocator (page, heading, sheet, row, offsets)  │
                               │ ├── Zero-Trust ACL (tenant_id, roles, depts, users)    │
                               │ └── Metadata (Strategy telemetry, row indices)         │
                               └────────────────────────────────────────────────────────┘
```

---

## 3. Subsystem Implementation Details

### 3.1. `backend/ingestion/chunking/tokens.py`
- Implements cached singleton tokenizer access via `get_tokenizer()` returning `tiktoken.get_encoding("cl100k_base")`.
- Exposes `count_tokens()`, `encode_tokens()`, and `decode_tokens()` with `disallowed_special=()`, ensuring special prompt tokens do not throw unhandled tokenizer exceptions.
- Implements `compute_chunk_hash(content: str)` producing standard SHA-256 64-character hexadecimal digests for exact chunk deduplication.
- Implements `generate_chunk_id(tenant_id, document_id, chunk_index, content_hash)` using RFC 4122 DNS namespace UUID v5, guaranteeing reproducible identifiers across re-indexing runs while conforming to PostgreSQL UUID data types.

### 3.2. `backend/ingestion/chunking/sliding_window.py`
- Implements `FixedSlidingWindowChunker(ChunkingStrategy)`.
- Calculates step size based on 10% overlap:
  $$\text{overlap} = \max\left(0, \min\left(\text{chunk\_size} - 1, \text{round}(\text{chunk\_size} \times \text{overlap\_percentage})\right)\right)$$
  $$\text{step} = \max\left(1, \text{chunk\_size} - \text{overlap}\right)$$
- Operates directly on token ID arrays, avoiding text cuts in the middle of subword tokens or multi-byte UTF-8 character sequences.
- Precomputes cumulative byte offsets of tokens to locate exact character intervals $[char\_start, char\_end)$ within the document's canonical text stream.
- Intersects window intervals with underlying `ExtractedElement`s to inherit `page_number`, `section_heading`, and spreadsheet coordinates across window transitions.

### 3.3. `backend/ingestion/chunking/recursive.py`
- Implements `RecursiveStructuralChunker(ChunkingStrategy)`.
- Normalizes incoming heterogeneous elements into sequential `StructuralUnit` records.
- Tracks outline hierarchy across levels: `current_heading_path = {1: H1, 2: H2, 3: H3}`.
- Enforces heading attachment guardrails: a heading unit is never flushed in isolation without associated content.
- When an individual unit or paragraph exceeds `chunk_size_tokens`, it invokes `_recursively_split_unit()` which cascades through structural separators:
  1. Paragraph boundaries (`\n\n`)
  2. Line boundaries (`\n`)
  3. Sentence boundaries (`[.!?]\s+`)
  4. Word boundaries (`\s+`)
  5. Subword tokens (unbreakable text)
- Prepending parent section headings (`preserve_headers=True`) maintains semantic context for split subsections.

### 3.4. `backend/ingestion/chunking/table_aware.py`
- Implements `SemanticTableAwareChunker(ChunkingStrategy)` (aliased as `TableAwareChunker`).
- Detects `TableElement` matrices and aggregates standalone `SpreadsheetRowElement` records by `sheet_name`.
- Converts row matrices into formatted GitHub Flavored Markdown tables.
- Iterates over table rows, greedily accumulating rows into chunks up to `chunk_size_tokens`.
- On every partitioned table chunk, the worksheet header (`### Worksheet: <name> (Rows <start>:<end>)`) and full column headers (`| Col 1 | Col 2 | ... |\n| --- | --- | ... |`) are repeated.
- Preserves original 1-based physical row indices in locator `row_range` (`Row 2:25`), `metadata["original_row_indices"]`, and `metadata["start_row"]`/`metadata["end_row"]`.
- Falls back to structural chunking for non-tabular content to guarantee zero content loss.

### 3.5. `backend/ingestion/chunking/engine.py`
- Implements `ChunkingEngine` coordinating registered strategies.
- Resolves strategy via `get_strategy()` with case-insensitive string and enum matching (`RECURSIVE_STRUCTURAL`, `FIXED_SLIDING_WINDOW`, `TABLE_AWARE`, `SEMANTIC_TABLE_AWARE`).
- Automatically resolves `SEMANTIC_TABLE_AWARE` for `XLSX` and `CSV` documents unless explicitly overridden by `ChunkingConfig`.
- Enforces post-chunking invariants: sequential 0-indexed `chunk_index`, tenant isolation verification, and SHA-256 content hash validation.
- Provides `chunk_document_sync()` running in an isolated worker thread event loop to safely avoid loop conflicts during synchronous calls.

---

## 4. Test Coverage & Verification

A dedicated unit test suite [`tests/test_structure_aware_chunking.py`](file:///home/mercury/AgentGraph/tests/test_structure_aware_chunking.py) containing **21 comprehensive unit tests** was executed in the `ml-env` environment:

```
tests/test_structure_aware_chunking.py::TestFixedSlidingWindowChunker::test_strategy_type_discriminator PASSED [  4%]
tests/test_structure_aware_chunking.py::TestFixedSlidingWindowChunker::test_sliding_window_10_percent_overlap[asyncio] PASSED [  9%]
tests/test_structure_aware_chunking.py::TestFixedSlidingWindowChunker::test_avoid_splitting_in_middle_of_tokens[asyncio] PASSED [ 14%]
tests/test_structure_aware_chunking.py::TestFixedSlidingWindowChunker::test_sliding_window_source_locator_preservation[asyncio] PASSED [ 19%]
tests/test_structure_aware_chunking.py::TestFixedSlidingWindowChunker::test_sliding_window_empty_document[asyncio] PASSED [ 23%]
tests/test_structure_aware_chunking.py::TestRecursiveStructuralChunker::test_prioritize_heading_hierarchy_h1_h2_h3[asyncio] PASSED [ 28%]
tests/test_structure_aware_chunking.py::TestRecursiveStructuralChunker::test_avoid_separating_headings_from_content[asyncio] PASSED [ 33%]
tests/test_structure_aware_chunking.py::TestRecursiveStructuralChunker::test_group_related_paragraphs_under_parent_headings[asyncio] PASSED [ 38%]
tests/test_structure_aware_chunking.py::TestRecursiveStructuralChunker::test_recursively_split_oversized_sections_and_paragraphs[asyncio] PASSED [ 42%]
tests/test_structure_aware_chunking.py::TestSemanticTableAwareChunker::test_convert_table_to_markdown_with_worksheet_and_headers[asyncio] PASSED [ 47%]
tests/test_structure_aware_chunking.py::TestSemanticTableAwareChunker::test_split_large_table_and_repeat_headers_across_chunks[asyncio] PASSED [ 52%]
tests/test_structure_aware_chunking.py::TestSemanticTableAwareChunker::test_chunk_spreadsheet_row_elements[asyncio] PASSED [ 57%]
tests/test_structure_aware_chunking.py::TestChunkingEngine::test_document_format_aware_strategy_resolution[asyncio] PASSED [ 61%]
tests/test_structure_aware_chunking.py::TestChunkingEngine::test_per_document_strategy_override[asyncio] PASSED [ 66%]
tests/test_structure_aware_chunking.py::TestChunkingEngine::test_to_db_record_and_qdrant_payload_contracts[asyncio] PASSED [ 71%]
tests/test_structure_aware_chunking.py::TestRecursiveStructuralChunker::test_strategy_type_discriminator PASSED [ 76%]
tests/test_structure_aware_chunking.py::TestSemanticTableAwareChunker::test_strategy_type_discriminator PASSED [ 80%]
tests/test_structure_aware_chunking.py::TestChunkingEngine::test_default_strategy_registration PASSED [ 85%]
tests/test_structure_aware_chunking.py::TestChunkingEngine::test_unsupported_strategy_raises_error PASSED [ 90%]
tests/test_structure_aware_chunking.py::TestChunkingEngine::test_deterministic_chunk_identifiers PASSED [ 95%]
tests/test_structure_aware_chunking.py::TestChunkingEngine::test_sync_chunking_wrapper PASSED [100%]
```

### Full Repository Regression Test Run

```bash
conda run -n ml-env pytest
```
**Results**:
- **219 passed, 4 skipped in 19.67s** across all 12 test modules:
  - `tests/test_document_deduplication.py` (17 tests)
  - `tests/test_document_deduplication_db.py` (4 skipped without local postgres service)
  - `tests/test_docx_extraction.py` (27 tests)
  - `tests/test_extraction_models_and_locators.py` (36 tests)
  - `tests/test_file_validation.py` (25 tests)
  - `tests/test_ingestion_architecture.py` (24 tests)
  - `tests/test_native_pdf_extraction.py` (15 tests)
  - `tests/test_scanned_pdf_ocr_extraction.py` (14 tests)
  - `tests/test_spreadsheet_extraction.py` (29 tests)
  - `tests/test_storage_abstraction.py` (11 tests)
  - `tests/test_structure_aware_chunking.py` (21 tests)

---

## 5. Security & Provenance Locator Contracts

Every chunk generated by the engine conforms to the zero-trust persistence contract defined in Phase 1 and Stage 2:

```python
# PostgreSQL Relational Record (document_chunks table)
{
    "id": "c4a9296e-5f91-5f04-893f-c3042459c3cf",
    "tenant_id": "tenant-enterprise-vn-001",
    "document_id": "doc-guideline-2026-uuid",
    "chunk_index": 0,
    "content": "...",
    "content_hash": "a1b2c3d4...",
    "token_count": 92,
    "page_number": 1,
    "section_heading": "System Architecture",
    "sheet_name": null,
    "row_range": null,
    "char_start": 0,
    "char_end": 450,
}

# Qdrant Zero-Trust Vector Payload
{
    "document_id": "doc-guideline-2026-uuid",
    "chunk_index": 0,
    "content": "...",
    "source_location": {
        "page_number": 1,
        "section_heading": "System Architecture",
        "sheet_name": null,
        "row_range": null,
        "char_start": 0,
        "char_end": 450,
    },
    "acl": {
        "tenant_id": "tenant-enterprise-vn-001",
        "roles": ["ENGINEERING_LEAD", "EXECUTIVE"],
        "departments": ["Engineering", "Product"],
        "users": ["user-corp-author-42", "user-reviewer-99"],
    },
    "metadata": {
        "strategy": "RECURSIVE_STRUCTURAL",
        "heading_level": 1,
        "heading_hierarchy": ["System Architecture"],
    }
}
```

---

## 6. Deliverables & Next Steps

### Completed Deliverables
- Strategy A ([`FixedSlidingWindowChunker`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/sliding_window.py#L22)): 10% overlap, subword preservation, coordinate mapping.
- Strategy B ([`RecursiveStructuralChunker`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/recursive.py#L42)): Outline hierarchy (`H1` $\to$ `H2` $\to$ `H3`), paragraph grouping, recursive subdivision.
- Strategy C ([`SemanticTableAwareChunker`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/table_aware.py#L26)): Markdown table rendering, sheet headers, repeated column headers, original row ranges.
- Engine Orchestrator ([`ChunkingEngine`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/engine.py#L27)): Strategy registry, document format heuristics, invariant verification.
- Unit Test Suite ([`tests/test_structure_aware_chunking.py`](file:///home/mercury/AgentGraph/tests/test_structure_aware_chunking.py)): 21 unit tests covering all acceptance criteria.

### Next Steps (Stage 3 – Task 10)
- **Near-Duplicate Detection (MinHash LSH)**: Implement chunk-level MinHash signatures (128 permutation hashes) and LSH index to detect near-duplicate chunk revisions ($Jaccard > 0.92$) prior to embedding generation.
