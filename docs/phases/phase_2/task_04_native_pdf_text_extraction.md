# Task 04: Native PDF Text Extraction – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, PyMuPDF 2.1, Pydantic v2, PostgreSQL 16+, Prisma ORM)  
**Phase**: Phase 2 – Heterogeneous Document Extraction (Stage 2: Format Extractors)  
**Subsystems**:
- [`backend/ingestion/extraction/pdf.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py)
- [`backend/ingestion/extraction/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/__init__.py)
- [`backend/ingestion/exceptions.py`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)
- [`tests/test_native_pdf_extraction.py`](file:///home/mercury/AgentGraph/tests/test_native_pdf_extraction.py)

---

## 1. Executive Summary & Objective

The primary objective of **Stage 2 (Heterogeneous Document Extraction) – Task 04** was to implement a production-grade, high-throughput native PDF extraction adapter using PyMuPDF (`pymupdf`), capable of parsing digital enterprise documents while preserving document structural hierarchy, spatial coordinates, multi-column reading order, heading relationships, and tabular matrices without performing OCR or chunking.

### Mandatory Requirements & Acceptance Criteria Satisfied
1. **Engine Selection**: Implemented [`NativePDFExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py#L42-L474) using **PyMuPDF 2.1**, providing optimized C-level parsing speed, memory safety, and native `find_tables()` support.
2. **Page-by-Page Extraction**: Extracted text page by page, strictly enforcing 1-based page numbers across all element attributes and inner [`ChunkLocator`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py#L71-L125) provenance containers.
3. **Structural Preservation**:
   - Dynamic body font size detection across the document to identify section headings (`HeadingElement` levels 1–6).
   - Cohesive narrative prose preservation in `ParagraphElement`.
   - Embedded table detection via `TableFinder` into normalized `TableElement` representations.
   - List element extraction with bullet and numbering detection in `ListElement`.
   - Hierarchical `section_heading` context tracking across descendant elements.
4. **Scanned & Empty Page Detection**: Monitored character extraction density against a configurable threshold (`scanned_threshold_chars=50`). Pages with little or no extractable text are flagged with audit warnings, tagged in `metadata.custom_attributes["potential_scanned_pages"]`, and represented as `PageElement` with `is_scanned=True`.
5. **Multi-Column Reading Order Reconstruction**: Implemented side-by-side vertical column analysis to order multi-column layouts correctly (Spanning Title $\rightarrow$ Column 1 top-to-bottom $\rightarrow$ Column 2 top-to-bottom $\rightarrow$ Spanning Footer/Footnotes).
6. **Robust Error Boundaries**: Gracefully handles password-protected/encrypted PDFs with [`EncryptedDocumentError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L25-L28), corrupted or malformed documents with [`CorruptDocumentError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L35-L38), and 0-byte inputs with [`EmptyFileError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L50-L53).
7. **Canonical Offsets & Traceability**: Synchronized with `ExtractionResult.compute_canonical_offsets()`, guaranteeing exact half-open $[char\_start, char\_end)$ slices against `raw_text` and passing `validate_traceability()`.
8. **Resource Lifecycle Management**: Guaranteed deterministic disposal of native MuPDF document descriptors in `try ... finally: doc.close()`.
9. **Zero Hardcoding**: Derived tenant IDs, classification levels, and uploader identities dynamically from authenticated `IngestionContext`.

---

## 2. Architecture & Subsystem Design

```
                     ┌───────────────────────────────────────┐
                     │ Raw PDF Content (Bytes, Stream, Path) │
                     └───────────────────┬───────────────────┘
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ NativePDFExtractor (PyMuPDF / fitz)   │
                     │ ├── Security Check (Encryption/PW)    │
                     │ ├── Integrity Check (Corrupt/0-page)  │
                     │ └── Body Font Size Profiling          │
                     └───────────────────┬───────────────────┘
                                         │
                     ┌───────────────────┴───────────────────┐
                     │ Page-by-Page Processing Loop (1..N)   │
                     │                                       │
                     │ 1. Table Detection (page.find_tables) │
                     │ 2. Scanned Page Density Analysis      │
                     │ 3. Side-by-Side Column Band Sorter    │
                     │ 4. Element Classification (H/P/T/L)   │
                     │ 5. Section Heading Tracking           │
                     └───────────────────┬───────────────────┘
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ ExtractionResult                      │
                     │ ├── elements: list[ExtractedElement]  │
                     │ ├── metadata: DocumentMetadata        │
                     │ ├── warnings: list[str]               │
                     │ └── stats: ExtractionStats            │
                     └───────────────────┬───────────────────┘
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ Canonical Offsets & Traceability      │
                     │ ├── preserve_reading_order()          │
                     │ ├── compute_canonical_offsets()       │
                     │ └── validate_traceability()           │
                     └───────────────────────────────────────┘
```

---

## 3. Key Implementation Details

### 3.1. Multi-Column Reading Order Algorithm

In complex enterprise reports, academic articles, and multi-column policies, naive top-to-bottom coordinate sorting incorrectly interleaves left and right column paragraphs ($y_0$-ordering). 

The [`_sort_blocks_reading_order`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py#L389-L448) implementation detects parallel column flows:
1. **Side-by-Side Detection**: Scans block pairs $(b_1, b_2)$ on a page for vertical overlap ($\min(y_{1,1}, y_{1,2}) - \max(y_{0,1}, y_{0,2}) > 5\text{ pt}$) paired with horizontal separation ($x_{1,1} \le x_{0,2} + 15\text{ pt}$).
2. **Vertical Zoning**: Computes the multi-column zone $[Y_{start}, Y_{end}]$.
   - **Top Banner Blocks** ($y_1 \le Y_{start} + 10\text{ pt}$): Full-width titles or executive headers appearing before the columns.
   - **Bottom Banner Blocks** ($y_0 \ge Y_{end} - 10\text{ pt}$): Full-width footers or footnotes appearing after the columns.
   - **Multi-Column Blocks**: Partitioned across the candidate column split $x_{split}$. Left column blocks ($x_0 < x_{split}$) are sorted by $(y_0, x_0)$ top-to-bottom; right column blocks ($x_0 \ge x_{split}$) are sorted by $(y_0, x_0)$ top-to-bottom.
3. **Assembly**: Combines sections into deterministic reading order:
   $$\text{Reading Order} = \text{Top Banners} + \text{Left Column} + \text{Right Column} + \text{Bottom Banners}$$

### 3.2. Dynamic Heading & Section Context Propagation

To facilitate verifiable line-level citations and semantic chunking:
1. **Body Font Baseline**: Profiles font sizes across up to the first 10 document pages, computing the weighted character frequency median/mode (typically 10.0–11.0 pt).
2. **Heading Discriminator**:
   - Short blocks ($< 180$ characters, $\le 2$ lines) with font size ratio $\ge 1.15\times$ body size, bold styling with ratio $\ge 1.0\times$, or section regex matches (`r"^(?:(?:\d+\.)+\d*|[A-Z]\.|\b(?:Section|Chapter|Article)\s+\d+)\s+[A-Z]"`) are instantiated as [`HeadingElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L277-L315).
   - Determines heading levels 1 through 6 based on section numbering depth (e.g., `1.` $\rightarrow$ 1, `1.2` $\rightarrow$ 2, `1.2.3` $\rightarrow$ 3) or font ratio tiers ($\ge 1.6\times \rightarrow 1$, $\ge 1.35\times \rightarrow 2$, $\ge 1.2\times \rightarrow 3$).
3. **Hierarchical Attribution**: Tracks `current_section_heading` across the page stream, propagating parent section headings to all subsequent paragraphs, tables, and lists.

### 3.3. Scanned & Low-Density Page Detection

For heterogeneous enterprise batches mixing digital and scanned pages:
- If a page has fewer than `scanned_threshold_chars` (default: 50 characters), it is flagged as a potential scanned page requiring OCR.
- Flagged pages are appended to `ExtractionResult.warnings` and recorded in `metadata.custom_attributes["potential_scanned_pages"]`.
- Pages with 0 text characters emit a [`PageElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L458-L477) placeholder with `is_scanned=True` to preserve page existence in the element sequence without violating provenance traceability.

### 3.4. Tabular Structure Isolation

- Embedded tables are detected via `page.find_tables()`.
- Extracted into [`TableElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L327-L373) instances with column headers and row matrices.
- Raw text blocks falling inside table bounding boxes are isolated to prevent duplicate paragraph extraction of table cell contents.

---

## 4. Implementation Inventory

| File Path | Purpose | Key Additions / Modifications |
| :--- | :--- | :--- |
| [`backend/ingestion/extraction/pdf.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py) | PDF Extractor Adapter | Implemented `NativePDFExtractor` with PyMuPDF 2.1 integration, layout analysis, font profiling, and table extraction |
| [`backend/ingestion/exceptions.py`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L25-L28) | Domain Exceptions | Added `EncryptedDocumentError` for password-protected / encrypted PDF documents |
| [`backend/ingestion/extraction/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/__init__.py) | Extraction Package Facade | Exported `NativePDFExtractor` alongside `DocumentExtractor` and `ExtractorRegistry` |
| [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py) | Subsystem Root Facade | Re-exported `NativePDFExtractor` and `EncryptedDocumentError` |
| [`tests/test_native_pdf_extraction.py`](file:///home/mercury/AgentGraph/tests/test_native_pdf_extraction.py) | Unit Test Suite | 15 comprehensive unit tests covering multi-page, multi-column, tables, scanned pages, and error conditions |

---

## 5. Verification & Test Suite Results

All tests executed in Conda `ml-env` (Python 3.12.16):

```bash
pytest tests/test_native_pdf_extraction.py -v
```

### Test Suite Execution Summary
- `TestNativePDFExtractorRegistration::test_extractor_registry_resolution`: **PASSED**
- `TestNativePDFExtractorRegistration::test_can_extract_capabilities`: **PASSED**
- `TestMultiPageExtraction::test_extract_multipage_pdf_async[asyncio]`: **PASSED**
- `TestMultiPageExtraction::test_extract_multipage_sync_wrapper`: **PASSED**
- `TestMultiColumnLayoutReconstruction::test_multicolumn_reading_order[asyncio]`: **PASSED** (Verified Title $\rightarrow$ Left Column $\rightarrow$ Right Column $\rightarrow$ Footer reading order)
- `TestTableAndStructuredElements::test_pdf_table_detection[asyncio]`: **PASSED**
- `TestScannedAndEmptyPagesDetection::test_scanned_page_detection_and_warning[asyncio]`: **PASSED**
- `TestRobustErrorHandlingAndEdgeCases::test_empty_pdf_file_rejected[asyncio]`: **PASSED**
- `TestRobustErrorHandlingAndEdgeCases::test_corrupted_pdf_rejected[asyncio]`: **PASSED**
- `TestRobustErrorHandlingAndEdgeCases::test_encrypted_pdf_rejected[asyncio]`: **PASSED**
- `TestRobustErrorHandlingAndEdgeCases::test_unsupported_content_type[asyncio]`: **PASSED**
- `TestRobustErrorHandlingAndEdgeCases::test_nonexistent_path_rejected[asyncio]`: **PASSED**
- `TestFileAndStreamInputs::test_extract_from_binary_stream[asyncio]`: **PASSED**
- `TestFileAndStreamInputs::test_extract_from_temp_file_path[asyncio]`: **PASSED**
- `TestSerializationRoundTrip::test_json_and_dict_serialization_roundtrip[asyncio]`: **PASSED**

**Task 04 Results**: `15 passed in 5.61s`  
**Full Ingestion Regression Suite (`pytest tests -v`)**: `111 passed in 5.28s` (0 failures, 0 regressions)

---

## 6. Forward Integration Points

1. **Scanned PDF OCR (Task 05)**: Pages flagged with `is_scanned=True` or recorded in `potential_scanned_pages` provide the explicit handover coordinates for the OCR pipeline (RapidOCR / Tesseract) without duplicate re-extraction of digital pages.
2. **Chunking Pipeline (Task 06)**: Extracted `HeadingElement`, `ParagraphElement`, and `TableElement` instances carry canonical character offsets $[char\_start, char\_end)$ and parent `section_heading` values ready for recursive structural chunking.
