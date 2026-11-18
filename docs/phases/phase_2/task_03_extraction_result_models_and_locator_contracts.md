# Task 03: Extraction Result Models and Locator Contracts – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, Pydantic v2, PostgreSQL 16+, Prisma ORM)  
**Phase**: Phase 2 – Heterogeneous Document Ingestion & Indexing Pipeline (Stage 1: Ingestion Foundation)  
**Subsystems**:
- [`backend/ingestion/types/elements.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py)
- [`backend/ingestion/types/coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py)
- [`backend/ingestion/types/enums.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/enums.py)
- [`backend/ingestion/types/extraction.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/extraction.py)
- [`backend/ingestion/types/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/__init__.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)

---

## 1. Executive Summary & Objective

The objective of **Stage 1 (Ingestion Foundation) – Task 03** was to establish a normalized, strictly typed representation of extracted document content and enforce uniform locator coordinates and provenance contracts across all upstream extraction adapters (native PDF, scanned OCR, DOCX, XLSX, and CSV).

### Strict Scope Boundaries Adhered To
1. **Normalized Representation**: Established a uniform contract where every extracted element supports 9 core attributes: `content`, `element_type`, `page_number`, `section_heading`, `sheet_name`, `row_range`, `char_start`, `char_end`, and `metadata`.
2. **Explicit Coordinate Semantics**:
   - Page numbers are strictly 1-based ($page \ge 1$; 0 or negative numbers are rejected with `ValidationError`).
   - Spreadsheet row numbers refer to physical worksheet row positions ($row \ge 1$; inverted ranges $start > end$ or $row < 1$ are rejected).
   - Character offsets follow the documented 0-indexed half-open interval $[char\_start, char\_end)$ convention ($char\_start \le char\_end$).
   - Canonical text slicing guarantees that `canonical_text[char_start:char_end] == element.content`.
3. **Comprehensive Element Type Support**:
   - `HEADING`: Hierarchical document outline headers (H1–H6) with markdown heading prefixing.
   - `PARAGRAPH`: Narrative prose paragraphs.
   - `TABLE`: Tabular matrix structures rendered as GitHub Flavored Markdown tables.
   - `LIST`: Bulleted and numbered lists with indentation and sequencing.
   - `TEXT_BLOCK`: Generic and unclassified narrative text blocks.
   - `SPREADSHEET_ROW`: Tabular record maps with worksheet metadata and row provenance.
   - `PAGE`: Physical document boundary containers with nested element tracking.
4. **Reading Order Preservation**: Enforced deterministic reading order through sequence indexing (`reading_order`) and `ExtractionResult.preserve_reading_order()`.
5. **Source Traceability & Honest Validation**: Guaranteed that non-empty elements have verifiable provenance coordinates back to their origin.
6. **Polymorphic Serialization**: Complete dictionary and JSON serialization/deserialization via Pydantic v2 discriminated unions and `TypeAdapter(ExtractedElement)`.

---

## 2. Architecture & Subsystem Design

```
[Raw Document Extractor Adapters (PDF, DOCX, XLSX, CSV, OCR)]
                               │
                               ▼
        ┌──────────────────────────────────────────────┐
        │       ExtractedElement (Polymorphic Union)    │
        │ ├── HeadingElement       (H1-H6)             │
        │ ├── ParagraphElement     (Narrative body)    │
        │ ├── TableElement         (GFM Markdown table)│
        │ ├── ListElement          (Ordered/Unordered) │
        │ ├── TextBlockElement     (Raw text block)    │
        │ ├── SpreadsheetRowElement(Row matrices)      │
        │ └── PageElement          (Page boundaries)   │
        └──────────────────────┬───────────────────────┘
                               │
                               ▼ .to_normalized()
        ┌──────────────────────────────────────────────┐
        │ NormalizedElement                            │
        │ ├── content: str                             │
        │ ├── element_type: ElementType                │
        │ ├── page_number: int | None (1-based)        │
        │ ├── section_heading: str | None              │
        │ ├── sheet_name: str | None                   │
        │ ├── row_range: str | None (1-based physical) │
        │ ├── char_start / char_end (0-indexed [s, e)) │
        │ ├── reading_order: int | None                │
        │ └── metadata: dict[str, Any]                 │
        └──────────────────────┬───────────────────────┘
                               │
                               ▼
        ┌──────────────────────────────────────────────┐
        │ ExtractionResult                             │
        │ ├── preserve_reading_order()                 │
        │ ├── compute_canonical_offsets()              │
        │ ├── to_normalized_elements()                 │
        │ └── validate_traceability()                  │
        └──────────────────────────────────────────────┘
```

---

## 3. Technical Specifications & Shared Data Contracts

### 3.1. Explicit Coordinate Semantics ([`coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py))

| Coordinate Metric | Indexing Convention | Semantics & Validation Rules |
| :--- | :--- | :--- |
| **`page_number`** | `1-based` | $page \ge 1$. Page 1 is the first document page. Zero or negative page numbers raise `ValidationError`. |
| **`row_range`** | `1-based physical` | Physical worksheet row numbers ($row \ge 1$). Formats: `"Row 14"`, `"15:30"`, `"Row 15-30"`. $start \le end$ enforced. |
| **`char_start`, `char_end`** | `0-indexed [s, e)` | Half-open character interval within the canonical text stream. $char\_start \ge 0, char\_end \ge 0$, and $char\_start \le char\_end$. |
| **`BoundingBox`** | Physical / Normalized | Rectangular spatial box where $x_0 \le x_1$ and $y_0 \le y_1$. |

### 3.2. Uniform Normalized Model: `NormalizedElement` ([`elements.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py))

```python
class NormalizedElement(BaseModel):
    element_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str = Field(..., description="Canonical extracted text content")
    element_type: ElementType = Field(..., description="Semantic categorization discriminator")
    page_number: int | None = Field(default=None, ge=1)
    section_heading: str | None = None
    sheet_name: str | None = None
    row_range: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    reading_order: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

- **Two-Way Conversion**: Any concrete extracted element can invoke `.to_normalized()` to produce a `NormalizedElement`. Conversely, `NormalizedElement.to_concrete_element()` reconstructs the typed concrete instance (`HeadingElement`, `TableElement`, `ListElement`, etc.).
- **Source Traceability Gate**: `has_source_traceability()` and `validate_traceability(strict=True)` ensure that every non-empty element is linked to an authorized source location.

### 3.3. Element Type Specializations ([`elements.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py))

1. [`HeadingElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L225): Markdown heading prefixing (`#` through `######`), strict level validation ($1 \le level \le 6$), automatic self-attribution of `section_heading`.
2. [`ParagraphElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L257): Narrative body text with direct `content` bridging.
3. [`TableElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L268): Markdown table serialization from column headers and cell row matrices.
4. [`ListElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L311): Ordered and unordered list serialization.
5. [`TextBlockElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L215): Unstructured raw text block element.
6. [`SpreadsheetRowElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L332): Worksheet row record with automatic `"Row <row_index>"` coordinate generation.
7. [`PageElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L377): Page boundary container supporting dimension metrics and OCR confidence tracking.

### 3.4. Canonical Text Stream & Offset Computation ([`extraction.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/extraction.py))

`ExtractionResult.compute_canonical_offsets(separator="\n\n")`:
- Sequences all elements in preserved reading order ($reading\_order = 0, 1, 2, \dots$).
- Synthesizes the single canonical UTF-8 text stream (`self.raw_text`).
- Calculates the exact $[char\_start, char\_end)$ slice for every element such that:
  $$\text{raw\_text}[\text{char\_start}:\text{char\_end}] \equiv \text{element.content}$$
- Updates inner `ChunkLocator` coordinates synchronously.

---

## 4. Implementation Inventory

| File Path | Component | Changes & Additions |
| :--- | :--- | :--- |
| [`backend/ingestion/types/enums.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/enums.py) | Enums | Added `LIST` and `TEXT_BLOCK` to `ElementType`, implemented case-insensitive `_missing_` lookup |
| [`backend/ingestion/types/coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py) | Coordinates | Added coordinate semantics constants, `parse_row_range()`, 1-based row validation, `slice_canonical_text()`, and `ElementLocator` alias |
| [`backend/ingestion/types/elements.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py) | Elements | Extended `BaseExtractedElement` with 9 core attributes, added `ListElement`, `TextBlockElement`, `NormalizedElement`, traceability validation, and serialization |
| [`backend/ingestion/types/extraction.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/extraction.py) | Extraction | Added `preserve_reading_order()`, `compute_canonical_offsets()`, `to_normalized_elements()`, `validate_traceability()`, and JSON serialization |
| [`backend/ingestion/types/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/__init__.py) | Exports | Exported new element models, locators, and coordinate semantics constants |
| [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py) | Facade | Exported public contracts across root ingestion package |
| [`tests/test_extraction_models_and_locators.py`](file:///home/mercury/AgentGraph/tests/test_extraction_models_and_locators.py) | Test Suite | 36 dedicated unit tests verifying all Acceptance Criteria and edge cases |

---

## 5. Verification & Test Suite Execution

### 5.1. Automated Unit Tests (`pytest`)
All 96 unit tests across the entire test suite executed and passed in 0.34 seconds:
```bash
conda run -n ml-env pytest tests/
```
```
============================= test session starts ==============================
platform linux -- Python 3.12.16, pytest-9.2.0, pluggy-1.7.0
rootdir: /home/mercury/AgentGraph
plugins: anyio-4.16.0, langsmith-0.14.17
collected 96 items

tests/test_extraction_models_and_locators.py ........................... [ 28%]
.........                                                                [ 37%]
tests/test_file_validation.py .........................                  [ 63%]
tests/test_ingestion_architecture.py ........................            [ 88%]
tests/test_storage_abstraction.py ...........                            [100%]

============================== 96 passed in 0.34s ==============================
```

### 5.2. Non-Regression Database Verification
```bash
npx prisma validate --schema backend/schema.prisma
```
Output:
```
The schema at backend/schema.prisma is valid 🚀
```

---

## 6. Next Milestones (Phase 2 Roadmap)

With the normalized extraction result contracts and locator semantics firmly established:
1. **Task 04 – Native PDF & Scanned OCR Extractors**: Implement `NativePDFExtractor` (`pdfplumber` / `PyMuPDF`) and `OCRScannerExtractor` (`RapidOCR` / `Tesseract`), emitting attributed `ExtractedElement` records conforming to Task 03 contracts.
2. **Task 05 – Word (DOCX) & Spreadsheet (XLSX / CSV) Extractors**: Implement OpenXML heading traversers and tabular row-to-markdown serializers.
3. **Task 06 – Structure-Aware Chunking Engine**: Feed normalized elements into `RecursiveStructuralChunker` and `TableAwareChunker`.
