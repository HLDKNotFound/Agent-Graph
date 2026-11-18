# Task 06: Document Metadata & Classification Management – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & Security Architect  
**Date**: November 17, 2026  
**Environment**: Conda `ml-env` (PostgreSQL 16+ with pgvector)  

---

## 1. Executive Summary & Objective

The objective of Task 06 is to design and implement the core **Document Metadata & Classification Management** model for the **Enterprise Knowledge Agent** platform.

This data model serves as the foundational persistence layer for enterprise document ingestion, lifecycle tracking, multi-tenant SHA-256 deduplication, zero-trust security classification, and permission-aware retrieval.

### Key Architectural Highlights:
1. **Core `Document` Model**: Persists complete document metadata including UUID PK, tenant isolation, uploader user reference, department reference, file size (`BIGINT`), MIME type, original & normalized filenames, storage URI, SHA-256 hash, and extensible JSON metadata.
2. **Standardized Enums**:
   - `DocumentClassification`: `PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `RESTRICTED`.
   - `DocumentIngestionStatus`: `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `CANCELLED`.
3. **Tenant-Aware, Soft-Deletion Sensitive Deduplication**:
   - Leverages a PostgreSQL partial unique index `(tenant_id, content_hash) WHERE deleted_at IS NULL`.
   - Prevents duplicate active uploads within a single tenant.
   - Permits identical file content across distinct tenants without leakage.
   - Allows re-uploading identical file content after soft deletion (`deleted_at IS NOT NULL`).
4. **Zero-Trust Cross-Tenant Boundary Enforcement**:
   - Database-level trigger `enforce_document_tenant_isolation` guarantees that assigned `department_id` and `uploaded_by_id` strictly belong to the same `tenant_id` as the document.
5. **Relational Lifecycle Governance**:
   - `onDelete: SetNull` for Department and Uploader User, preventing accidental cascading document loss.
   - `onDelete: Cascade` for Tenant, ensuring complete deprovisioning hygiene.

---

## 2. Implemented Models & Enums

### 2.1. Document Enums

```prisma
enum DocumentClassification {
  PUBLIC
  INTERNAL
  CONFIDENTIAL
  RESTRICTED
}

enum DocumentIngestionStatus {
  PENDING
  PROCESSING
  COMPLETED
  FAILED
  CANCELLED
}
```

### 2.2. Document Model (`documents`)

```prisma
model Document {
  id                 String                  @id @default(uuid()) @db.Uuid
  tenantId           String                  @map("tenant_id") @db.Uuid
  departmentId       String?                 @map("department_id") @db.Uuid
  uploadedById       String?                 @map("uploaded_by_id") @db.Uuid
  originalFilename   String                  @map("original_filename") @db.VarChar(255)
  normalizedFilename String                  @map("normalized_filename") @db.VarChar(255)
  storageUri         String                  @map("storage_uri") @db.Text
  mimeType           String                  @map("mime_type") @db.VarChar(100)
  fileSizeBytes      BigInt                  @map("file_size_bytes")
  contentHash        String                  @map("content_hash") @db.VarChar(64)
  classification     DocumentClassification  @default(INTERNAL)
  status             DocumentIngestionStatus @default(PENDING)
  metadata           Json?                   @default("{}")
  createdAt          DateTime                @default(now()) @map("created_at") @db.Timestamptz(6)
  updatedAt          DateTime                @updatedAt @map("updated_at") @db.Timestamptz(6)
  deletedAt          DateTime?               @map("deleted_at") @db.Timestamptz(6)

  // Relationships
  tenant     Tenant      @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  department Department? @relation(fields: [departmentId], references: [id], onDelete: SetNull)
  uploadedBy User?       @relation("DocumentUploader", fields: [uploadedById], references: [id], onDelete: SetNull)

  // Query Optimization & Deduplication Indexes
  @@index([tenantId, contentHash])
  @@index([tenantId, classification])
  @@index([tenantId, status])
  @@index([tenantId, createdAt(sort: Desc)])
  @@index([uploadedById])
  @@index([departmentId])
  @@map("documents")
}
```

### 2.3. Field Specifications Table

| Field | Type | Attributes & Modifiers | Description |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `@id @default(uuid()) @db.Uuid` | Immutable surrogate primary key |
| `tenant_id` | `UUID` | `@db.Uuid`, Foreign Key | Owning tenant identifier (`onDelete: Cascade`) |
| `department_id` | `UUID?` | Nullable (`@db.Uuid`), Foreign Key | Department scope reference (`onDelete: SetNull`) |
| `uploaded_by_id` | `UUID?` | Nullable (`@db.Uuid`), Foreign Key | Uploader actor reference (`onDelete: SetNull`) |
| `original_filename` | `VARCHAR(255)` | Not Null | Original filename as provided during upload |
| `normalized_filename` | `VARCHAR(255)` | Not Null | Sanitized, URL-safe, lowercase canonical filename |
| `storage_uri` | `TEXT` | Not Null | Storage URI or object storage key (e.g. `s3://...`) |
| `mime_type` | `VARCHAR(100)` | Not Null | Standard IANA MIME type (e.g. `application/pdf`) |
| `file_size_bytes` | `BIGINT` | Not Null | Exact file size in bytes |
| `content_hash` | `VARCHAR(64)` | Not Null | SHA-256 64-character lowercase hexadecimal hash |
| `classification` | `ENUM` | `DocumentClassification`, Default `INTERNAL` | Security classification for RBAC and filtering |
| `status` | `ENUM` | `DocumentIngestionStatus`, Default `PENDING` | Current ingestion pipeline lifecycle status |
| `metadata` | `JSONB` | Nullable (`@default("{}")`) | Structured document metadata (pages, author, OCR) |
| `created_at` | `TIMESTAMPTZ(6)` | `@default(now())` | Timezone-aware creation timestamp |
| `updated_at` | `TIMESTAMPTZ(6)` | `@updatedAt` | Automatic update timestamp |
| `deleted_at` | `TIMESTAMPTZ(6)` | Nullable | Soft-deletion timestamp (`null` if active) |

---

## 3. Deduplication Architecture: Tenant-Aware & Soft-Delete Sensitive

Duplicate file uploads waste object storage capacity, duplicate vector embeddings in Qdrant, and pollute RAG retrieval candidate sets.

### 3.1. Deduplication Invariants
1. **Tenant Isolation**: Identical document contents (same SHA-256 hash) uploaded to **Tenant A** and **Tenant B** are completely independent and MUST both be accepted.
2. **Active Conflict Prevention**: Within a single tenant, if an active document (`deleted_at IS NULL`) with hash $H$ already exists, attempting to insert another document with hash $H$ MUST fail with a unique constraint violation.
3. **Soft-Deletion Re-upload Support**: If a document with hash $H$ was soft-deleted (`deleted_at IS NOT NULL`), re-uploading the same file into that tenant MUST succeed, creating a fresh active document lifecycle.

### 3.2. Partial Unique Index Implementation
Prisma query indexes cover `(tenantId, contentHash)` for general lookups, while the PostgreSQL database enforces the partial uniqueness constraint:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS "documents_tenant_active_content_hash_key"
ON "documents" ("tenant_id", "content_hash")
WHERE "deleted_at" IS NULL;
```

---

## 4. Zero-Trust Security & Ownership

### 4.1. Cross-Tenant Isolation Enforcement
In multi-tenant architectures, foreign keys to departments or users could inadvertently cross tenant boundaries if not strictly verified. We implement an automated database trigger:

```sql
CREATE OR REPLACE FUNCTION check_document_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_dept_tenant_id UUID;
    v_uploader_tenant_id UUID;
BEGIN
    IF NEW."department_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_dept_tenant_id FROM "departments" WHERE "id" = NEW."department_id";
        IF v_dept_tenant_id IS NOT NULL AND v_dept_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Document Violation: Document tenant % does not match department tenant %',
                NEW."tenant_id", v_dept_tenant_id;
        END IF;
    END IF;

    IF NEW."uploaded_by_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_uploader_tenant_id FROM "users" WHERE "id" = NEW."uploaded_by_id";
        IF v_uploader_tenant_id IS NOT NULL AND v_uploader_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Document Violation: Document tenant % does not match uploader user tenant %',
                NEW."tenant_id", v_uploader_tenant_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_document_tenant_isolation ON "documents";
CREATE TRIGGER enforce_document_tenant_isolation
BEFORE INSERT OR UPDATE ON "documents"
FOR EACH ROW EXECUTE FUNCTION check_document_tenant_isolation();
```

### 4.2. Relational Deletion Policies
- **Department Deleted**: Sets `department_id = NULL` via `ON DELETE SET NULL`. Existing documents and vector chunks remain accessible to authorized tenant administrators.
- **User Deleted**: Sets `uploaded_by_id = NULL` via `ON DELETE SET NULL`. Preserves corporate documents, line-level citations, and audit lineage even after staff departure.
- **Tenant Deleted**: Executes `ON DELETE CASCADE`, cleanly purging all tenant documents and dependent records.

---

## 5. Performance Indexing Matrix

| Index Name | Indexed Columns | Index Purpose & Query Workflow |
| :--- | :--- | :--- |
| `documents_tenant_id_content_hash_idx` | `(tenant_id, content_hash)` | Fast pre-upload deduplication lookup |
| `documents_tenant_active_content_hash_key` | `(tenant_id, content_hash) WHERE deleted_at IS NULL` | Database-level unique constraint for active files |
| `documents_tenant_id_classification_idx` | `(tenant_id, classification)` | Security-filtered document retrieval and RBAC gating |
| `documents_tenant_id_status_idx` | `(tenant_id, status)` | Ingestion pipeline worker queues (polling `PENDING`/`PROCESSING`) |
| `documents_tenant_id_created_at_idx` | `(tenant_id, created_at DESC)` | Tenant document catalog pagination & timeline sorting |
| `documents_uploaded_by_id_idx` | `(uploaded_by_id)` | User upload history and governance views |
| `documents_department_id_idx` | `(department_id)` | Departmental workspace filtering |

---

## 6. Example Usage Queries

### 6.1. Document Creation with Deduplication Check

```typescript
import { PrismaClient, DocumentClassification, DocumentIngestionStatus } from '@prisma/client';
import crypto from 'crypto';

const prisma = new PrismaClient();

async function uploadDocument(params: {
  tenantId: string;
  departmentId?: string;
  uploaderId?: string;
  originalFilename: string;
  buffer: Buffer;
  storageUri: string;
  mimeType: string;
  classification?: DocumentClassification;
}) {
  const contentHash = crypto.createHash('sha256').update(params.buffer).digest('hex');
  const normalizedFilename = params.originalFilename.trim().toLowerCase().replace(/\s+/g, '_');

  // Step 1: Pre-upload tenant-scoped deduplication check
  const existingActiveDoc = await prisma.document.findFirst({
    where: {
      tenantId: params.tenantId,
      contentHash: contentHash,
      deletedAt: null,
    },
  });

  if (existingActiveDoc) {
    return {
      status: 'DUPLICATE_FOUND',
      document: existingActiveDoc,
    };
  }

  // Step 2: Persist document metadata
  const doc = await prisma.document.create({
    data: {
      tenantId: params.tenantId,
      departmentId: params.departmentId,
      uploadedById: params.uploaderId,
      originalFilename: params.originalFilename,
      normalizedFilename: normalizedFilename,
      storageUri: params.storageUri,
      mimeType: params.mimeType,
      fileSizeBytes: BigInt(params.buffer.length),
      contentHash: contentHash,
      classification: params.classification ?? DocumentClassification.INTERNAL,
      status: DocumentIngestionStatus.PENDING,
      metadata: {
        checksumAlgorithm: 'SHA-256',
        uploadedAt: new Date().toISOString(),
      },
    },
  });

  return {
    status: 'CREATED',
    document: doc,
  };
}
```

### 6.2. Soft Deletion Query

```typescript
async function softDeleteDocument(documentId: string, tenantId: string) {
  return await prisma.document.update({
    where: {
      id: documentId,
      tenantId: tenantId,
    },
    data: {
      deletedAt: new Date(),
      status: DocumentIngestionStatus.CANCELLED,
    },
  });
}
```

### 6.3. Permission-Aware Retrieval Query

```typescript
async function getAccessibleDocuments(params: {
  tenantId: string;
  maxClassification: DocumentClassification;
  departmentId?: string;
}) {
  // Ordered classification hierarchy
  const classificationHierarchy: Record<DocumentClassification, DocumentClassification[]> = {
    PUBLIC: ['PUBLIC'],
    INTERNAL: ['PUBLIC', 'INTERNAL'],
    CONFIDENTIAL: ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL'],
    RESTRICTED: ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED'],
  };

  return await prisma.document.findMany({
    where: {
      tenantId: params.tenantId,
      deletedAt: null,
      status: DocumentIngestionStatus.COMPLETED,
      classification: {
        in: classificationHierarchy[params.maxClassification],
      },
      ...(params.departmentId ? { departmentId: params.departmentId } : {}),
    },
    orderBy: { createdAt: 'desc' },
  });
}
```

---

## 7. Verification Test Suite Execution Results

The verification script `backend/test_document_management.js` executed 9 distinct test phases:

```bash
$ node backend/test_document_management.js
🚀 Starting Task 06: Document Metadata & Classification Management Verification...

Step 1: Setting up test tenants, departments, and users...
  Tenant A: 5f5fdde5-c4c6-48f1-8300-c6f2de1e01ce (test-doc-tenant-a-1794928875523)
  Tenant B: ad3d26c2-5f70-4ac2-86f8-b981266315bf (test-doc-tenant-b-1794928875523)
  User A:   5249f744-efa7-44e6-ab01-b74148b01503 in Dept A: da7c68cb-f465-4244-aee7-2c5ec22e5ded
  User B:   131843c6-3fca-4d7a-8266-44df89ee53fe in Dept B: a121f461-86b8-4ca4-81fa-7afe98f54fe3

Step 2: Creating document with full metadata and validating field types...
  Created Document 1: ID=5e431101-f20e-44e7-a07b-feec8583df3e
  Classification: CONFIDENTIAL, Status: PENDING, Size: 2548920 bytes
  Content Hash: 74a53998b327e8e0ea673ed568b15f601b00fca9e0fcb37cc1a7e1205f1d0366
  PASSED: Document creation and type validation successful.

Step 3: Verifying all DocumentClassification and DocumentIngestionStatus enum values...
  Verified classifications: PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED
  Verified statuses: PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED
  PASSED: All classification and ingestion status enums persisted and retrieved.

Step 4: Testing tenant-aware SHA-256 deduplication (active duplicate collision)...
  PASSED: Duplicate active document rejected by unique index: Unique constraint failed on the fields: (`tenant_id`,`content_hash`)

Step 5: Testing multi-tenant deduplication (identical hash allowed in different tenant)...
  Created Document in Tenant B with identical hash: ID=0f3f3f2d-410a-42a3-8f7a-e31b77ffb067
  PASSED: Multi-tenant isolation permits identical file content across distinct tenants.

Step 6: Testing soft-deletion aware deduplication...
  Soft-deleted doc1 in Tenant A: deletedAt=2026-11-17T15:21:15.678Z
  Successfully re-uploaded document with identical hash: ID=16bde583-e8fc-4282-8101-b758c11eee5b
  PASSED: Soft-deleted document allows re-uploading identical file content in same tenant.
  PASSED: Third duplicate correctly rejected while re-uploaded document is active.

Step 7: Testing database-level zero-trust cross-tenant boundary trigger...
  PASSED: Cross-tenant department reference rejected: ConnectorError(PostgresError: Cross-Tenant Document Violation)
  PASSED: Cross-tenant uploader reference rejected: ConnectorError(PostgresError: Cross-Tenant Document Violation)

Step 8: Testing performance and retrieval queries against composite indexes...
  PASSED: Query by (tenantId, contentHash) verified.
  PASSED: Query by (tenantId, classification) verified: 2 confidential doc(s).
  PASSED: Query by (tenantId, status) verified: 2 processing doc(s).
  PASSED: Query by (tenantId, createdAt DESC) timeline verified: 5 doc(s) returned.
  PASSED: Query by uploadedById verified: 2 doc(s) uploaded by Alice.
  PASSED: Query by departmentId verified: 2 doc(s) in Legal & Compliance.

Step 9: Testing foreign key preservation (SetNull) and tenant cascade...
  PASSED: Department deletion sets departmentId to NULL (ON DELETE SET NULL), document preserved.
  PASSED: User deletion sets uploadedById to NULL (ON DELETE SET NULL), document preserved.
  PASSED: Tenant deletion cascade removes all child documents.

======================================================
🎉 ALL TASK 06 ACCEPTANCE CRITERIA VERIFIED SUCCESSFULLY!
======================================================
```

---

## 8. Acceptance Criteria Verification Checklist

| Criterion | Requirement | Verification Method | Status |
| :--- | :--- | :--- | :--- |
| **Document Metadata Persistence** | UUID, filenames, URI, MIME, BigInt size, Hash, Timestamps | Step 2 in test suite | ✅ PASS |
| **Classification Enforcement** | `PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `RESTRICTED` | Step 3 in test suite | ✅ PASS |
| **Ingestion Status Enforcement** | `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `CANCELLED` | Step 3 in test suite | ✅ PASS |
| **Tenant-Aware Deduplication** | Active duplicates blocked within tenant; identical hash allowed across tenants | Step 4 & 5 in test suite | ✅ PASS |
| **Soft-Deletion Sensitivity** | Soft-deleted documents do not block re-upload of same hash | Step 6 in test suite | ✅ PASS |
| **Zero-Trust Cross-Tenant Trigger** | Department & Uploader tenant must match Document tenant | Step 7 in test suite | ✅ PASS |
| **Performance Indexing** | Fast queries on hash, classification, status, timeline, user, dept | Step 8 in test suite | ✅ PASS |
| **Relational Preservation** | Dept/User removal sets null; Tenant removal cascades | Step 9 in test suite | ✅ PASS |
| **Zero Migration Regressions** | Task 01 to 05 test suites continue passing | Regression test run | ✅ PASS |
