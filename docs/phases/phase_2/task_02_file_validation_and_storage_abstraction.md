# Task 02: File Validation and Storage Abstraction – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & ML Systems Engineer  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (Python 3.12.16, Pydantic v2, PostgreSQL 16+, Prisma ORM)  
**Phase**: Phase 2 – Heterogeneous Document Ingestion & Indexing Pipeline (Stage 1: Ingestion Foundation)  
**Subsystems**:
- [`backend/ingestion/validation`](file:///home/mercury/AgentGraph/backend/ingestion/validation)
- [`backend/ingestion/storage`](file:///home/mercury/AgentGraph/backend/ingestion/storage)
- [`backend/ingestion/config.py`](file:///home/mercury/AgentGraph/backend/ingestion/config.py)
- [`backend/ingestion/exceptions.py`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py)

---

## 1. Executive Summary & Objective

The objective of **Stage 1 (Ingestion Foundation) – Task 02** was to construct a hardened, production-grade file validation and storage abstraction layer prior to document parsing and text extraction. This layer acts as the frontline defensive gatekeeper for enterprise document uploads, protecting the system from malicious file uploads, directory traversal attacks, corrupted archives, MIME/extension spoofing, and unauthorized cross-tenant data access.

### Strict Scope Boundaries Adhered To
1. **Multi-Format Magic Byte Verification**: Validated files using actual binary signatures (magic bytes) and format-specific internal structures (PDF, DOCX, XLSX, CSV), rejecting reliance on client-declared file extensions or HTTP headers alone.
2. **Defensive Path Confinement**: Enforced zero-trust path traversal defenses against absolute paths, relative traversal sequences (`..`), null byte injections (`\x00`), and reserved device names.
3. **Pluggable Storage Abstraction**: Decoupled physical persistence (Local Filesystem with atomic writes, S3-compatible Object Storage for MinIO/AWS/Cloudflare R2) from business logic through a unified driver contract (`StorageBackend`).
4. **Strict Zero-Trust Tenant Boundary Isolation**: Enforced deterministic tenant-partitioned storage keys (`tenants/{tenant_id}/documents/{document_id}/{version_id}/{filename}`) and active tenant boundary validation on every storage operation (`save`, `get`, `delete`, `exists`, `get_metadata`).
5. **Zero Hardcoding Principle**: Managed all storage locations, S3 credentials, bucket names, and upload restrictions via dynamic environment variables using Pydantic Settings (`pydantic-settings`).

---

## 2. Architecture & Subsystem Organization

```
[Incoming Document Stream / File Path]
                  │
                  ▼
┌────────────────────────────────────────────────────────┐
│ DefaultDocumentValidator                               │
│ ├── Filename Sanitization & Traversal Blocker          │
│ ├── File Size Gate (Min / Max byte limits)             │
│ ├── Binary Magic Byte Inspection (PDF, ZIP, CSV)       │
│ └── Structural Integrity (OpenXML DOM / PDF %%EOF)     │
└──────────────────────────┬─────────────────────────────┘
                           │ Validated
                           ▼
┌────────────────────────────────────────────────────────┐
│ StorageService (Tenant Boundary Coordinator)           │
│ ├── Partitioned Key: tenants/{tenant_id}/documents/... │
│ ├── Cross-Tenant Boundary Access Validation            │
│ ├── SHA-256 Content Digest & Metadata Extraction       │
│ └── Storage Operation Dispatch                         │
└──────────────────────────┬─────────────────────────────┘
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐
│ LocalStorageBackend         │ │ S3StorageBackend            │
│ ├── Path Confinement Check  │ │ ├── AWS S3 / MinIO / R2     │
│ └── Atomic Replace Writes   │ │ └── Boto3 Async Threading   │
└─────────────────────────────┘ └─────────────────────────────┘
```

### 2.1. Directory Structure

```
backend/ingestion/
├── __init__.py                                 # Subsystem entry point and public exports
├── config.py                                   # Pydantic Settings dynamic environment configuration
├── exceptions.py                               # Comprehensive typed domain exceptions
│
├── validation/                                 # File validation & sanitization
│   ├── __init__.py                             # Validation package exports
│   ├── interfaces.py                           # DocumentValidator, ValidationResult contracts
│   ├── sanitizer.py                            # Filename sanitization, path traversal defenses
│   ├── signatures.py                           # Magic bytes & structural integrity checkers
│   └── validator.py                            # DefaultDocumentValidator implementation
│
└── storage/                                    # Storage abstraction & drivers
    ├── __init__.py                             # Storage package exports
    ├── interfaces.py                           # StorageBackend abstract base class
    ├── models.py                               # StoredFileMetadata data model
    ├── local.py                                # LocalStorageBackend with path confinement
    ├── s3.py                                   # S3StorageBackend for S3 / MinIO / Cloudflare R2
    ├── service.py                              # StorageService tenant boundary coordinator
    └── factory.py                              # get_storage_backend & get_storage_service
```

---

## 3. Detailed Component Implementation

### 3.1. Dynamic Configuration ([`backend/ingestion/config.py`](file:///home/mercury/AgentGraph/backend/ingestion/config.py))

Adhering strictly to Section 1 of the Operational Guidelines (Zero Hardcoding Principle), all operational parameters are resolved via `IngestionSettings` (`pydantic_settings.BaseSettings`):

```python
class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    storage_backend: str = Field(default="local", alias="STORAGE_BACKEND")
    storage_local_root: str = Field(default="storage/documents", alias="STORAGE_LOCAL_ROOT")

    s3_endpoint_url: str | None = Field(default=None, alias="S3_ENDPOINT_URL")
    s3_access_key_id: str | None = Field(default=None, alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str | None = Field(default=None, alias="S3_SECRET_ACCESS_KEY")
    s3_bucket_name: str = Field(default="enterprise-knowledge-docs", alias="S3_BUCKET_NAME")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")

    max_file_size_bytes: int = Field(default=52_428_800, alias="MAX_FILE_SIZE_BYTES")  # 50MB
    min_file_size_bytes: int = Field(default=1, alias="MIN_FILE_SIZE_BYTES")
    max_filename_length: int = Field(default=255, alias="MAX_FILENAME_LENGTH")
    allowed_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".docx", ".xlsx", ".csv"])
```

### 3.2. Filename Sanitization & Path Traversal Prevention ([`sanitizer.py`](file:///home/mercury/AgentGraph/backend/ingestion/validation/sanitizer.py))

- **Path Traversal Blocker**: Detects and raises [`PathTraversalError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L45) on relative traversal sequences (`..`), leading slashes (`/`, `\`), and URL-encoded variants (`%2e%2e%2f`).
- **Null Byte Injection Defense**: Blocks null byte poisoning attacks (`filename.pdf\x00.exe`).
- **Reserved Names Elimination**: Disallows Windows system device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).
- **Dangerous Character Filtering**: Sanitizes control characters and dangerous symbols (`[\x00-\x1f\x7f<>:"/\\|?*]`), replacing them with safe underscores.
- **Path Confinement Verification**: [`validate_safe_path`](file:///home/mercury/AgentGraph/backend/ingestion/validation/sanitizer.py#L110) guarantees that resolved targets cannot break out of their authorized base directory (`resolved_target.is_relative_to(resolved_base)`).

### 3.3. File Signatures & Structural Integrity Checks ([`signatures.py`](file:///home/mercury/AgentGraph/backend/ingestion/validation/signatures.py))

Implemented using standard library utilities to guarantee zero native C-binding external dependencies:
1. **PDF (`application/pdf`)**:
   - Signature: Validates `%PDF-` header within file prefix.
   - Structure: Enforces minimum byte length, verifies the presence of mandatory `%%EOF` trailer, and ensures existence of PDF dictionary objects (`obj`, `xref`, `/Root`, `/Type`).
   - Corrupted or truncated PDFs trigger [`MalformedFileError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L65).
2. **DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`)**:
   - Signature: Validates standard PK ZIP magic bytes (`PK\x03\x04`).
   - OpenXML Structure: Inspects archive manifest `[Content_Types].xml` and requires `word/document.xml` or `word/` package entries.
   - Detects format spoofing (e.g., XLSX renamed as `.docx` or generic ZIP renamed as `.docx`) and raises [`FileSignatureMismatchError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L60).
3. **XLSX (`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`)**:
   - Signature: Validates PK ZIP magic bytes (`PK\x03\x04`).
   - OpenXML Structure: Verifies `[Content_Types].xml` and requires `xl/workbook.xml` or `xl/` entries.
   - Rejects DOCX files renamed as `.xlsx` or corrupted ZIP streams.
4. **CSV (`text/csv`)**:
   - Signature: Validates text encoding (UTF-8, UTF-8-BOM, Latin-1, ASCII) without binary null bytes (`\x00`).
   - Structure: Parses sample lines using Python `csv.reader` to verify non-empty tabular data.
   - Explicitly rejects executable headers (`MZ`, `\x7fELF`, Mach-O) disguised as text.

### 3.4. Default Document Validator ([`validator.py`](file:///home/mercury/AgentGraph/backend/ingestion/validation/validator.py))

Implements [`DocumentValidator`](file:///home/mercury/AgentGraph/backend/ingestion/validation/interfaces.py) with dual operational modes:
- `validate(content, filename, context) -> ValidationResult`: Non-raising diagnostic report returning `is_valid: bool`, `errors: list[str]`, `warnings: list[str]`, `detected_mime_type: str`, `file_size_bytes: int`, and contextual diagnostic metadata.
- `validate_or_raise(content, filename, context) -> ValidationResult`: Raises fine-grained domain exceptions (`EmptyFileError`, `FileSizeExceededError`, `PathTraversalError`, `FileSignatureMismatchError`, `MalformedFileError`, `UnsupportedDocumentTypeError`).

### 3.5. Storage Abstraction Layer ([`backend/ingestion/storage/`](file:///home/mercury/AgentGraph/backend/ingestion/storage))

- **Abstract Driver Contract ([`interfaces.py`](file:///home/mercury/AgentGraph/backend/ingestion/storage/interfaces.py))**:
  - `put(key, data, content_type) -> str`: Writes binary stream and returns canonical URI (`file://` or `s3://`).
  - `get(key) -> bytes`: Reads stored binary content.
  - `delete(key) -> bool`: Idempotently removes stored object.
  - `exists(key) -> bool`: Verifies object presence.
  - `get_raw_metadata(key) -> dict`: Returns physical size, timestamps, and driver properties.
- **Local Filesystem Driver ([`local.py`](file:///home/mercury/AgentGraph/backend/ingestion/storage/local.py))**:
  - Path confinement prevents symlink or key traversal outside `STORAGE_LOCAL_ROOT`.
  - Atomic persistence writes to a temporary file (`.tmp_upload_*`) in the target directory and executes atomic `os.replace` to prevent corrupted partial files during power loss or concurrent reads.
- **S3 Object Storage Driver ([`s3.py`](file:///home/mercury/AgentGraph/backend/ingestion/storage/s3.py))**:
  - Interacts with S3-compatible APIs (AWS, MinIO, Cloudflare R2) via `boto3`.
  - Maps S3 error codes (`NoSuchKey` $\to$ `StorageFileNotFoundError`, `AccessDenied` $\to$ `StorageAccessDeniedError`).
- **Storage Service & Zero-Trust Tenant Boundary Isolation ([`service.py`](file:///home/mercury/AgentGraph/backend/ingestion/storage/service.py))**:
  - Structured Key Pattern: `tenants/{tenant_id}/documents/{document_id}/{version_id}/{filename}`.
  - Boundary Enforcement: Every storage call validates that the requesting `context.tenant_id` matches the storage key's tenant partition. Any cross-tenant access attempt immediately halts and raises [`TenantStorageViolationError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L90).
  - Pre-persistence Validation: Automatically invokes `DefaultDocumentValidator.validate_or_raise` before persisting to storage, guaranteeing that corrupted or malicious payloads never enter object storage.

---

## 4. Verification & Test Execution

A comprehensive test suite was developed across two dedicated test modules, alongside the regression validation of Task 01 interfaces:

1. **[`tests/test_file_validation.py`](file:///home/mercury/AgentGraph/tests/test_file_validation.py)**: 25 unit tests
   - Filename sanitization, relative and absolute traversal blocking, URL-encoded traversal, null bytes, Windows reserved names, and leading dot removal.
   - Magic bytes detection across PDF, DOCX, XLSX, CSV, executables (`MZ`), and generic ZIPs.
   - Format integrity verification: empty files, file size caps, extension mismatches (DOCX renamed to `.xlsx`), corrupted PDF trailers, truncated ZIPs, and CSVs containing binary null bytes.
2. **[`tests/test_storage_abstraction.py`](file:///home/mercury/AgentGraph/tests/test_storage_abstraction.py)**: 11 unit tests
   - Local storage put/get/delete lifecycle, non-existent file handling, and path confinement escape blocking.
   - Tenant isolation: save and retrieve lifecycle, cross-tenant read/write denial (`TenantStorageViolationError`), document ID mismatch rejection, and key traversal blocking.
   - Pre-persistence validation rejection of empty and executable files.
   - S3 backend driver properties, missing dependency handling, and mocked S3 API operations.
3. **[`tests/test_ingestion_architecture.py`](file:///home/mercury/AgentGraph/tests/test_ingestion_architecture.py)**: 24 regression unit tests

### 4.1. Test Execution Results

```bash
============================= test session starts ==============================
platform linux -- Python 3.12.16, pytest-9.2.0, pluggy-1.7.0 -- /home/mercury/miniconda3/envs/ml-env/bin/python
rootdir: /home/mercury/AgentGraph
plugins: anyio-4.16.0, langsmith-0.14.17
collected 60 items

tests/test_file_validation.py::TestFilenameSanitization::test_valid_filenames PASSED [  1%]
tests/test_file_validation.py::TestFilenameSanitization::test_path_traversal_relative_blocked PASSED [  3%]
tests/test_file_validation.py::TestFilenameSanitization::test_path_traversal_absolute_blocked PASSED [  5%]
tests/test_file_validation.py::TestFilenameSanitization::test_url_encoded_traversal_blocked PASSED [  6%]
tests/test_file_validation.py::TestFilenameSanitization::test_null_byte_injection_blocked PASSED [  8%]
tests/test_file_validation.py::TestFilenameSanitization::test_windows_reserved_names_blocked PASSED [ 10%]
tests/test_file_validation.py::TestFilenameSanitization::test_empty_or_whitespace_filename_rejected PASSED [ 11%]
tests/test_file_validation.py::TestFilenameSanitization::test_leading_dots_stripped PASSED [ 13%]
tests/test_file_validation.py::TestFilenameSanitization::test_max_filename_length_enforcement PASSED [ 15%]
tests/test_file_validation.py::TestFormatAndSignatureDetection::test_detect_valid_pdf PASSED [ 16%]
tests/test_file_validation.py::TestFormatAndSignatureDetection::test_detect_valid_docx PASSED [ 18%]
tests/test_file_validation.py::TestFormatAndSignatureDetection::test_detect_valid_xlsx PASSED [ 20%]
tests/test_file_validation.py::TestFormatAndSignatureDetection::test_detect_valid_csv PASSED [ 21%]
tests/test_file_validation.py::TestFormatAndSignatureDetection::test_detect_executable_binary_rejected PASSED [ 23%]
tests/test_file_validation.py::TestFormatAndSignatureDetection::test_detect_generic_zip_rejected PASSED [ 25%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_validate_all_four_supported_formats PASSED [ 26%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_empty_file PASSED [ 28%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_exceeding_file_size PASSED [ 30%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_unsupported_file_extension PASSED [ 31%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_signature_mismatch PASSED [ 33%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_docx_disguised_as_xlsx PASSED [ 35%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_corrupted_pdf_missing_eof PASSED [ 36%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_corrupted_docx_truncated_zip PASSED [ 38%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_reject_csv_with_binary_null_bytes PASSED [ 40%]
tests/test_file_validation.py::TestDefaultDocumentValidator::test_path_traversal_in_validate_or_raise PASSED [ 41%]
tests/test_storage_abstraction.py::TestLocalStorageBackend::test_put_get_delete_lifecycle PASSED [ 43%]
tests/test_storage_abstraction.py::TestLocalStorageBackend::test_get_nonexistent_file_raises PASSED [ 45%]
tests/test_storage_abstraction.py::TestLocalStorageBackend::test_path_confinement_blocks_traversal PASSED [ 46%]
tests/test_storage_abstraction.py::TestStorageServiceTenantIsolation::test_save_and_retrieve_valid_document PASSED [ 48%]
tests/test_storage_abstraction.py::TestStorageServiceTenantIsolation::test_cross_tenant_access_denied PASSED [ 50%]
tests/test_storage_abstraction.py::TestStorageServiceTenantIsolation::test_document_id_mismatch_denied PASSED [ 51%]
tests/test_storage_abstraction.py::TestStorageServiceTenantIsolation::test_path_traversal_in_storage_key_blocked PASSED [ 53%]
tests/test_storage_abstraction.py::TestStorageServiceTenantIsolation::test_validation_prevents_saving_corrupt_file PASSED [ 55%]
tests/test_storage_abstraction.py::TestS3StorageBackend::test_s3_backend_properties PASSED [ 56%]
tests/test_storage_abstraction.py::TestS3StorageBackend::test_s3_missing_boto3_raises_configuration_error PASSED [ 58%]
tests/test_storage_abstraction.py::TestS3StorageBackend::test_s3_put_get_with_mock PASSED [ 60%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_mime PASSED [ 61%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_mime_unsupported PASSED [ 63%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_filename PASSED [ 65%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_document_type_from_filename_unsupported PASSED [ 66%]
tests/test_ingestion_architecture.py::TestDocumentEnums::test_prisma_status_alignment PASSED [ 68%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_valid_bounding_box PASSED [ 70%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_invalid_bounding_box PASSED [ 71%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_chunk_locator_valid_half_open_offsets PASSED [ 73%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_chunk_locator_invalid_offsets PASSED [ 75%]
tests/test_ingestion_architecture.py::TestCoordinatesAndLocators::test_chunk_locator_spreadsheet_provenance PASSED [ 76%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_heading_element PASSED [ 78%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_paragraph_element PASSED [ 80%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_table_element_markdown PASSED [ 81%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_spreadsheet_row_element_markdown PASSED [ 83%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_discriminated_union_polymorphic_parsing PASSED [ 85%]
tests/test_ingestion_architecture.py::TestExtractedElementsDiscriminatedUnion::test_page_element_with_nested_elements PASSED [ 86%]
tests/test_ingestion_architecture.py::TestExtractionResult::test_extraction_result_helpers PASSED [ 88%]
tests/test_ingestion_architecture.py::TestDocumentChunkAndQdrantPayload::test_chunk_to_qdrant_payload PASSED [ 90%]
tests/test_ingestion_architecture.py::TestDocumentChunkAndQdrantPayload::test_chunk_to_db_record PASSED [ 91%]
tests/test_ingestion_architecture.py::TestIngestionContext::test_context_acl_derivation PASSED [ 93%]
tests/test_ingestion_architecture.py::TestExtractorRegistryAndExtensibility::test_custom_extractor_registration_and_retrieval PASSED [ 95%]
tests/test_ingestion_architecture.py::TestExtractorRegistryAndExtensibility::test_extractor_not_found PASSED [ 96%]
tests/test_ingestion_architecture.py::TestExtractorRegistryAndExtensibility::test_decorator_registration PASSED [ 98%]
tests/test_ingestion_architecture.py::TestSubsystemInterfacesUninstantiability::test_abstract_contracts_cannot_be_instantiated PASSED [100%]

============================== 60 passed in 0.36s ==============================
PYTEST EXIT CODE: 0
```

---

## 5. Security & Isolation Matrix

| Threat Vector | Mitigation Strategy | Failure Response |
| :--- | :--- | :--- |
| **Path Traversal Attacks** (`../../etc/passwd`, `C:\Windows`) | Strict character sanitization and `Path.is_relative_to` verification. | Raises [`PathTraversalError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L45). |
| **Cross-Tenant Document Access** (Tenant B requesting Tenant A file) | Mandatory tenant prefix verification (`tenants/{tenant_id}/...`) in [`StorageService`](file:///home/mercury/AgentGraph/backend/ingestion/storage/service.py). | Raises [`TenantStorageViolationError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L90). |
| **Extension Spoofing** (`malware.exe` renamed to `policy.pdf`) | Magic bytes inspection against known format signatures before parser execution. | Raises [`FileSignatureMismatchError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L60). |
| **Format Spoofing** (`sheet.xlsx` renamed to `memo.docx`) | OpenXML archive XML manifest verification (`word/document.xml` vs `xl/workbook.xml`). | Raises [`FileSignatureMismatchError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L60). |
| **Zip Bomb / Corrupted Archives** | Archive header validation and truncated stream error trapping (`zipfile.BadZipFile`). | Raises [`MalformedFileError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L65). |
| **Denial of Service via File Size** | Configurable `MAX_FILE_SIZE_BYTES` enforcement before storage or extraction. | Raises [`FileSizeExceededError`](file:///home/mercury/AgentGraph/backend/ingestion/exceptions.py#L55). |
| **Partial / Incomplete Upload Writes** | Local storage writes to temporary files first, followed by atomic POSIX `os.replace`. | Atomically cleaned up on exception; zero orphaned partial files. |

---

## 6. Next Steps & Downstream Integration

With the file validation and storage abstraction layer completed and verified:
1. **Task 03 (Native & Scanned PDF Extraction Pipeline)**:
   - Integrate `StorageService` to retrieve stored PDF binaries.
   - Implement `PdfDocumentExtractor` registered in `default_extractor_registry`.
   - Implement dual digital (`pdfplumber` / `pypdf`) and OCR fallback (`Tesseract` / `RapidOCR`) pipelines.
2. **Task 04 (Office & Tabular Document Extractors)**:
   - Implement `DocxDocumentExtractor` for hierarchical headings and narrative prose.
   - Implement `SpreadsheetDocumentExtractor` for multi-tab Excel (`openpyxl`) and CSV files.
3. **Task 05 (FastAPI Document Upload Gateway)**:
   - Wire `StorageService` and `DefaultDocumentValidator` into the `/api/v1/documents/upload` HTTP controller.
