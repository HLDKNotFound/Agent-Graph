---
name: document-ingestion-pipeline
description: >-
  Ingests, parses, chunks, deduplicates, and indexes enterprise documents (PDF, DOCX, XLSX, OCR)
  into PostgreSQL and Qdrant. Use this skill when processing raw corporate documents, verifying
  line-level lineage coordinates, configuring zero-trust ACL payloads, or debugging ingestion failures.
---

# Enterprise Document Ingestion & Lineage Pipeline

This skill guides the agent through processing unstructured and semi-structured corporate files, extracting structured metadata, generating chunk embeddings, and indexing them into Qdrant and PostgreSQL with strict access control.

---

## 1. Supported Document Formats & Extractors

| Format | Parsing Engine | Extraction Specifics |
| :--- | :--- | :--- |
| **PDF (Digital)** | `pypdf`, `pdfplumber`, `PyMuPDF` | Extracts digital text streams, font size hierarchy (H1-H3), page boundaries, and embedded tables. |
| **PDF (Scanned)** | `Tesseract OCR`, `RapidOCR` | Detects image-only pages, performs image binarization/deskewing, extracts text with bounding boxes per line. |
| **DOCX** | `python-docx` | Traverses OpenXML DOM: hierarchical headings (`Heading 1-6`), bullet lists, table structures, and footnotes. |
| **XLSX / CSV** | `openpyxl`, `pandas` | Extracts sheets, row/column coordinates, transforms tabular data into standalone markdown records and semantic key-value tuples. |

---

## 2. Ingestion & Lineage Procedures

### Step 1: Pre-Ingestion Deduplication Check
Before reading or chunking file content, compute the SHA-256 hash of the binary file to detect exact duplicates:
```python
import hashlib

def compute_file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()
```
- Query PostgreSQL `documents` table by `(tenant_id, file_hash_sha256)`:
  - If identical file hash exists under the same tenant: Link new reference or return existing document ID without re-embedding.
  - If new: Proceed to extraction and upload raw file to Object Storage (`storageKey`).

### Step 2: Content Parsing & Structural Chunking
Select the appropriate chunking strategy based on file format and document nature:
1. **Recursive Structural Chunking (Standard Documents & Manuals)**:
   - Split using structural separators: `["\n\n# ", "\n\n## ", "\n\n### ", "\n\n", "\n", ". "]`.
   - Prepend parent section headings to each child chunk to maintain context continuity.
2. **Fixed-Size Chunking with Sliding Window (Dense Narrative Text)**:
   - Window size: 512 tokens. Overlap: 64 tokens (~12.5%).
3. **Table-Aware Semantic Chunking (XLSX, CSV, Tabular DOCX)**:
   - Serialize each table row with header names: `| Department | Allowance | Condition |`.
   - Preserve sheet name, column headers, and row coordinate range.

### Step 3: Exact Lineage Attribution
For every chunk generated, populate the exact locator attributes:
```json
{
  "chunk_index": 8,
  "page_number": 12,
  "section_heading": "3.2 Annual Leave Allowances",
  "sheet_name": null,
  "row_range": null,
  "char_start": 14200,
  "char_end": 15820
}
```

### Step 4: Near-Duplicate Detection (MinHash LSH)
Compute MinHash signature (128 permutation hashes) on normalized 3-shingles:
- If Jaccard similarity $> 0.92$ with existing chunk in the same collection, flag revision overlap.

### Step 5: Zero-Trust Vector Payload Indexing (Qdrant)
Push vectors into Qdrant collection with mandatory ACL payload filters:
```python
from qdrant_client.models import PointStruct

point = PointStruct(
    id=chunk_id,  # UUID v4
    vector=embedding_vector,  # 1024-dim dense vector from EmbeddingProvider
    payload={
        "document_id": document_id,
        "chunk_index": chunk_index,
        "content": chunk_content,
        "source_location": {
            "page_number": page_number,
            "section_heading": section_heading,
            "sheet_name": sheet_name,
            "row_range": row_range,
        },
        "acl": {
            "tenant_id": tenant_id,
            "roles": allowed_roles,          # e.g. ["HR_SPECIALIST", "EXECUTIVE"]
            "departments": allowed_depts,    # e.g. ["Human Resources", "Executive Committee"]
            "users": allowed_user_ids        # Optional user whitelist
        }
    }
)
```

### Step 6: PostgreSQL Relational Persistence & Full-Text Search
Persist chunk record in `document_chunks` table in PostgreSQL. The GIN index automatically indexes `to_tsvector('simple', content)` for sparse BM25 retrieval.

---

## 3. Operational Ingestion CLI
Run ingestion tasks dynamically without hardcoding paths or database URLs:
```bash
# Ingest single document into tenant workspace
python -m backend.ingestion.cli \
  --file-path "${INPUT_FILE_PATH}" \
  --tenant-id "${TENANT_ID}" \
  --department-code "${DEPT_CODE:-HR}" \
  --classification "${CLASSIFICATION:-INTERNAL}" \
  --uploaded-by "${USER_ID}"

# Re-index all pending documents in Celery task queue
celery -A backend.tasks.celery_app worker --loglevel=INFO --concurrency=4
```
