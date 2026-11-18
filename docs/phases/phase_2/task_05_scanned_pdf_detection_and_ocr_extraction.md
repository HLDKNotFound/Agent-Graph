# Task 05: Scanned PDF Detection and OCR Extraction – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, PyMuPDF 2.1, RapidOCR ONNX Runtime 1.4.4, Pydantic v2, PostgreSQL 16+, Prisma ORM)  
**Phase**: Phase 2 – Heterogeneous Document Extraction (Stage 2: Format Extractors)  
**Subsystems**:
- [`backend/ingestion/extraction/ocr.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py)
- [`backend/ingestion/extraction/pdf.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py)
- [`backend/ingestion/config.py`](file:///home/mercury/AgentGraph/backend/ingestion/config.py)
- [`backend/ingestion/types/coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py)
- [`backend/ingestion/extraction/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/__init__.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)
- [`.env.example`](file:///home/mercury/AgentGraph/.env.example)
- [`tests/test_scanned_pdf_ocr_extraction.py`](file:///home/mercury/AgentGraph/tests/test_scanned_pdf_ocr_extraction.py)

---

## 1. Executive Summary & Objective

The primary objective of **Stage 2 (Heterogeneous Document Extraction) – Task 05** was to implement a robust, offline-capable scanned document detection and optical character recognition (OCR) extraction pipeline. This subsystem detects scanned or image-based PDF pages, renders them into in-memory images at configurable DPI, applies OCR only to pages that require it, accurately projects bounding boxes back to PDF point coordinates, reconstructs reading order, handles Vietnamese and English documents, and isolates per-page errors without failing entire documents.

### Mandatory Requirements & Acceptance Criteria Satisfied
1. **Offline OCR Engine Implementation**: Implemented [`RapidOCREngine`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L90-L195) leveraging `rapidocr-onnxruntime` with local ONNX models for fast, offline, and high-accuracy inference. Provided pluggable [`TesseractOCREngine`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L198-L270) with automatic availability detection.
2. **Interface Reuse (Task 04 Contract)**: Implemented [`ScannedPDFExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py#L868-L905) extending [`NativePDFExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py#L60-L100), fulfilling the `DocumentExtractor` abstract contract and emitting normalized `ExtractionResult` outputs.
3. **Multi-Criteria Scanned Page Detection**: Designed [`ScannedPageDetector`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L318-L410) evaluating digital character count ($< 50$ chars configurable), embedded raster image presence (`page.get_images()`), and page surface coverage ratio ($\ge 40\%$).
4. **Adaptive Page-Level Gating**: Native digital text pages bypass OCR completely ("Native text pages do not unnecessarily invoke OCR"), eliminating redundant computational overhead and latency.
5. **In-Memory Page Rasterization**: Implemented [`render_pdf_page_to_image`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L413-L445) rasterizing pages at configurable DPI (`OCR_DPI`, default 300) directly into in-memory RGB NumPy arrays with zero disk I/O.
6. **Provenance Coordinates & Confidence Attribution**: Extracted text blocks, 1-based page numbers, spatial bounding boxes projected to PDF point coordinates (`[0, width]` $\times$ `[0, height]`), and normalized confidence scores $\in [0.0, 1.0]$.
7. **Reading Order & Layout Reconstruction**: Grouped adjacent OCR blocks into cohesive paragraphs and classified numbered/titled headings (`HeadingElement` and `ParagraphElement`).
8. **Vietnamese & English Language Support**: Handled English and Vietnamese enterprise documents with full diacritic preservation.
9. **Environment-Driven Configuration**: Integrated `OCR_ENABLED`, `OCR_ENGINE`, `OCR_DPI`, `OCR_LANGUAGES`, `OCR_SCANNED_CHAR_THRESHOLD`, `OCR_IMAGE_COVERAGE_THRESHOLD`, and `OCR_MIN_CONFIDENCE` into `IngestionSettings` and `.env.example`.
10. **Per-Page Error Isolation**: Individual page OCR failures are caught, logged, and isolated using fallback `PageElement` instances with `ocr_failed: True`, ensuring the document continues processing.
11. **Normalized Extraction Contracts**: Integrated with `compute_canonical_offsets()` and `validate_traceability()`, maintaining $[char\_start, char\_end)$ slices against `raw_text`.
12. **Zero External API / Cloud Dependencies**: 100% self-contained offline execution with local ONNX models.

---

## 2. Architecture & Subsystem Design

```
                     ┌───────────────────────────────────────┐
                     │ Raw PDF Content (Bytes, Stream, Path) │
                     └───────────────────┬───────────────────┘
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ ScannedPDFExtractor / NativePDF       │
                     │ ├── Security & Password Validation    │
                     │ ├── Document Integrity Verification   │
                     │ └── Body Font Size Profiling          │
                     └───────────────────┬───────────────────┘
                                         │
                    ┌────────────────────┴────────────────────┐
                    │  Page-by-Page Inspection Loop (1..N)    │
                    │                                         │
                    │  Digital Text & Table Extraction        │
                    │                   │                     │
                    │                   ▼                     │
                    │     ScannedPageDetector.detect()        │
                    │          /                 \            │
                    │  [Native Page]        [Scanned Page]    │
                    │         │                   │           │
                    │ Native Layout Sorter  Render Page (DPI) │
                    │ (Zero OCR Overhead)         │           │
                    │                             ▼           │
                    │                    RapidOCR / Tesseract │
                    │                    (Pixel -> PDF Points)│
                    │                             │           │
                    │                    OCR Reading Order    │
                    │                    & Heading Classifier │
                    │                             │           │
                    │  [Page Error] ──────────────┤           │
                    │  Fallback PageElement       │           │
                    └────────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ ExtractionResult                      │
                     │ ├── elements (H, P, T, L, Page)       │
                     │ ├── metadata.custom_attributes        │
                     │ │   ├── potential_scanned_pages       │
                     │ │   ├── ocr_pages                     │
                     │ │   └── ocr_applied                   │
                     │ ├── warnings (Isolated OCR errors)    │
                     │ └── stats (ocr_applied, total_pages)  │
                     └───────────────────┬───────────────────┘
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ Canonical Offsets & Traceability      │
                     │ ├── compute_canonical_offsets()       │
                     │ └── validate_traceability(strict=True)│
                     └───────────────────────────────────────┘
```

---

## 3. Key Implementation Details

### 3.1. Pluggable OCR Engine Layer ([ocr.py](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L55-L270))

The OCR engine abstraction isolates physical inference drivers from ingestion orchestration:
- **`RapidOCREngine`**: Executes PP-OCRv4 ONNX models packaged within the environment. Transforms four-point pixel polygon coordinates $[(px_0, py_0), (px_1, py_1), (px_2, py_2), (px_3, py_3)]$ into PDF point bounding boxes:
  $$x_0 = \frac{\min(px)}{\text{scale}_x}, \quad y_0 = \frac{\min(py)}{\text{scale}_y}, \quad x_1 = \frac{\max(px)}{\text{scale}_x}, \quad y_1 = \frac{\max(py)}{\text{scale}_y}$$
- **`TesseractOCREngine`**: Integrates with `pytesseract` when available, reading tabular positional data and scaling word bounding boxes to PDF points.
- **Factory & Caching**: `get_ocr_engine()` dynamically resolves, instantiates, and caches engine instances based on runtime parameters.

### 3.2. Scanned Page Detection Heuristics ([ocr.py](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L318-L410))

To avoid unnecessary OCR on high-volume digital documents, `ScannedPageDetector` implements a multi-stage gate:
1. **Digital Table Presence**: If the page contains structured digital tables and text $\ge \text{threshold}$, it is classified as native.
2. **Text Count Threshold**: If digital character count $\ge 50$ (configurable via `OCR_SCANNED_CHAR_THRESHOLD`), the page is classified as native.
3. **Image Surface Area Ratio**: For low-text pages ($< 50$ chars), the detector scans `page.get_images()`, aggregates image rectangles via `page.get_image_rects(xref)`, and computes:
   $$\text{Coverage} = \min\left(\frac{\sum (\text{rect.width} \times \text{rect.height})}{\text{page.width} \times \text{page.height}}, 1.0\right)$$
   If coverage $\ge 40\%$ (configurable via `OCR_IMAGE_COVERAGE_THRESHOLD`) or digital characters $= 0$, the page is flagged as scanned.

### 3.3. In-Memory Rasterization & Coordinate Projection ([ocr.py](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L413-L445))

PDF rasterization uses PyMuPDF pixmaps rendered directly from memory:
```python
zoom = dpi / 72.0
matrix = pymupdf.Matrix(zoom, zoom)
pix = page.get_pixmap(matrix=matrix, alpha=False)
img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
```
Scale factors (`scale_x = pix.width / page.rect.width`, `scale_y = pix.height / page.rect.height`) map bounding boxes back to point coordinates, ensuring provenance parity with digital PDF elements.

### 3.4. Reading Order & Layout Reconstruction ([ocr.py](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py#L448-L560))

Raw OCR outputs often fragment sentences across line boxes. `sort_and_classify_ocr_blocks`:
1. Spatially sorts recognized blocks by $(y_0, x_0)$ within vertical band tolerances.
2. Groups adjacent lines into paragraph units when the vertical gap $< 1.6 \times \text{line\_height}$ and horizontal alignment is consistent.
3. Classifies numbered section headings (`OCR_NUMBERED_HEADING_PATTERN`) and uppercase titles (`OCR_ALL_CAPS_HEADING_PATTERN`) into `HeadingElement` instances, propagating `section_heading` to descendant paragraphs.

### 3.5. Isolated Page Error Handling ([pdf.py](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py#L220-L260))

When an individual page encounters an OCR failure (e.g., corrupted image stream, decoding anomaly):
- The exception is trapped in a per-page `try ... except` block.
- A descriptive warning is recorded in `ExtractionResult.warnings`.
- A fallback `PageElement` with `metadata={"ocr_failed": True, "error": str(err)}` preserves page existence and provenance.
- Remaining pages continue processing without terminating document ingestion.

---

## 4. Implementation Inventory

| File Path | Purpose | Key Additions / Modifications |
| :--- | :--- | :--- |
| [`backend/ingestion/extraction/ocr.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/ocr.py) | OCR Engine & Detector Subsystem | Implemented `OCREngine`, `RapidOCREngine`, `TesseractOCREngine`, `ScannedPageDetector`, `render_pdf_page_to_image`, and `sort_and_classify_ocr_blocks` |
| [`backend/ingestion/extraction/pdf.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/pdf.py) | PDF Extractor Adapters | Integrated OCR execution loop into `NativePDFExtractor`, implemented `ScannedPDFExtractor`, and per-page failure isolation |
| [`backend/ingestion/config.py`](file:///home/mercury/AgentGraph/backend/ingestion/config.py) | Dynamic Settings | Added `OCR_ENABLED`, `OCR_ENGINE`, `OCR_DPI`, `OCR_LANGUAGES`, `OCR_SCANNED_CHAR_THRESHOLD`, `OCR_IMAGE_COVERAGE_THRESHOLD`, `OCR_MIN_CONFIDENCE` |
| [`backend/ingestion/types/coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py) | Provenance Coordinates | Added `width` and `height` convenience properties to `BoundingBox` |
| [`backend/ingestion/extraction/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/__init__.py) | Extraction Package Facade | Exported `ScannedPDFExtractor`, `OCREngine`, `RapidOCREngine`, `TesseractOCREngine`, `get_ocr_engine`, `ScannedPageDetector`, `OCRBlock` |
| [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py) | Subsystem Root Facade | Re-exported `ScannedPDFExtractor` and OCR engine types at ingestion root level |
| [`.env.example`](file:///home/mercury/AgentGraph/.env.example) | Configuration Template | Documented OCR configuration variables with descriptions and safe defaults |
| [`.env`](file:///home/mercury/AgentGraph/.env) | Local Environment | Updated with default OCR configuration keys |
| [`tests/test_scanned_pdf_ocr_extraction.py`](file:///home/mercury/AgentGraph/tests/test_scanned_pdf_ocr_extraction.py) | Unit Test Suite | 14 comprehensive unit tests verifying detection heuristics, scaling, RapidOCR, mixed documents, Vietnamese text, and failure isolation |

---

## 5. Verification & Test Suite Results

All unit tests executed under Conda environment `ml-env` (Python 3.12.16):

```bash
conda run -n ml-env pytest tests/test_scanned_pdf_ocr_extraction.py -v
```

### Test Suite Execution Summary
- `TestScannedPDFDetection::test_detection_digital_page`: **PASSED** (Verified native text pages bypass scanned flag)
- `TestScannedPDFDetection::test_detection_image_scanned_page`: **PASSED** (Verified embedded image page flagged as scanned)
- `TestScannedPDFDetection::test_detection_empty_page`: **PASSED** (Verified 0-text, 0-image page flagged as scanned/blank)
- `TestScannedPDFDetection::test_detection_native_table_page`: **PASSED** (Verified native digital table page preserved without OCR)
- `TestPageImageRendering::test_render_pdf_page_to_image_scaling`: **PASSED** (Verified DPI scaling and coordinate ratios at 150 & 300 DPI)
- `TestScannedPDFOCRExtraction::test_scanned_pdf_extraction_rapidocr`: **PASSED** (Verified end-to-end OCR extraction, bounding boxes, confidence, canonical offsets)
- `TestScannedPDFOCRExtraction::test_native_pdf_skips_ocr`: **PASSED** (Verified digital PDF skips OCR engine completely)
- `TestScannedPDFOCRExtraction::test_mixed_pdf_adaptive_ocr`: **PASSED** (Verified hybrid PDF executes OCR only on scanned page 2; pages 1 and 3 remain native)
- `TestScannedPDFOCRExtraction::test_vietnamese_text_ocr_extraction`: **PASSED** (Verified Vietnamese text recognition with confidence scores)
- `TestScannedPDFOCRExtraction::test_isolated_page_ocr_failure`: **PASSED** (Verified per-page failure isolation preserves other pages and records warning)
- `TestScannedPDFOCRExtraction::test_extractor_registry_resolution_prefer_ocr`: **PASSED** (Verified registry resolves `ScannedPDFExtractor` when `prefer_ocr=True`)
- `TestScannedPDFOCRExtraction::test_native_pdf_extractor_enable_ocr_flag`: **PASSED** (Verified `enable_ocr` toggle on `NativePDFExtractor`)
- `TestScannedPDFOCRExtraction::test_configurable_ocr_settings_from_environment`: **PASSED** (Verified `IngestionSettings` resolution)
- `TestScannedPDFOCRExtraction::test_tesseract_engine_graceful_availability`: **PASSED** (Verified graceful fallback when Tesseract binary is absent)

**Task 05 Results**: `14 passed in 11.00s`  
**Full Subsystem Regression Suite (`pytest tests/`)**: `125 passed in 15.64s` (0 failures, 0 regressions across all Phase 1 and Stage 2 test suites).

---

## 6. Forward Integration Points

1. **Word & Office Document Extraction (Task 06 / Task 07)**: The pluggable `DocumentExtractor` interface and `ExtractorRegistry` architecture establish the design pattern for DOCX (`python-docx`) and XLSX/CSV (`openpyxl` / `pandas`) extractors.
2. **Recursive Structural Chunking**: Elements produced by `ScannedPDFExtractor` (`HeadingElement`, `ParagraphElement`, `PageElement`) carry valid spatial bounding boxes, canonical string slice offsets $[char\_start, char\_end)$, and hierarchical `section_heading` associations, directly compatible with subsequent chunking and embedding pipelines.
3. **Audit Logging & Telemetry**: Extracted OCR telemetry (`stats.ocr_applied`, `metadata.custom_attributes["ocr_pages"]`, `metadata.custom_attributes["potential_scanned_pages"]`) provides metrics for Langfuse tracing and Prometheus monitoring.
