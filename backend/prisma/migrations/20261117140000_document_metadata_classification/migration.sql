-- Migration: Document Metadata & Classification Management
-- Target Database: PostgreSQL 16+ with pgvector extension enabled

-- ============================================================================
-- 1. CREATE ENUMS
-- ============================================================================

DO $$ BEGIN
    CREATE TYPE "DocumentClassification" AS ENUM ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE "DocumentIngestionStatus" AS ENUM ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- ============================================================================
-- 2. CREATE DOCUMENTS TABLE
-- ============================================================================

-- CreateTable: documents
CREATE TABLE IF NOT EXISTS "documents" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenant_id" UUID NOT NULL,
    "department_id" UUID,
    "uploaded_by_id" UUID,
    "original_filename" VARCHAR(255) NOT NULL,
    "normalized_filename" VARCHAR(255) NOT NULL,
    "storage_uri" TEXT NOT NULL,
    "mime_type" VARCHAR(100) NOT NULL,
    "file_size_bytes" BIGINT NOT NULL,
    "content_hash" VARCHAR(64) NOT NULL,
    "classification" "DocumentClassification" NOT NULL DEFAULT 'INTERNAL',
    "status" "DocumentIngestionStatus" NOT NULL DEFAULT 'PENDING',
    "metadata" JSONB DEFAULT '{}',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ(6),

    CONSTRAINT "documents_pkey" PRIMARY KEY ("id")
);

-- ============================================================================
-- 3. CREATE PERFORMANCE & QUERY OPTIMIZATION INDEXES
-- ============================================================================

-- Fast Content Hash / Deduplication Query Index
CREATE INDEX IF NOT EXISTS "documents_tenant_id_content_hash_idx"
ON "documents" ("tenant_id", "content_hash");

-- Classification Filtering Index (RBAC / retrieval authorization)
CREATE INDEX IF NOT EXISTS "documents_tenant_id_classification_idx"
ON "documents" ("tenant_id", "classification");

-- Ingestion Status Filtering Index (Pipeline worker queues & monitoring)
CREATE INDEX IF NOT EXISTS "documents_tenant_id_status_idx"
ON "documents" ("tenant_id", "status");

-- Tenant-Scoped Document Timeline Index (Chronological listing & cursor pagination)
CREATE INDEX IF NOT EXISTS "documents_tenant_id_created_at_idx"
ON "documents" ("tenant_id", "created_at" DESC);

-- Uploader Index (User document history & audit attribution)
CREATE INDEX IF NOT EXISTS "documents_uploaded_by_id_idx"
ON "documents" ("uploaded_by_id");

-- Department Index (Departmental document catalog & scope filtering)
CREATE INDEX IF NOT EXISTS "documents_department_id_idx"
ON "documents" ("department_id");

-- ============================================================================
-- 4. SOFT-DELETION AWARE TENANT-ISOLATED UNIQUE DEDUPLICATION INDEX
-- ============================================================================

-- Partial Unique Index:
-- 1. Enforces tenant-aware SHA-256 uniqueness across active (non-soft-deleted) documents.
-- 2. Identical hashes are explicitly permitted across distinct tenants (strict multi-tenancy).
-- 3. Soft-deleted documents (deleted_at IS NOT NULL) do not prevent re-uploading identical content.
CREATE UNIQUE INDEX IF NOT EXISTS "documents_tenant_active_content_hash_key"
ON "documents" ("tenant_id", "content_hash")
WHERE "deleted_at" IS NULL;

-- ============================================================================
-- 5. FOREIGN KEY CONSTRAINTS
-- ============================================================================

-- Foreign key linking document to tenant (CASCADE on tenant removal)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'documents_tenant_id_fkey' AND table_name = 'documents'
    ) THEN
        ALTER TABLE "documents"
            ADD CONSTRAINT "documents_tenant_id_fkey"
            FOREIGN KEY ("tenant_id")
            REFERENCES "tenants"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- Foreign key linking document to department (ON DELETE SET NULL to preserve document on department change)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'documents_department_id_fkey' AND table_name = 'documents'
    ) THEN
        ALTER TABLE "documents"
            ADD CONSTRAINT "documents_department_id_fkey"
            FOREIGN KEY ("department_id")
            REFERENCES "departments"("id")
            ON DELETE SET NULL
            ON UPDATE CASCADE;
    END IF;
END $$;

-- Foreign key linking document to uploader user (ON DELETE SET NULL to preserve document & provenance on user departure)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'documents_uploaded_by_id_fkey' AND table_name = 'documents'
    ) THEN
        ALTER TABLE "documents"
            ADD CONSTRAINT "documents_uploaded_by_id_fkey"
            FOREIGN KEY ("uploaded_by_id")
            REFERENCES "users"("id")
            ON DELETE SET NULL
            ON UPDATE CASCADE;
    END IF;
END $$;

-- ============================================================================
-- 6. AUTOMATIC UPDATED_AT TRIGGER
-- ============================================================================

CREATE OR REPLACE FUNCTION update_documents_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW."updated_at" = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_documents_updated_at ON "documents";
CREATE TRIGGER trg_documents_updated_at
BEFORE UPDATE ON "documents"
FOR EACH ROW EXECUTE FUNCTION update_documents_updated_at();

-- ============================================================================
-- 7. ZERO-TRUST CROSS-TENANT BOUNDARY VALIDATION TRIGGER
-- ============================================================================

-- Guarantees department and uploader user belong strictly to the document's tenant
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
