# Task 07: Document Versioning & Historical Lineage – Implementation Report

**Status**: Completed  
**Engineer**: Senior Backend & Security Architect  
**Date**: November 18, 2026  
**Environment**: Conda `ml-env` (PostgreSQL 16+ with pgvector)  

---

## 1. Executive Summary & Objective

The objective of Task 07 is to extend the enterprise document management architecture to support **immutable document version history**, **non-destructive revisions**, **traceable document lineage**, and **zero-trust cross-tenant isolation**.

In an enterprise RAG knowledge platform, documents evolve over time as organizational policies, technical standards, and contracts are amended. To guarantee factual consistency, citation verifiability, and audit compliance, downstream retrieval components and document chunks must strictly associate with specific document version snapshots. Chunks from obsolete versions must never be silently commingled with content from the latest version.

### Key Architectural Highlights:
1. **Core `DocumentVersion` Model**:
   - Represents an immutable snapshot of a document at a specific point in time.
   - Required attributes: UUID PK (`id`), `documentId` FK, `tenantId` FK, `versionNumber` (`Int`), `storageUri` (`Text`), `originalFilename` (`VarChar(255)`), `mimeType` (`VarChar(100)`), `fileSizeBytes` (`BigInt`), `contentHash` (`VarChar(64)`), `status` (`DocumentIngestionStatus`), `metadata` (`Json`), `isCurrent` (`Boolean`), `createdAt` (`Timestamptz(6)`), and `createdById` (`User` FK).
2. **Version Uniqueness & Sequencing**:
   - Unique constraint `UNIQUE (document_id, version_number)` guarantees each version has a strictly unique, monotonically increasing version number within its parent document.
   - Check constraint `CHECK (version_number > 0)` prevents invalid version counters.
3. **Dual Identification of Current Version**:
   - Direct pointer on `Document`: `currentVersionId UUID? @unique` linking to `DocumentVersion(id)`.
   - Boolean flag on `DocumentVersion`: `is_current BOOLEAN` backed by partial unique index `UNIQUE (document_id) WHERE is_current = TRUE AND deleted_at IS NULL`, guaranteeing that at most one active version is flagged as current at the PostgreSQL engine level.
   - Bidirectional triggers (`trg_sync_document_version_current_status_before`, `trg_sync_document_version_current_status_after`, `trg_sync_document_current_version_pointer`) maintain automatic consistency between `Document.currentVersionId` and `DocumentVersion.isCurrent`.
4. **Historical Metadata Preservation**:
   - When version $N+1$ is uploaded, version $N$'s storage URI, extraction metadata, SHA-256 content hash, and creation timestamp remain completely immutable.
5. **Zero-Trust Cross-Tenant Boundary Enforcement**:
   - Database trigger `enforce_document_version_tenant_isolation` guarantees that `DocumentVersion.tenantId` strictly matches `Document.tenantId` and `User.tenantId` (for creator attribution).
   - Database trigger `check_document_tenant_isolation` prevents assigning a `currentVersionId` from another tenant or another document.
6. **Retention & Lineage Lifecycle**:
   - Soft-deletion: `deleted_at` timestamp supports archiving versions while preserving compliance and audit trails.
   - User departure: `createdById` has `onDelete: SetNull`, preserving provenance.
   - Document deletion: Cascades to remove all child versions (`onDelete: Cascade`).
   - Tenant deprovisioning: Cascades to remove all documents and versions.
   - Downstream chunk lineage: Vector chunks and processing jobs reference `document_version_id`, preventing stale version chunks from leaking into live search results.

---

## 2. Implemented Data Model & Relations

### 2.1. Prisma Model Definition

```prisma
// ==========================================
// DOCUMENT METADATA & CLASSIFICATION MANAGEMENT
// ==========================================

model Document {
  id                 String                  @id @default(uuid()) @db.Uuid
  tenantId           String                  @map("tenant_id") @db.Uuid
  departmentId       String?                 @map("department_id") @db.Uuid
  uploadedById       String?                 @map("uploaded_by_id") @db.Uuid
  currentVersionId   String?                 @unique @map("current_version_id") @db.Uuid
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
  tenant         Tenant            @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  department     Department?       @relation(fields: [departmentId], references: [id], onDelete: SetNull)
  uploadedBy     User?             @relation("DocumentUploader", fields: [uploadedById], references: [id], onDelete: SetNull)
  currentVersion DocumentVersion?  @relation("DocumentCurrentVersion", fields: [currentVersionId], references: [id], onDelete: SetNull)
  versions       DocumentVersion[] @relation("DocumentVersions")

  // Query Optimization & Deduplication Indexes
  @@index([tenantId, contentHash])
  @@index([tenantId, classification])
  @@index([tenantId, status])
  @@index([tenantId, createdAt(sort: Desc)])
  @@index([uploadedById])
  @@index([departmentId])
  @@map("documents")
}

// ==========================================
// DOCUMENT VERSIONING & HISTORICAL LINEAGE
// ==========================================

model DocumentVersion {
  id               String                  @id @default(uuid()) @db.Uuid
  documentId       String                  @map("document_id") @db.Uuid
  tenantId         String                  @map("tenant_id") @db.Uuid
  createdById      String?                 @map("created_by_id") @db.Uuid
  versionNumber    Int                     @map("version_number")
  storageUri       String                  @map("storage_uri") @db.Text
  originalFilename String                  @map("original_filename") @db.VarChar(255)
  mimeType         String                  @map("mime_type") @db.VarChar(100)
  fileSizeBytes    BigInt                  @map("file_size_bytes")
  contentHash      String                  @map("content_hash") @db.VarChar(64)
  status           DocumentIngestionStatus @default(PENDING)
  metadata         Json?                   @default("{}")
  isCurrent        Boolean                 @default(false) @map("is_current")
  createdAt        DateTime                @default(now()) @map("created_at") @db.Timestamptz(6)
  deletedAt        DateTime?               @map("deleted_at") @db.Timestamptz(6)

  // Relationships
  document           Document  @relation("DocumentVersions", fields: [documentId], references: [id], onDelete: Cascade)
  currentForDocument Document? @relation("DocumentCurrentVersion")
  tenant             Tenant    @relation(fields: [tenantId], references: [id], onDelete: Cascade)
  createdBy          User?     @relation("DocumentVersionCreator", fields: [createdById], references: [id], onDelete: SetNull)

  // Optimization, Lineage & Tenant Isolation Indexes
  @@unique([documentId, versionNumber])
  @@index([tenantId, documentId])
  @@index([tenantId, contentHash])
  @@index([tenantId, status])
  @@index([documentId, isCurrent])
  @@index([createdById])
  @@index([createdAt(sort: Desc)])
  @@map("document_versions")
}
```

### 2.2. Field Specifications Table

| Field | Type | Attributes & Modifiers | Description |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `@id @default(uuid()) @db.Uuid` | Immutable surrogate primary key |
| `document_id` | `UUID` | Not Null, Foreign Key | Reference to parent `Document` (`onDelete: Cascade`) |
| `tenant_id` | `UUID` | Not Null, Foreign Key | Owning tenant identifier (`onDelete: Cascade`) |
| `created_by_id` | `UUID?` | Nullable, Foreign Key | Creator user reference (`onDelete: SetNull`) |
| `version_number`| `INTEGER` | Not Null, `CHECK (version_number > 0)` | Strictly sequential version index within document |
| `storage_uri` | `TEXT` | Not Null | S3/Object Storage URI of the physical file snapshot |
| `original_filename` | `VARCHAR(255)` | Not Null | Original filename at the time of version upload |
| `mime_type` | `VARCHAR(100)` | Not Null | MIME type (e.g. `application/pdf`, `text/csv`) |
| `file_size_bytes` | `BIGINT` | Not Null | Exact physical file size in bytes |
| `content_hash` | `VARCHAR(64)` | Not Null | SHA-256 checksum of the version's payload |
| `status` | `DocumentIngestionStatus` | Default `PENDING` | Pipeline lifecycle status of the version |
| `metadata` | `JSONB` | Default `{}` | Extensible extraction metadata (page count, OCR confidence, etc.) |
| `is_current` | `BOOLEAN` | Default `false` | True if this is the active current version |
| `created_at` | `TIMESTAMPTZ(6)`| Default `now()` | Immutable timestamp of version creation |
| `deleted_at` | `TIMESTAMPTZ(6)`| Nullable | Soft deletion timestamp |

---

## 3. Database Migration & Constraints

Migration file: `backend/migrations/20261118100000_document_versioning_historical_lineage/migration.sql`

### 3.1. Version Number Uniqueness & Single Current Version Constraints
```sql
-- Version number must be unique per document
CREATE UNIQUE INDEX "document_versions_document_id_version_number_key"
ON "document_versions" ("document_id", "version_number");

-- At most one active current version per document
CREATE UNIQUE INDEX "document_versions_single_current_per_doc_idx"
ON "document_versions" ("document_id")
WHERE "is_current" = TRUE AND "deleted_at" IS NULL;
```

### 3.2. Zero-Trust Cross-Tenant Boundary Triggers
```sql
CREATE OR REPLACE FUNCTION check_document_version_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_doc_tenant_id UUID;
    v_creator_tenant_id UUID;
BEGIN
    SELECT "tenant_id" INTO v_doc_tenant_id FROM "documents" WHERE "id" = NEW."document_id";
    IF v_doc_tenant_id IS NOT NULL AND v_doc_tenant_id != NEW."tenant_id" THEN
        RAISE EXCEPTION 'Cross-Tenant Document Version Violation: DocumentVersion tenant % does not match document tenant %',
            NEW."tenant_id", v_doc_tenant_id;
    END IF;

    IF NEW."created_by_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_creator_tenant_id FROM "users" WHERE "id" = NEW."created_by_id";
        IF v_creator_tenant_id IS NOT NULL AND v_creator_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Document Version Violation: DocumentVersion tenant % does not match creator user tenant %',
                NEW."tenant_id", v_creator_tenant_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER enforce_document_version_tenant_isolation
BEFORE INSERT OR UPDATE ON "document_versions"
FOR EACH ROW EXECUTE FUNCTION check_document_version_tenant_isolation();
```

### 3.3. Automatic Current Version Synchronization Triggers
```sql
-- BEFORE Trigger: Demote previous current version before inserting/updating new current version
CREATE OR REPLACE FUNCTION sync_document_version_current_status_before()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW."is_current" = TRUE THEN
        UPDATE "document_versions"
        SET "is_current" = FALSE
        WHERE "document_id" = NEW."document_id"
          AND "id" != NEW."id"
          AND "is_current" = TRUE;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sync_document_version_current_status_before
BEFORE INSERT OR UPDATE OF "is_current" ON "document_versions"
FOR EACH ROW
WHEN (NEW."is_current" = TRUE)
EXECUTE FUNCTION sync_document_version_current_status_before();

-- AFTER Trigger: Synchronize parent document's current_version_id and top-level attributes
CREATE OR REPLACE FUNCTION sync_document_version_current_status_after()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW."is_current" = TRUE THEN
        UPDATE "documents"
        SET "current_version_id" = NEW."id",
            "original_filename" = NEW."original_filename",
            "storage_uri" = NEW."storage_uri",
            "mime_type" = NEW."mime_type",
            "file_size_bytes" = NEW."file_size_bytes",
            "content_hash" = NEW."content_hash",
            "status" = NEW."status",
            "updated_at" = CURRENT_TIMESTAMP
        WHERE "id" = NEW."document_id";
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sync_document_version_current_status_after
AFTER INSERT OR UPDATE OF "is_current" ON "document_versions"
FOR EACH ROW
WHEN (NEW."is_current" = TRUE)
EXECUTE FUNCTION sync_document_version_current_status_after();
```

---

## 4. Example Query Patterns

### 4.1. Creating a New Version

```javascript
// Step A: Determine next version number
const latest = await prisma.documentVersion.findFirst({
  where: { documentId: doc.id },
  orderBy: { versionNumber: 'desc' },
});
const nextVersionNumber = latest ? latest.versionNumber + 1 : 1;

// Step B: Insert the new version (database trigger automatically handles demotion & parent sync)
const newVersion = await prisma.documentVersion.create({
  data: {
    documentId: doc.id,
    tenantId: doc.tenantId,
    createdById: userId,
    versionNumber: nextVersionNumber,
    storageUri: 's3://enterprise-vault/alpha/docs/handbook_v2.pdf',
    originalFilename: 'employee_handbook_v2.pdf',
    mimeType: 'application/pdf',
    fileSizeBytes: 2097152n,
    contentHash: computeSha256(fileContent),
    status: 'COMPLETED',
    metadata: {
      pageCount: 30,
      author: 'HR & Legal',
      changeSummary: 'Updated parental leave policy and annual bonus schedules',
    },
    isCurrent: true,
  },
});
```

### 4.2. Retrieving Version History

```javascript
// Chronological version history filtered by tenant and active status
const versionHistory = await prisma.documentVersion.findMany({
  where: {
    documentId: doc.id,
    tenantId: tenantId, // Strict tenant scoping
    deletedAt: null,    // Filter out soft-deleted versions
  },
  orderBy: { versionNumber: 'desc' },
  include: {
    createdBy: {
      select: { id: true, fullName: true, email: true },
    },
  },
});
```

### 4.3. Identifying the Current Version

```javascript
// Pattern A: Via Document relationship (single query)
const doc = await prisma.document.findUnique({
  where: { id: documentId },
  include: { currentVersion: true },
});
const currentVersion = doc.currentVersion;

// Pattern B: Direct query via isCurrent flag
const currentVersionDirect = await prisma.documentVersion.findFirst({
  where: {
    documentId: documentId,
    tenantId: tenantId,
    isCurrent: true,
    deletedAt: null,
  },
});
```

### 4.4. Retrieving a Specific Historical Version

```javascript
// Retrieve specific version by compound key
const historicalVersion = await prisma.documentVersion.findUnique({
  where: {
    documentId_versionNumber: {
      documentId: documentId,
      versionNumber: 1,
    },
  },
  include: {
    createdBy: { select: { fullName: true, email: true } },
  },
});
```

---

## 5. Test Suite & Verification Results

Verification suite: `backend/test_document_versioning.js`

### Execution Output:
```text
🚀 Starting Task 07: Document Versioning & Historical Lineage Verification...

Step 1: Setting up test tenants, departments, and users...
  Tenant A: 32f6d6a4-17d4-4e3e-a0a9-6d7acea339ae (test-version-tenant-a-1794971528780)
  Tenant B: b430b47a-1c8e-420e-a0f4-4ed31a0c8533 (test-version-tenant-b-1794971528780)
  User Alice (Tenant A): 3b1a1d17-0447-4e1d-8538-09a2a60e1572
  User Bob (Tenant B):   53915763-4e55-42a5-91ff-adddfd1456b3

Step 2: Creating parent document and Version 1 (Initial Snapshot)...
  PASSED: Document and Version 1 created. Current version synchronized.

Step 3: Creating Version 2 (Non-destructive update with preserved historical metadata)...
  PASSED: Version 2 created. Historical Version 1 metadata remains 100% immutable.

Step 4: Creating Version 3 (Testing multiple versions per document)...
  PASSED: Multiple versions supported (Document has 3 active versions).

Step 5: Testing version number uniqueness per document...
  PASSED: Duplicate version number rejected by unique constraint.

Step 6: Executing Example Query – Retrieving chronological version history...
  Retrieved 3 versions in history:
    - v3: corporate_governance_v3.pdf (Current: true, Size: 3145728b, Hash: 3a4b23ebc042...)
    - v2: corporate_governance_v2.pdf (Current: false, Size: 2097152b, Hash: 6fcbcc9edadc...)
    - v1: corporate_governance_v1.pdf (Current: false, Size: 1048576b, Hash: afd4f5fa42a8...)
  PASSED: Version history retrieved successfully.

Step 7: Executing Example Query – Identifying Current Version...
  PASSED: Current version identified via both Document relation and isCurrent flag (v3).

Step 8: Executing Example Query – Retrieving a Specific Historical Version (v1)...
  Retrieved historical version: v1 (corporate_governance_v1.pdf, Pages: 15)
  PASSED: Specific historical version retrieved accurately.

Step 9: Testing Zero-Trust Cross-Tenant Boundary Triggers...
  PASSED: Cross-tenant document version assignment blocked by database trigger.
  PASSED: Cross-tenant creator reference blocked by database trigger.
  PASSED: Mismatched current_version_id blocked by validation trigger.

Step 10: Testing Deletion, Retention Policies and Lineage Isolation...
  PASSED: Soft deletion preserves compliance lineage while filtering active history.
  PASSED: User deletion preserves version record and metadata (createdById set to null).
  PASSED: Document hard deletion cascades to child document versions.
  PASSED: Downstream chunk lineage isolation verified. Current version ID (cf31a6ca-da2f-4ab5-ba7a-51c80745b17b) eliminates stale version chunk leakage.
  PASSED: Tenant cascade deprovisioning removes all documents and versions.

======================================================
🎉 ALL TASK 07 ACCEPTANCE CRITERIA VERIFIED SUCCESSFULLY!
======================================================
```

Regression test suite: `backend/test_document_management.js`, `backend/test_session_management.js`, `backend/test_audit_logging.js` all pass with 100% success.
