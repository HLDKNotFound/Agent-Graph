# Task 06: Microsoft Word DOCX Extraction – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, `python-docx` 1.2.0, `lxml` 6.1.3, Pydantic v2, PostgreSQL 16+, Prisma ORM)  
**Phase**: Phase 2 – Heterogeneous Document Extraction (Stage 2: Format Extractors)  
**Subsystems**:
- [`backend/ingestion/extraction/docx.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/docx.py)
- [`backend/ingestion/extraction/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/__init__.py)
- [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py)
- [`tests/test_docx_extraction.py`](file:///home/mercury/AgentGraph/tests/test_docx_extraction.py)

---

## 1. Executive Summary & Objective

The objective of **Stage 2 (Heterogeneous Document Extraction) – Task 06** was to implement a production-grade Microsoft Word (`.docx`) document extraction engine using `python-docx`. The subsystem parses Office OpenXML (OOXML) word processing documents, preserves paragraphs, headings, tables, and lists in their exact sequential document body reading order, reconstructs multi-tier heading hierarchies (`Heading 1` through `Heading 6`), tracks section hierarchy breadcrumbs, extracts tabular structures into Markdown representations, maps bulleted and numbered lists, attributes exact canonical character offsets, and detects empty, malformed, and password-protected files.

### Mandatory Requirements & Acceptance Criteria Satisfied

1. **`python-docx` Parsing Engine**: Implemented [`DocxExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/docx.py#L48-L232) (aliased as `OpenXMLDocxExtractor`) conforming to the `DocumentExtractor` abstract base class and registered with the pluggable [`default_extractor_registry`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/registry.py#L120) for `DocumentType.DOCX` and OOXML MIME types.
2. **Document-Order Body Traversal**: Instead of iterating `doc.paragraphs` and `doc.tables` in isolation, the extractor iterates child XML elements of `doc.element.body` (`<w:p>` and `<w:tbl>`), guaranteeing that paragraphs, headings, tables, and lists retain their exact interleaved order as authored.
3. **Multi-Tier Heading Hierarchy**: Detects heading levels (`Heading 1`, `Heading 2`, `Heading 3`, and through `Heading 6`), `Title` (mapped to level 1), `Subtitle` (mapped to level 2), OpenXML paragraph outline levels (`<w:outlineLvl>`), and Markdown structural markers (`# Header`), emitting [`HeadingElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L277-L315) instances.
4. **Section Hierarchy Association**: Tracks an outline stack (`heading_stack`), associating all child paragraphs, tables, and lists with their nearest `section_heading`, full parent hierarchy (`section_hierarchy`), and breadcrumb path (`section_path`), providing rich structural metadata for downstream recursive chunking.
5. **Table Extraction with Structure Preservation**: Traverses rows and cells via `tbl.rows`, handles cell merges safely, extracts column headers and cell values, and formats them into [`TableElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L327-L373) instances with GitHub Flavored Markdown representations.
6. **List Item Extraction**: Identifies bulleted and numbered list items via OpenXML numbering properties (`<w:numPr>`), list styles (`List Bullet`, `List Number`, `List Paragraph`), and text markers, aggregating consecutive items into [`ListElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L374-L397) instances with Markdown syntax.
7. **Canonical Character Offsets**: Slices text strictly against canonical representation via `compute_canonical_offsets(separator="\n\n")`, enforcing $[char\_start, char\_end)$ interval integrity:
   $$\text{raw\_text}[char\_start:char\_end] \equiv \text{element.content}$$
8. **Integrity & Security Validation**:
   - 0-byte input raises [`EmptyFileError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L55-L58).
   - Corrupted/truncated ZIP archives or missing `word/document.xml` raise [`CorruptDocumentError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L40-L43).
   - Password-protected / encrypted Word documents (OLE compound container with `EncryptedPackage`) raise [`EncryptedDocumentError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L25-L28).
   - Legacy binary Word files (`.doc`) raise [`UnsupportedDocumentTypeError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L20-L23).
   - Blank documents (0 extractable elements) generate telemetry warnings with `is_empty=True` metadata.
9. **Normalized Extraction Result**: Emits standardized [`ExtractionResult`](file:///home/mercury/AgentGraph/backend/ingestion/types/extraction.py#L36-L177) objects with telemetry stats, enriched metadata, and full provenance coordinates.
10. **Zero Implementation of Chunking or Embeddings**: Clean boundaries established; chunking and embedding logic are deferred to subsequent pipeline stages.

---

## 2. Architecture & Subsystem Design

```
                     ┌────────────────────────────────────────┐
                     │ Raw DOCX Content (Bytes, Stream, Path) │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │ DocxExtractor                          │
                     │ ├── File Integrity & Magic Byte Check  │
                     │ ├── Encrypted OLE Package Detection    │
                     │ └── ZIP Structure & Part Verification  │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │ Body Element Traversal (doc.element.body)
                     │ ├── <w:p> (Paragraph XML)              │
                     │ │   ├── Heading Classifier (H1 - H6)   │
                     │ │   │   ├── Update Heading Stack       │
                     │ │   │   └── Emit HeadingElement        │
                     │ │   ├── List Item Detector             │
                     │ │   │   ├── Detect numPr / List styles │
                     │ │   │   └── Aggregate ListElement      │
                     │ │   └── Body Prose Paragraph           │
                     │ │       └── Emit ParagraphElement      │
                     │ └── <w:tbl> (Table XML)                │
                     │     ├── Extract Headers & Body Rows    │
                     │     ├── Merged Cell Safe Handling      │
                     │     └── Emit TableElement (Markdown)   │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │ Section Hierarchy Tracking             │
                     │ ├── section_heading (Nearest H)        │
                     │ ├── section_hierarchy ([H1, H2, H3])   │
                     │ └── section_path ("H1 > H2 > H3")      │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │ ExtractionResult                       │
                     │ ├── elements (H, P, T, L)              │
                     │ ├── metadata.core_properties           │
                     │ │   ├── title, author, dates           │
                     │ │   └── revision, total_elements       │
                     │ ├── stats (ExtractionStats)            │
                     │ └── warnings (Empty document flag)     │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │ Canonical Offsets & Traceability       │
                     │ ├── compute_canonical_offsets()        │
                     │ └── validate_traceability(strict=True) │
                     └────────────────────────────────────────┘
```

---

## 3. Key Implementation Details

### 3.1 Document Body Traversal in Reading Order
In standard `python-docx` usage, `doc.paragraphs` and `doc.tables` are segregated collections. To preserve authorial document order where tables and lists appear between paragraphs under specific headings, [`DocxExtractor._extract()`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/docx.py#L90-L232) iterates `doc.element.body`:

```python
for child in doc.element.body:
    tag = child.tag
    if tag.endswith("p"):
        p = Paragraph(child, doc)
        # Process heading, list item, or paragraph
    elif tag.endswith("tbl"):
        tbl = Table(child, doc)
        # Process table
```

### 3.2 Heading Hierarchy & Section Tracking
Heading classification inspects multiple signals:
1. `HEADING_STYLE_PATTERN.match(style_name)` for `"Heading 1"`, `"Heading 2"`, etc.
2. Special case styles: `"Title"` $\rightarrow 1$, `"Subtitle"` $\rightarrow 2$.
3. OpenXML paragraph outline levels: `<w:outlineLvl w:val="0"/>` $\rightarrow$ Level $1$.
4. Text structural regex: Markdown-like syntax (`# Title`, `## Subtitle`).

When a heading of level $L$ is encountered, deeper levels are purged from `heading_stack`, the current level is updated, and all subsequent elements inherit `section_heading`, `section_hierarchy`, and `section_path`.

### 3.3 Table Extraction & Merged Cell Resilience
Tables are converted into standard Markdown tables:
- First row is treated as headers, subsequent rows as data.
- Merged cell exceptions (which occur when accessing `tbl.columns` on asymmetrical tables in `python-docx`) are safely bypassed:
```python
try:
    col_count = len(tbl.columns)
except Exception:
    col_count = len(headers) if headers else 0
```

### 3.4 List Aggregation
Consecutive list paragraphs of the same type (`ordered=True` or `ordered=False`) are aggregated into a single `ListElement` to produce cohesive Markdown list blocks (`- Item` or `1. Item`), flushing when a regular paragraph, heading, or table is reached.

### 3.5 Security & Integrity Checks
Pre-extraction validation protects against malformed files:
- Checks for OLE Compound Document signature (`b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"`). If `EncryptedPackage` is found, raises `EncryptedDocumentError`. Otherwise flags legacy `.doc` files.
- Inspects ZIP header and verifies existence of essential OOXML components (`word/document.xml` or `[Content_Types].xml`).

---

## 4. Verification & Testing

Comprehensive unit tests in [`tests/test_docx_extraction.py`](file:///home/mercury/AgentGraph/tests/test_docx_extraction.py) validate all functional and error-handling requirements:

```bash
pytest tests/test_docx_extraction.py -v
============================== 27 passed in 2.32s ==============================

pytest
============================= 152 passed in 18.06s =============================
```

### Test Coverage Highlights

| Test Category | Covered Scenarios |
| :--- | :--- |
| **Registry Resolution** | Resolves `DocxExtractor` by `DocumentType.DOCX` and MIME type; verifies `OpenXMLDocxExtractor` alias. |
| **Heading Hierarchy** | Verifies nested H1, H2, and H3 level assignments and Markdown prefix rendering. |
| **Section Association** | Confirms paragraphs and lists inherit nearest parent heading and hierarchical breadcrumb paths. |
| **Document Order** | Validates interleaved sequences: Heading $\rightarrow$ Paragraph $\rightarrow$ Table $\rightarrow$ Paragraph $\rightarrow$ List $\rightarrow$ Heading $\rightarrow$ List $\rightarrow$ Paragraph. |
| **Table Extraction** | Checks column header parsing, row matrix contents, Markdown table rendering, and table-only documents. |
| **List Extraction** | Checks bulleted (`-`) and numbered (`1.`) Markdown rendering and item cleaning. |
| **Traceability & Offsets** | Validates that $\text{raw\_text}[char\_start:char\_end] == content$ across all elements with zero tolerance. |
| **Metadata & Telemetry** | Verifies extraction of core properties (title, author, creation date, revision, total element count). |
| **Error Handling** | Tests 0-byte files (`EmptyFileError`), corrupted ZIPs (`CorruptDocumentError`), missing XML (`CorruptDocumentError`), password-protected files (`EncryptedDocumentError`), legacy `.doc` (`UnsupportedDocumentTypeError`), and blank documents. |
| **Input Polymorphism** | Validates identical parsing across `bytes`, `io.BytesIO` streams, and filesystem `Path` references. |
| **Round-Trip Serialization** | Full `to_dict` / `from_dict`, `to_json` / `from_json`, and `to_normalized_elements` round-tripping. |

---

## 5. File Lineage

| File Path | Description |
| :--- | :--- |
| [`backend/ingestion/extraction/docx.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/docx.py) | Complete Microsoft Word DOCX extraction adapter implementation. |
| [`backend/ingestion/extraction/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/__init__.py) | Exported `DocxExtractor` and `OpenXMLDocxExtractor`. |
| [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py) | Root exports for document ingestion subsystem. |
| [`tests/test_docx_extraction.py`](file:///home/mercury/AgentGraph/tests/test_docx_extraction.py) | Unit test suite (27 tests) verifying DOCX extraction and edge cases. |
| [`docs/phases/phase_2/task_06_microsoft_word_docx_extraction.md`](file:///home/mercury/AgentGraph/docs/phases/phase_2/task_06_microsoft_word_docx_extraction.md) | Architectural documentation and phase report. |
