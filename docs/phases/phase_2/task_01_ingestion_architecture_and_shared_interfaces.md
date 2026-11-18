# Task 01: Ingestion Module Architecture & Shared Interfaces – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12, Pydantic v2, PostgreSQL 16+, Prisma ORM)  
**Phase**: Phase 2 – Heterogeneous Document Ingestion & Indexing Pipeline  
**Subsystem**: [`backend/ingestion`](file:///home/mercury/AgentGraph/backend/ingestion)  

---

## 1. Executive Summary & Objective

The objective of **Stage 1 (Ingestion Foundation) – Task 01** was to establish the architectural foundation and shared type contracts for the **Enterprise Document Ingestion Subsystem**. This subsystem processes heterogeneous enterprise documents (PDF native/scanned OCR, DOCX, XLSX, CSV, Markdown, plain text), extracts structured elements with line-level provenance coordinates, enforces multi-tenant zero-trust RBAC access control payloads, and prepares token-bounded chunk records for dual persistence across PostgreSQL 16+ and Qdrant Vector DB.

### Strict Scope Boundaries Adhered To
1. **Zero Premature Parser Logic**: Per task requirements, no concrete PDF, DOCX, XLSX, or OCR parsers were implemented; parser implementations are decoupled and deferred to subsequent tasks.
2. **Phase 1 Infrastructure Preserved**: Existing database schemas ([`backend/schema.prisma`](file:///home/mercury/AgentGraph/backend/schema.prisma)), migrations, and PostgreSQL indexes remain completely untouched and verified (`npx prisma validate` passed).
3. **Open-Closed Extensibility**: Created a thread-safe extractor registry and abstract base class system allowing new document types and OCR strategies to be plugged in dynamically without altering existing orchestration logic.
4. **Zero Hardcoding**: All models and context contracts reject hardcoded tenant IDs, filesystem paths, or role credentials, enforcing dynamic resolution via authenticated context and environment configuration.

---

## 2. Architectural Design & Subsystem Organization

The ingestion subsystem resides under [`backend/ingestion`](file:///home/mercury/AgentGraph/backend/ingestion) as a clean, modular Python package. Business logic is strictly segregated from background worker runtimes (Celery) and HTTP API gateways (FastAPI).

### 2.1. Directory Structure

```
backend/
├── __init__.py                               # Top-level backend Python package marker
└── ingestion/
    ├── __init__.py                           # Central facade exporting all interfaces & types
    ├── exceptions.py                         # Typed domain exception hierarchy
    │
    ├── types/                                # Immutable data contracts & Pydantic schemas
    │   ├── __init__.py                       # Export package for all domain models
    │   ├── enums.py                          # DocumentType, DocumentMimeType, DocumentClassification, etc.
    │   ├── coordinates.py                    # ChunkLocator, BoundingBox (half-open [char_start, char_end))
    │   ├── metadata.py                       # DocumentMetadata, AccessControlPayload
    │   ├── elements.py                       # ExtractedElement Discriminated Union (Text, Heading, Table, Row, Page)
    │   ├── extraction.py                     # ExtractionResult, ExtractionStats
    │   ├── chunking.py                       # DocumentChunkInput, ChunkingConfig
    │   └── context.py                        # IngestionContext, IngestionOptions
    │
    ├── extraction/                           # Extractor contracts & dynamic registry
    │   ├── __init__.py
    │   ├── base.py                           # DocumentExtractor (abstract base class)
    │   └── registry.py                       # ExtractorRegistry & @register_extractor decorator
    │
    ├── validation/                           # Format, size & magic-byte integrity checking
    │   ├── __init__.py
    │   └── interfaces.py                     # DocumentValidator, ValidationResult
    │
    ├── chunking/                             # Chunking strategies (Recursive, Sliding Window, Table-Aware)
    │   ├── __init__.py
    │   └── base.py                           # ChunkingStrategy (abstract base class)
    │
    ├── deduplication/                        # Exact SHA-256 and MinHash LSH chunk indexing
    │   ├── __init__.py
    │   └── base.py                           # DeduplicationStrategy, DeduplicationResult
    │
    ├── embedding/                            # Model-agnostic dense vector representation (BGE-M3)
    │   ├── __init__.py
    │   └── base.py                           # EmbeddingProvider, EmbeddingResult
    │
    ├── indexing/                             # Qdrant upsert with zero-trust ACL payloads
    │   ├── __init__.py
    │   └── base.py                           # VectorIndexProvider, VectorIndexResult
    │
    └── orchestration/                        # Pipeline lifecycle management
        ├── __init__.py
        └── base.py                           # IngestionOrchestrator, IngestionPipelineResult
```

---

## 3. Technical Specifications & Shared Data Contracts

### 3.1. Provenance Coordinates & Locators ([`coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py))

To satisfy Module 1 & 7 line-level citation accuracy ($\ge 0.95$), every chunk and element tracks fine-grained spatial and structural coordinates:
- [`ChunkLocator`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py#L30):
  - `page_number`: 1-indexed page integer (PDF, OCR, multi-page documents).
  - `section_heading`: Nearest hierarchical heading (H1–H6) maintaining semantic outline continuity.
  - `sheet_name`: Worksheet title for multi-tab spreadsheets (e.g., `"Salaries_2026"`).
  - `row_range`: String coordinate for spreadsheet rows (e.g., `"Row 14"` or `"14:28"`).
  - `char_start` & `char_end`: 0-indexed half-open interval $[char\_start, char\_end)$ strictly validated ($char\_start \le char\_end$).
  - `bounding_box`: Optional spatial bounding box (`x0, y0, x1, y1`) with measurement unit (`pt`, `px`, `normalized`).
  - `to_provenance_payload()`: Serializes coordinates into standardized dicts for Qdrant payload storage.

### 3.2. Common Extracted Element Hierarchy ([`elements.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py))

Implemented using Pydantic v2 discriminated unions (`element_type` field discriminator) to guarantee polymorphic deserialization and strict type safety:
1. [`TextElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L38) (`element_type="text"`): Body text blocks.
2. [`HeadingElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L48) (`element_type="heading"`): Hierarchical heading with `level` $\in [1, 6]$.
3. [`ParagraphElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L64) (`element_type="paragraph"`): Narrative prose paragraphs.
4. [`TableElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L74) (`element_type="table"`): Table matrices with column headers, row matrices, caption, and auto-conversion to standard GitHub Flavored Markdown (`to_markdown()`).
5. [`SpreadsheetRowElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L119) (`element_type="spreadsheet_row"`): Tabular row record containing `sheet_name`, `row_index`, and key-value column-to-cell mappings with Markdown serialization.
6. [`PageElement`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py#L162) (`element_type="page"`): Physical page boundary supporting dimensions, OCR confidence, and nested extracted elements.
7. `ExtractedElement`: Annotated union with discriminator `element_type`.

### 3.3. Document Metadata & Zero-Trust RBAC Payloads ([`metadata.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/metadata.py))

- [`AccessControlPayload`](file:///home/mercury/AgentGraph/backend/ingestion/types/metadata.py#L12): Captures `tenant_id`, allowed `roles`, allowed `departments`, and explicit user whitelist (`users`). Generates Qdrant ACL payload dicts for vector-level RBAC filtering.
- [`DocumentMetadata`](file:///home/mercury/AgentGraph/backend/ingestion/types/metadata.py#L36): Models document attributes matching PostgreSQL `documents` schema, including `file_hash_sha256` (64 hex characters), `file_size_bytes`, `mime_type`, `document_type`, and `classification` (`PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `RESTRICTED`).

### 3.4. Chunk Persistence Contract ([`chunking.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/chunking.py))

- [`DocumentChunkInput`](file:///home/mercury/AgentGraph/backend/ingestion/types/chunking.py#L39): Fully attributed chunk model bridging extraction, chunking, and dual-store persistence:
  - `to_qdrant_payload()`: Emits payload matching Step 5 of the Ingestion Pipeline specification (`document_id`, `chunk_index`, `content`, `source_location`, `acl`).
  - `to_db_record()`: Emits dict matching PostgreSQL `document_chunks` table columns.

### 3.5. Pluggable Extractor Registry ([`extraction/registry.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/registry.py))

Adheres to the Open-Closed Principle (OCP):
- [`DocumentExtractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/base.py): Base class defining abstract properties (`name`, `supported_document_types`, `supported_mime_types`, `supports_ocr`) and execution methods (`extract`, `extract_sync`).
- [`ExtractorRegistry`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/registry.py#L17): Thread-safe registry maintaining active extractors. Supports resolution by `DocumentType`, MIME string, and OCR preference (`prefer_ocr=True`).
- [`@register_extractor`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/registry.py#L112): Decorator enabling modular plug-and-play parser registration.

### 3.6. Subsystem Interfaces
- **Validation**: [`DocumentValidator`](file:///home/mercury/AgentGraph/backend/ingestion/validation/interfaces.py#L22) & [`ValidationResult`](file:///home/mercury/AgentGraph/backend/ingestion/validation/interfaces.py#L10)
- **Chunking**: [`ChunkingStrategy`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/base.py#L12) (`RECURSIVE_STRUCTURAL`, `FIXED_SLIDING_WINDOW`, `TABLE_AWARE`)
- **Deduplication**: [`DeduplicationStrategy`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/base.py#L31) & [`DeduplicationResult`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/base.py#L11) (SHA-256 & MinHash LSH)
- **Embedding**: [`EmbeddingProvider`](file:///home/mercury/AgentGraph/backend/ingestion/embedding/base.py#L20) & [`EmbeddingResult`](file:///home/mercury/AgentGraph/backend/ingestion/embedding/base.py#L10) (1024-dim dense representation)
- **Vector Indexing**: [`VectorIndexProvider`](file:///home/mercury/AgentGraph/backend/ingestion/indexing/base.py#L22) & [`VectorIndexResult`](file:///home/mercury/AgentGraph/backend/ingestion/indexing/base.py#L11)
- **Orchestration**: [`IngestionOrchestrator`](file:///home/mercury/AgentGraph/backend/ingestion/orchestration/base.py#L27) & [`IngestionPipelineResult`](file:///home/mercury/AgentGraph/backend/ingestion/orchestration/base.py#L12)

---

## 4. Implementation Inventory

| File Path | Component | Description |
| :--- | :--- | :--- |
| [`backend/__init__.py`](file:///home/mercury/AgentGraph/backend/__init__.py) | Backend Root | Python package marker for root `backend` namespace |
| [`backend/ingestion/__init__.py`](file:///home/mercury/AgentGraph/backend/ingestion/__init__.py) | Ingestion Facade | Consolidated module export exposing 52 public symbols |
| [`backend/ingestion/exceptions.py`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py) | Error Handling | Structured domain exception hierarchy (`IngestionError`, `ExtractionError`, etc.) |
| [`backend/ingestion/types/enums.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/enums.py) | Enums | `DocumentType`, `DocumentMimeType`, `DocumentClassification`, `DocumentIngestionStatus`, `PipelineStage`, `ElementType` |
| [`backend/ingestion/types/coordinates.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/coordinates.py) | Coordinates | `ChunkLocator` and `BoundingBox` spatial and structural offset models |
| [`backend/ingestion/types/metadata.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/metadata.py) | Metadata & ACL | `DocumentMetadata` and `AccessControlPayload` data contracts |
| [`backend/ingestion/types/elements.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/elements.py) | Discriminated Union | `TextElement`, `HeadingElement`, `ParagraphElement`, `TableElement`, `SpreadsheetRowElement`, `PageElement` |
| [`backend/ingestion/types/extraction.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/extraction.py) | Extraction Results | `ExtractionResult` container with convenience accessor methods and `ExtractionStats` |
| [`backend/ingestion/types/chunking.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/chunking.py) | Chunk Contracts | `DocumentChunkInput` with `to_qdrant_payload()` and `to_db_record()` mappers |
| [`backend/ingestion/types/context.py`](file:///home/mercury/AgentGraph/backend/ingestion/types/context.py) | Security Context | Multi-tenant zero-trust `IngestionContext` and `IngestionOptions` |
| [`backend/ingestion/extraction/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/base.py) | Extractor Base | `DocumentExtractor` abstract base class with async and sync wrappers |
| [`backend/ingestion/extraction/registry.py`](file:///home/mercury/AgentGraph/backend/ingestion/extraction/registry.py) | Extractor Registry | Thread-safe `ExtractorRegistry` and `@register_extractor` decorator |
| [`backend/ingestion/validation/interfaces.py`](file:///home/mercury/AgentGraph/backend/ingestion/validation/interfaces.py) | Validation Base | `DocumentValidator` and `ValidationResult` contracts |
| [`backend/ingestion/chunking/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/chunking/base.py) | Chunking Base | `ChunkingStrategy` abstract base class contract |
| [`backend/ingestion/deduplication/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/deduplication/base.py) | Deduplication Base | `DeduplicationStrategy` and `DeduplicationResult` contracts |
| [`backend/ingestion/embedding/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/embedding/base.py) | Embedding Base | `EmbeddingProvider` and `EmbeddingResult` contracts |
| [`backend/ingestion/indexing/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/indexing/base.py) | Vector Indexing Base | `VectorIndexProvider` and `VectorIndexResult` contracts |
| [`backend/ingestion/orchestration/base.py`](file:///home/mercury/AgentGraph/backend/ingestion/orchestration/base.py) | Orchestration Base | `IngestionOrchestrator` and `IngestionPipelineResult` contracts |
| [`tests/__init__.py`](file:///home/mercury/AgentGraph/tests/__init__.py) | Test Package | Test suite root package marker |
| [`tests/test_ingestion_architecture.py`](file:///home/mercury/AgentGraph/tests/test_ingestion_architecture.py) | Test Suite | 24 automated unit tests covering all data contracts, registries, and validators |

---

## 5. Verification & Test Suite Execution

### 5.1. Automated Unit Tests (`pytest`)
All 24 unit tests executed successfully in 0.20 seconds:
```bash
pytest -v tests/
```
```
=================================== test session starts ====================================
platform linux -- Python 3.12.16, pytest-9.2.0, pluggy-1.7.0 -- /home/mercury/miniconda3/envs/ml-env/bin/python3.12
rootdir: /home/mercury/AgentGraph
collected 24 items                                                                         

tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_mime PASSED [  4%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_mime_unsupported PASSED [  8%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_filename PASSED [ 12%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_filename_unsupported PASSED [ 16%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_prisma_status_alignment PASSED [ 20%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_valid_bounding_box PASSED [ 25%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_invalid_bounding_box PASSED [ 29%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_chunk_locator_valid_half_open_offsets PASSED [ 33%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_chunk_locator_invalid_offsets PASSED [ 37%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_chunk_locator_spreadsheet_provenance PASSED [ 41%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_heading_element PASSED [ 45%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_paragraph_element PASSED [ 50%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_table_element_markdown PASSED [ 54%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_spreadsheet_row_element_markdown PASSED [ 58%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_discriminated_union_polymorphic_parsing PASSED [ 62%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_page_element_with_nested_elements PASSED [ 66%]
tests/test_ingestion_architecture.py::TestExtractionResult::test_extraction_result_helpers PASSED [ 70%]
tests/test_ingestion_architecture.py::TestDocumentChunkAndQdrantPayload::test_chunk_to_qdrant_payload PASSED [ 75%]
tests/test_ingestion_architecture.py::TestDocumentChunkAndQdrantPayload::test_chunk_to_db_record PASSED [ 79%]
tests/test_ingestion_architecture.py::TestIngestionContext::test_context_acl_derivation PASSED [ 83%]
tests/test_ingestion_architecture.py::TestExtractorRegistryAndExtensibility::test_custom_extractor_registration_and_retrieval PASSED [ 87%]
tests/test_ingestion_architecture.py::TestExtractorRegistryAndExtensibility::test_extractor_not_found PASSED [ 91%]
tests/test_ingestion_architecture.py::TestExtractorRegistryAndExtensibility::test_decorator_registration PASSED [ 95%]
tests/test_ingestion_architecture.py::TestSubsystemInterfacesUninstantiability::test_abstract_contracts_cannot_be_instantiated PASSED [100%]

==================================== 24 passed in 0.20s ====================================
```

### 5.2. Non-Regression Database Infrastructure Verification
Verified that Prisma schema and prior Phase 1 database infrastructure remain unaffected:
```bash
npx prisma validate --schema backend/schema.prisma
```
Output:
```
The schema at backend/schema.prisma is valid 🚀
```

---

## 6. Next Milestones (Phase 2 Roadmap)

With the foundational architecture, common interfaces, and extractor registry established, subsequent Phase 2 tasks will proceed as follows:
1. **Task 02 – Pre-Ingestion Deduplication & Validation**: Implement binary SHA-256 hash checking against PostgreSQL `documents` and file integrity validators.
2. **Task 03 – Native PDF & Scanned OCR Extractors**: Implement `NativePDFExtractor` (`pdfplumber` / `PyMuPDF`) and `OCRScannerExtractor` (`RapidOCR` / `Tesseract`), registering them into `ExtractorRegistry`.
3. **Task 04 – Microsoft Word (DOCX) & Spreadsheet (XLSX / CSV) Extractors**: Implement OpenXML heading traversers and tabular row-to-markdown serializers.
4. **Task 05 – Structure-Aware Chunking Engine**: Implement `RecursiveStructuralChunker`, `SlidingWindowChunker`, and `TableAwareChunker` emitting attributed `DocumentChunkInput` records.
