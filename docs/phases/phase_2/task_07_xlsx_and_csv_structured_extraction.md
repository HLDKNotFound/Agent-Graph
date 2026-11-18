# Task 07: XLSX and CSV Structured Extraction – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, `openpyxl` 3.1.5, `et-xmlfile` 2.0.0, Python `csv`, Pydantic v2, PostgreSQL 16+, Prisma ORM)  
**Phase**: Phase 2 – Heterogeneous Document Extraction (Stage 2: Format Extractors)  
**Subsystems**:
- [`backend/ingestion/extraction/spreadsheet.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py)
- [`backend/ingestion/extraction/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/__init__.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)
- [`backend/ingestion/config.py`](file:///home/mercury/AgentGraph/backend/ingestion/config.py)
- [`backend/ingestion/types/context.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/context.py)
- [`tests/test_spreadsheet_extraction.py`](file:///home/mercury/AgentGraph/tests/test_spreadsheet_extraction.py)

---

## 1. Executive Summary & Objective

The primary objective of **Stage 2 (Heterogeneous Document Extraction) – Task 07** was to implement production-grade, structured spreadsheet content extraction for Microsoft Excel (`.xlsx`) and Comma-Separated Values (`.csv`) files while strictly preserving worksheet lineage and row-level source provenance.

The subsystem extracts multi-worksheet workbooks, preserves exact 1-based original physical row numbers (including title headers, column headers, and empty gaps), maintains column-to-value key-value mappings, expands merged cell regions, normalizes diverse cell data types, provides memory-bounded streaming for massive files, and follows a clearly documented formula evaluation and cached value fallback policy.

### Mandatory Requirements & Acceptance Criteria Satisfied

1. **`openpyxl` & CSV Extraction Engines**: Implemented [`XlsxExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py#L118) and [`CsvExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py#L517) (with composite router [`SpreadsheetExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py#L740) and alias `OpenXMLXlsxExtractor`) conforming to the `DocumentExtractor` interface and registered with the pluggable [`default_extractor_registry`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/registry.py#L120).
2. **Multiple Worksheets Support**: Iterates through all worksheets in a workbook, preserving individual worksheet names (`sheet_name`), sheet counts, and per-sheet bounding coordinates.
3. **Physical 1-Based Row Numbers & Gaps**: Preserves original 1-based physical worksheet row coordinates (`row_index`) on all extracted records. Gaps resulting from empty rows between data blocks are explicitly tracked in metadata (`gaps: [5, 7]`) and do not cause subsequent rows to be renumbered.
4. **Header Row Detection & Disambiguation**: Skips sheet title rows, auto-detects header row positions, fills blank column headers (`Column_2`), and disambiguates duplicated header names (`Total`, `Total_2`).
5. **Merged Cell Value Propagation**: Traverses `ws.merged_cells.ranges` via [`_build_merged_cells_map()`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py#L427) to map top-left values across all covered coordinates `(row, col)`, preventing data holes in rows covered by spanning category headers.
6. **Data Type Normalization**: Converts primitive and temporal cell data types:
   - Integers and floats (whole floats normalized to `int`).
   - Dates, datetimes, and times formatted to ISO 8601 strings (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`).
   - Booleans (`True`/`False`).
   - Text strings stripped of whitespace.
7. **Column-to-Value Key Mapping**: Emits [`SpreadsheetRowElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L398) with `cells: dict[str, Any]` mapping column header to row cell value, preserving column-to-value associations for vector search and citation badges.
8. **Normalized Tabular Model**: Emits [`TableElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L327) containing headers, body row matrices, and GitHub Flavored Markdown table representations.
9. **Documented Formula Policy**: Documented in [`FORMULA_POLICY_DOCUMENTATION`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py#L65). Extracts cached evaluation results by default (`data_only=True`), falling back to literal formula expressions (`=SUM(...)`) when cached evaluations are missing.
10. **Memory-Bounded Streaming**: Automatically activates `read_only=True` XML pull-parser streaming mode for files exceeding [`spreadsheet_streaming_threshold_bytes`](file:///home/mercury/AgentGraph/backend/ingestion/config.py#L96) (default: 10MB) or when bounded by [`spreadsheet_max_rows_per_sheet`](file:///home/mercury/AgentGraph/backend/ingestion/config.py#L91) (default: 50,000).
11. **Security & Integrity Checks**:
    - Rejects 0-byte files with [`EmptyFileError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L55).
    - Detects password-protected / encrypted workbooks (OLE `EncryptedPackage`) and raises [`EncryptedDocumentError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L25).
    - Detects binary null bytes in CSV files and raises [`FileSignatureMismatchError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L65).
    - Detects mismatched formats (e.g. DOCX renamed to XLSX).
12. **Zero Embeddings or Direct DB Writes**: Strictly enforces extraction responsibilities; chunking, embeddings, and Qdrant persistence are decoupled.

---

## 2. Architecture & Subsystem Design

```
                      ┌──────────────────────────────────────────────┐
                      │ Raw Spreadsheet (Bytes, Stream, or File Path)│
                      └──────────────────────┬───────────────────────┘
                                             │
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │ Pluggable Registry Dispatcher                │
                      │ ├── XlsxExtractor (XLSX, SpreadsheetML)      │
                      │ ├── CsvExtractor  (CSV, TSV, DSV)            │
                      │ └── SpreadsheetExtractor (Unified Adapter)   │
                      └──────────────────────┬───────────────────────┘
                                             │
                   ┌─────────────────────────┴────────────────────────┐
                   │                                                  │
                   ▼                                                  ▼
     ┌───────────────────────────┐                      ┌───────────────────────────┐
     │ XLSX Extraction Pipeline  │                      │ CSV Extraction Pipeline   │
     │ ├── File Integrity & OLE  │                      │ ├── Null Byte & Exe Check │
     │ ├── Threshold Check       │                      │ ├── Multi-Encoding Decode │
     │ │   ├── Normal Mode       │                      │ └── Delimiter Sniffing    │
     │ │   │   ├── Merged Cells  │                      └─────────────┬─────────────┘
     │ │   │   └── Formula Fallbk│                                    │
     │ │   └── Streaming Mode    │                                    │
     │ │       └── iter_rows()   │                                    │
     │ └── Multi-Sheet Traversal │                                    │
     └─────────────┬─────────────┘                                    │
                   │                                                  │
                   └─────────────────────────┬────────────────────────┘
                                             │
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │ Tabular Normalization & Provenance           │
                      │ ├── Header Row Detection & Deduplication     │
                      │ ├── 1-Based Physical Row Numbers (Gaps)      │
                      │ ├── Primitive & ISO Date Type Normalization  │
                      │ ├── TableElement (Markdown Table Matrix)     │
                      │ └── SpreadsheetRowElement (Key-Value Dict)   │
                      └──────────────────────┬───────────────────────┘
                                             │
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │ ExtractionResult                             │
                      │ ├── elements (TableElement, SpreadsheetRow)  │
                      │ ├── metadata (sheets, summaries, properties) │
                      │ ├── stats (ExtractionStats telemetry)        │
                      │ ├── compute_canonical_offsets()              │
                      │ └── validate_traceability(strict=True)       │
                      └──────────────────────────────────────────────┘
```

---

## 3. Key Implementation Details

### 3.1 Merged Cell Value Propagation
In Microsoft Excel, a merged range stores its value only in the top-left cell. To ensure that queries against rows covered by spanning category headers (e.g. "Technology" division spanning 5 engineering teams) retrieve the category, [`_build_merged_cells_map()`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py#L427) maps all cells `(r, c)` within each `CellRange` to the top-left cell's value:

```python
for rng in ws.merged_cells.ranges:
    top_left_val = ws.cell(row=rng.min_row, column=rng.min_col).value
    for r in range(rng.min_row, rng.max_row + 1):
        for c in range(rng.min_col, rng.max_col + 1):
            merged_map[(r, c)] = top_left_val
```

### 3.2 Physical Row Coordinates & Gap Preservation
Rows in spreadsheets often contain empty lines or header blocks. Rather than re-indexing rows to 1, 2, 3:
- The header row index is recorded in table metadata (`header_row_index: 3`).
- Data rows retain their physical 1-based worksheet row numbers (`row_index = 4`, `row_index = 6`, `row_index = 8`).
- Blank lines between data rows are tracked as gaps (`gaps: [5, 7]`).

### 3.3 Formula Evaluation and Cached Values Policy
Documented in [`FORMULA_POLICY_DOCUMENTATION`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/spreadsheet.py#L65):
- Workbooks are loaded with `data_only=True` to retrieve Excel's cached evaluated results.
- If a cell contains a formula but Excel never cached the calculation (e.g. generated via programmatic scripts), openpyxl returns `None`. In this case, the extractor inspects the raw formula expression and falls back to `=FORMULA(...)`, annotating metadata to guarantee zero data loss.

### 3.4 Dual Element Emission & Dynamic Configuration
In [`IngestionOptions`](file:///home/mercury/AgentGraph/backend/ingestion/types/context.py#L16):
- `emit_table_elements: bool = True`: Emits [`TableElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L327) with full markdown table representation.
- `emit_row_elements: bool = True`: Emits [`SpreadsheetRowElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L398) with row-level `cells` dictionaries.
- `max_rows_per_sheet: int | None = None`: Configurable row cap preventing out-of-memory errors on massive spreadsheets.

---

## 4. Verification & Testing

### 4.1 Unit Test Suite (`tests/test_spreadsheet_extraction.py`)
Executed via pytest in the `ml-env` Conda environment:
```bash
conda run -n ml-env pytest tests/test_spreadsheet_extraction.py
```
**Result**: **29 passed in 1.13s**

### 4.2 Full Regression Suite
```bash
conda run -n ml-env pytest tests/
```
**Result**: **181 passed in 21.63s** (0 failures, 100% passing across the entire ingestion and storage test suite)

### Test Coverage Highlights
| Test Class | Focus Areas Covered |
| :--- | :--- |
| `TestSpreadsheetExtractorRegistry` | Registration by `DocumentType.XLSX`, `DocumentType.CSV`, MIME types, and aliases |
| `TestMultiWorksheetExtraction` | Multi-sheet extraction, sheet names, per-sheet table elements, and row counts |
| `TestOriginalRowNumbersAndGapsPreservation` | Physical 1-based row numbers, header row index, and blank row gap tracking |
| `TestMergedCellsHandling` | Vertical and horizontal merged cell value propagation across spanned rows |
| `TestHeterogeneousDataTypes` | Primitives (int, float), booleans, temporal types (date, datetime, time) |
| `TestHeaderDisambiguationAndMissingValues` | Disambiguation of duplicate column headers (`Metric_2`) and empty headers (`Column_2`) |
| `TestFormulaEvaluationPolicy` | Fallback to formula strings when cached evaluations are missing; policy export |
| `TestStreamingAndMemoryManagement` | `read_only=True` XML pull-parsing streaming mode and row truncation bounds |
| `TestCsvExtractor` | Standard CSV, quoted commas, TSV delimiter sniffing, and physical line gaps |
| `TestTraceabilityAndCanonicalOffsets` | Strict `validate_traceability()` and canonical stream offset slicing |
| `TestErrorHandlingAndEdgeCases` | Rejection of 0-byte files, corrupt archives, encrypted OLE packages, null bytes |

---

## 5. Modified & Created Files Summary

- **Created**:
  - `backend/ingestion/extraction/spreadsheet.py`
  - `tests/test_spreadsheet_extraction.py`
  - `docs/phases/phase_2/task_07_xlsx_and_csv_structured_extraction.md`
- **Modified**:
  - `backend/ingestion/config.py`
  - `backend/ingestion/types/context.py`
  - `backend/ingestion/extraction/__init__.py`
  - `backend/ingestion/__init__.py`
