# Task 10: Chunk Provenance and Locator Validation – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 19, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, Pydantic v2, PostgreSQL 16+, Prisma ORM 5.22.0, Qdrant)  
**Phase**: Phase 2 / Stage 3 – Deduplication and Chunking (Task 10)  
**Subsystems**:
- [`backend/ingestion/validation/provenance.py`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py)
- [`backend/ingestion/validation/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/validation/__init__.py)
- [`backend/ingestion/types/coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py)
- [`backend/ingestion/exceptions.py`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py)
- [`backend/ingestion/chunking/engine.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/engine.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)
- [`tests/test_chunk_provenance_validation.py`](file:///home/mercury/AgentGraph/tests/test_chunk_provenance_validation.py)

---

## 1. Executive Summary & Objective

The primary objective of **Stage 3 (Deduplication and Chunking) – Task 10** was to implement a production-grade, dedicated **Chunk Provenance and Locator Validation Service** (`ChunkProvenanceValidator`). This service guarantees that every generated chunk has verifiable, physically grounded source provenance coordinates prior to relational persistence in PostgreSQL or vector indexing in Qdrant.

In an enterprise Retrieval-Augmented Generation (RAG) system, verifiable line-level citations (e.g. `[Page 3, Section: 3.2 Leave Policy]` or `[Worksheet: Salaries, Rows 14:28]`) depend on coordinates that strictly correspond to actual document boundaries. Task 10 eliminates silent coordinate corruption, validates boundaries across heterogeneous enterprise document formats (`PDF`, `DOCX`, `XLSX`, `CSV`, `TXT`, `MARKDOWN`), distinguishes format-specific field requirements, enforces half-open character intervals, and enables bidirectional round-trip reconstruction between relational database records, vector payloads, and original source locations.

### Mandatory Requirements & Acceptance Criteria Satisfied

1. **Dedicated Provenance Validation Service**:
   - Implemented [`ChunkProvenanceValidator`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L182) as a standalone, reusable service with singleton [`default_provenance_validator`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L510).
2. **Comprehensive Locator Field Validation**:
   - Strictly validates all six core locator coordinates:
     - `page_number`
     - `section_heading`
     - `sheet_name`
     - `row_range`
     - `char_start`
     - `char_end`
3. **Format-Aware Requirement Distinctions**:
   - Classifies fields per document format into [`FieldRequirement`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L29): `REQUIRED`, `OPTIONAL`, and `NOT_APPLICABLE`.
   - Rejects non-applicable coordinates (e.g. `sheet_name` or `row_range` on PDF/Word documents) rather than silently persisting nonsensical data.
4. **Physical Boundary Verification**:
   - **PDF Pages**: Validates that $1 \le \text{page\_number} \le \text{total\_pages}$. Detects and rejects page 0, negative values, and out-of-bounds references ($page > total\_pages$).
   - **Spreadsheet Rows**: Parses physical 1-based worksheet row ranges (`"Row 14"`, `"Row 15:30"`, `"15:30"`). Verifies $1 \le \text{start\_row} \le \text{end\_row} \le \text{max\_worksheet\_rows}$.
   - **Worksheet Names**: Validates that `sheet_name` exists in the workbook's detected sheet catalog (`sheet_names`).
   - **Character Offsets**: Enforces 0-indexed half-open intervals $[char\_start, char\_end)$ satisfying $0 \le char\_start \le char\_end \le \text{len(canonical\_text)}$.
5. **Zero Silent Coordinate Loss & Strict Exception Handling**:
   - Chunks devoid of any locator coordinates violate the verifiable provenance invariant and are rejected.
   - Raises [`ProvenanceValidationError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L75) containing complete diagnostic metadata (`document_id`, `chunk_id`, `chunk_index`, `field`, `invalid_value`, `details`).
   - Supports non-strict batch evaluation generating structured [`ProvenanceReport`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L76) and [`ChunkValidationOutcome`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L58).
6. **Source Location Reconstruction**:
   - Generates standardized citation badges (`format_citation()`) conforming to Phase 1 citation standards.
   - Slices exact underlying text from the canonical source document text stream.
7. **Bidirectional Persistence Preservation**:
   - Verified that all coordinates survive transformation to PostgreSQL relational records (`to_db_record()`) and Qdrant zero-trust vector payloads (`to_qdrant_payload()`).
   - Added deserialization helpers [`ChunkLocator.from_db_record`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py#L219) and [`ChunkLocator.from_qdrant_payload`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py#L237).
8. **Pipeline Gatekeeper Integration**:
   - Embedded provenance validation directly into [`ChunkingEngine._validate_chunks`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/engine.py#L170), ensuring every chunk generated by any chunking strategy is verified before returning.

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
                         │ ├── Strategy Execution (Sliding, Recursive, Table)     │
                         │ └── Invariant Checks (Tenant ID, Doc ID, SHA-256 Hash) │
                         └───────────────────────────┬────────────────────────────┘
                                                     │
                                                     ▼
                         ┌────────────────────────────────────────────────────────┐
                         │                ChunkProvenanceValidator                │
                         │ ├── DocumentBoundaryContext (Pages, Sheets, Max Rows)  │
                         │ ├── Format Rules (REQUIRED / OPTIONAL / NOT_APPLICABLE)│
                         │ ├── Physical Bounds (Page <= Total, Row <= Max Rows)   │
                         │ ├── Character Slice Boundary (char_start <= char_end)  │
                         │ └── Source Provenance Invariant Check                  │
                         └───────────────┬────────────────────────┬───────────────┘
                                         │                        │
                          [Invalid: Reject Early]       [Valid: Gate Passed]
                                         │                        │
                                         ▼                        ▼
                         ┌───────────────────────┐  ┌─────────────────────────────┐
                         │ProvenanceValidation-  │  │     DocumentChunkInput      │
                         │Error (Diagnostic Info)│  │ ├── to_db_record()          │
                         └───────────────────────┘  │ └── to_qdrant_payload()     │
                                                    └──────────────┬──────────────┘
                                                                   │
                                           ┌───────────────────────┴───────────────────────┐
                                           │                                               │
                                           ▼                                               ▼
                             ┌───────────────────────────┐                   ┌───────────────────────────┐
                             │ PostgreSQL document_chunks│                   │ Qdrant Vector DB Point    │
                             │ ├── page_number           │                   │ ├── source_location       │
                             │ ├── section_heading       │                   │ │   ├── page_number       │
                             │ ├── sheet_name            │                   │ │   ├── row_range         │
                             │ ├── row_range             │                   │ │   └── bounding_box      │
                             │ └── char_start / char_end │                   │ └── acl (tenant, roles)   │
                             └───────────────────────────┘                   └───────────────────────────┘
```

---

## 3. Format-Specific Locator Requirements Matrix

| Document Format | `page_number` | `section_heading` | `sheet_name` | `row_range` | `char_start` / `char_end` |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **PDF** | **REQUIRED** ($1 \le p \le \text{total}$) | OPTIONAL | **NOT_APPLICABLE** (None) | **NOT_APPLICABLE** (None) | OPTIONAL ($0 \le s \le e \le L$) |
| **DOCX** | OPTIONAL ($p \ge 1$) | OPTIONAL | **NOT_APPLICABLE** (None) | **NOT_APPLICABLE** (None) | OPTIONAL ($0 \le s \le e \le L$) |
| **XLSX** | OPTIONAL ($p \ge 1$) | OPTIONAL | **REQUIRED** ($\in \text{sheets}$) | **REQUIRED** ($1 \le r_s \le r_e \le R$) | OPTIONAL ($0 \le s \le e \le L$) |
| **CSV** | **NOT_APPLICABLE** | OPTIONAL | OPTIONAL | **REQUIRED** ($1 \le r_s \le r_e \le R$) | OPTIONAL ($0 \le s \le e \le L$) |
| **TXT / MD** | OPTIONAL | OPTIONAL | **NOT_APPLICABLE** | **NOT_APPLICABLE** | OPTIONAL ($0 \le s \le e \le L$) |

### Coordinate Invariants:
1. **`page_number`**: Must be 1-based ($page \ge 1$). 0 or negative values raise `INVALID_PAGE_COORDINATE`. If document boundary specifies `total_pages`, $page > total\_pages$ raises `PAGE_OUT_OF_BOUNDS`.
2. **`sheet_name`**: For XLSX, must be a non-empty string referencing an existing worksheet name in `sheet_names`. Unknown sheets raise `UNKNOWN_SHEET_NAME`.
3. **`row_range`**: Must adhere to 1-based physical row indexing. Start row and end row must be $\ge 1$, and $start\_row \le end\_row$. References beyond the sheet's detected row count raise `ROW_OUT_OF_BOUNDS`.
4. **`char_start` / `char_end`**: Must be non-negative integers representing a half-open interval $[char\_start, char\_end)$ within canonical UTF-8 text streams. Violations raise `INVALID_CHAR_OFFSETS` or `CHAR_OFFSETS_OUT_OF_BOUNDS`.
5. **Provenance Invariant**: Chunks where all locator fields are `None` violate the traceability contract and raise `MISSING_PROVENANCE`.

---

## 4. Key Implementation Components

### 4.1. `backend/ingestion/validation/provenance.py`
- Defines [`FieldRequirement`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L29) (`REQUIRED`, `OPTIONAL`, `NOT_APPLICABLE`) and [`ProvenanceErrorCode`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L37).
- Implements [`DocumentBoundaryContext`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L112) with automatic boundary extraction from [`ExtractionResult`](file:///home/mercury/AgentGraph/backend/ingestion/types/extraction.py#L36) (pages, sheet names, sheet row summaries, canonical text stream).
- Implements [`ChunkProvenanceValidator`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L182) containing:
  - `validate_locator()`: Checks individual coordinates against rules and physical boundaries.
  - `validate_chunk()`: Validates chunk contracts, boundaries, and tenant isolation.
  - `validate_chunks()`: Batch evaluation returning [`ProvenanceReport`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L76).
  - `reconstruct_source_location()`: Builds [`SourceLocationReconstruction`](file:///home/mercury/AgentGraph/backend/ingestion/validation/provenance.py#L93) with citation badge and canonical text slice.
  - `reconstruct_from_db_record()`: Reconstructs `ChunkLocator` from PostgreSQL record.
  - `reconstruct_from_qdrant_payload()`: Reconstructs `ChunkLocator` from Qdrant vector payload.
  - `verify_provenance_preservation()`: Validates bidirectional serializability.

### 4.2. `backend/ingestion/types/coordinates.py`
- Enhanced [`ChunkLocator`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py#L82) with:
  - `from_db_record(record)`: Deserializes from PostgreSQL row dictionary (snake_case and camelCase support).
  - `from_qdrant_payload(payload)`: Deserializes from Qdrant `source_location` dictionary.
  - `format_citation()`: Produces line-level citation badges (e.g. `[Page 3, Section: Arch]`, `[Worksheet: Salaries, Rows 14:28]`).

### 4.3. `backend/ingestion/exceptions.py`
- Added [`ProvenanceValidationError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L75) subclassing `ValidationError`, carrying `document_id`, `chunk_id`, `chunk_index`, `field`, `invalid_value`, and structured `details`.

### 4.4. `backend/ingestion/chunking/engine.py`
- Updated `_validate_chunks` to invoke `default_provenance_validator.validate_chunks(chunks, extraction_result=extraction_result, strict=True)`. Chunks with invalid provenance fail before persistence.

---

## 5. Verification & Test Results

The test suite [`tests/test_chunk_provenance_validation.py`](file:///home/mercury/AgentGraph/tests/test_chunk_provenance_validation.py) covers all requirements across 21 test cases:
1. `TestFormatSpecificLocatorRules`:
   - `test_pdf_valid_coordinates` (PASSED)
   - `test_pdf_missing_page_number_rejected` (PASSED)
   - `test_pdf_non_applicable_sheet_name_rejected` (PASSED)
   - `test_pdf_non_applicable_row_range_rejected` (PASSED)
   - `test_docx_valid_coordinates_without_page_number` (PASSED)
   - `test_docx_non_applicable_spreadsheet_coordinates_rejected` (PASSED)
   - `test_xlsx_valid_coordinates` (PASSED)
   - `test_xlsx_missing_sheet_name_rejected` (PASSED)
   - `test_csv_valid_coordinates` (PASSED)
2. `TestPhysicalBoundaryConstraints`:
   - `test_page_number_zero_or_negative_rejected` (PASSED)
   - `test_page_number_exceeds_total_pages_rejected` (PASSED)
   - `test_unknown_worksheet_name_rejected` (PASSED)
   - `test_spreadsheet_row_range_out_of_bounds_rejected` (PASSED)
   - `test_spreadsheet_row_range_start_exceeds_end_rejected` (PASSED)
   - `test_char_start_exceeds_char_end_rejected` (PASSED)
   - `test_char_offsets_exceed_canonical_text_rejected` (PASSED)
3. `TestMissingProvenanceInvariants`:
   - `test_completely_empty_locator_rejected` (PASSED)
4. `TestDiagnosticReporting`:
   - `test_non_strict_chunk_validation_returns_outcome` (PASSED)
   - `test_batch_validate_chunks_report` (PASSED)
5. `TestSourceLocationReconstruction`:
   - `test_reconstruct_pdf_source_location` (PASSED)
   - `test_reconstruct_spreadsheet_source_location` (PASSED)
   - `test_convenience_helper_functions` (PASSED)
6. `TestProvenancePreservation`:
   - `test_roundtrip_postgres_db_record` (PASSED)
   - `test_roundtrip_qdrant_vector_payload` (PASSED)
   - `test_verify_provenance_preservation_helper` (PASSED)
7. `TestChunkingEngineProvenanceIntegration`:
   - `test_chunking_engine_validates_chunks_automatically` (PASSED)
   - `test_chunking_engine_rejects_corrupted_coordinates_during_validation` (PASSED)
