-- Migration: Document Versioning & Historical Lineage
-- Target Database: PostgreSQL 16+ with pgvector extension enabled

-- ============================================================================
-- 1. CREATE DOCUMENT_VERSIONS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS "document_versions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "document_id" UUID NOT NULL,
    "tenant_id" UUID NOT NULL,
    "created_by_id" UUID,
    "version_number" INTEGER NOT NULL,
    "storage_uri" TEXT NOT NULL,
    "original_filename" VARCHAR(255) NOT NULL,
    "mime_type" VARCHAR(100) NOT NULL,
    "file_size_bytes" BIGINT NOT NULL,
    "content_hash" VARCHAR(64) NOT NULL,
    "status" "DocumentIngestionStatus" NOT NULL DEFAULT 'PENDING',
    "metadata" JSONB DEFAULT '{}',
    "is_current" BOOLEAN NOT NULL DEFAULT FALSE,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "deleted_at" TIMESTAMPTZ(6),

    CONSTRAINT "document_versions_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "document_versions_version_number_check" CHECK ("version_number" > 0)
);

-- ============================================================================
-- 2. ALTER DOCUMENTS TABLE: ADD CURRENT_VERSION_ID
-- ============================================================================

ALTER TABLE "documents" 
ADD COLUMN IF NOT EXISTS "current_version_id" UUID;

-- Ensure unique constraint on documents.current_version_id
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'documents_current_version_id_key'
    ) THEN
        ALTER TABLE "documents" ADD CONSTRAINT "documents_current_version_id_key" UNIQUE ("current_version_id");
    END IF;
END $$;

-- ============================================================================
-- 3. UNIQUE CONSTRAINTS & PERFORMANCE INDEXES
-- ============================================================================

-- Version number must be unique per document
CREATE UNIQUE INDEX IF NOT EXISTS "document_versions_document_id_version_number_key"
ON "document_versions" ("document_id", "version_number");

-- At most one active current version per document
CREATE UNIQUE INDEX IF NOT EXISTS "document_versions_single_current_per_doc_idx"
ON "document_versions" ("document_id")
WHERE "is_current" = TRUE AND "deleted_at" IS NULL;

-- Fast lineage lookup for active and historical versions per document
CREATE INDEX IF NOT EXISTS "document_versions_document_id_is_current_idx"
ON "document_versions" ("document_id", "is_current");

-- Tenant isolation and fast listing index
CREATE INDEX IF NOT EXISTS "document_versions_tenant_id_document_id_idx"
ON "document_versions" ("tenant_id", "document_id");

-- Content hash lookup per tenant (deduplication & lineage tracking)
CREATE INDEX IF NOT EXISTS "document_versions_tenant_id_content_hash_idx"
ON "document_versions" ("tenant_id", "content_hash");

-- Status index for ingestion pipeline workers
CREATE INDEX IF NOT EXISTS "document_versions_tenant_id_status_idx"
ON "document_versions" ("tenant_id", "status");

-- Creator index for audit history
CREATE INDEX IF NOT EXISTS "document_versions_created_by_id_idx"
ON "document_versions" ("created_by_id");

-- Chronological version history index
CREATE INDEX IF NOT EXISTS "document_versions_created_at_idx"
ON "document_versions" ("created_at" DESC);

-- ============================================================================
-- 4. FOREIGN KEY CONSTRAINTS
-- ============================================================================

-- document_versions.document_id -> documents.id (CASCADE on document hard delete)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'document_versions_document_id_fkey' AND table_name = 'document_versions'
    ) THEN
        ALTER TABLE "document_versions"
            ADD CONSTRAINT "document_versions_document_id_fkey"
            FOREIGN KEY ("document_id")
            REFERENCES "documents"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- document_versions.tenant_id -> tenants.id (CASCADE on tenant deprovisioning)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'document_versions_tenant_id_fkey' AND table_name = 'document_versions'
    ) THEN
        ALTER TABLE "document_versions"
            ADD CONSTRAINT "document_versions_tenant_id_fkey"
            FOREIGN KEY ("tenant_id")
            REFERENCES "tenants"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- document_versions.created_by_id -> users.id (SET NULL on user departure to preserve version lineage)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'document_versions_created_by_id_fkey' AND table_name = 'document_versions'
    ) THEN
        ALTER TABLE "document_versions"
            ADD CONSTRAINT "document_versions_created_by_id_fkey"
            FOREIGN KEY ("created_by_id")
            REFERENCES "users"("id")
            ON DELETE SET NULL
            ON UPDATE CASCADE;
    END IF;
END $$;

-- documents.current_version_id -> document_versions.id (SET NULL on version deletion)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'documents_current_version_id_fkey' AND table_name = 'documents'
    ) THEN
        ALTER TABLE "documents"
            ADD CONSTRAINT "documents_current_version_id_fkey"
            FOREIGN KEY ("current_version_id")
            REFERENCES "document_versions"("id")
            ON DELETE SET NULL
            ON UPDATE CASCADE;
    END IF;
END $$;

-- ============================================================================
-- 5. ZERO-TRUST CROSS-TENANT BOUNDARY VALIDATION TRIGGERS
-- ============================================================================

-- Guarantees document version strictly matches the parent document's tenant and creator's tenant
CREATE OR REPLACE FUNCTION check_document_version_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_doc_tenant_id UUID;
    v_creator_tenant_id UUID;
BEGIN
    -- Verify parent document belongs to the same tenant
    SELECT "tenant_id" INTO v_doc_tenant_id FROM "documents" WHERE "id" = NEW."document_id";
    IF v_doc_tenant_id IS NOT NULL AND v_doc_tenant_id != NEW."tenant_id" THEN
        RAISE EXCEPTION 'Cross-Tenant Document Version Violation: DocumentVersion tenant % does not match document tenant %',
            NEW."tenant_id", v_doc_tenant_id;
    END IF;

    -- Verify creator user belongs to the same tenant if specified
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

DROP TRIGGER IF EXISTS enforce_document_version_tenant_isolation ON "document_versions";
CREATE TRIGGER enforce_document_version_tenant_isolation
BEFORE INSERT OR UPDATE ON "document_versions"
FOR EACH ROW EXECUTE FUNCTION check_document_version_tenant_isolation();

-- Update check_document_tenant_isolation to also validate current_version_id
CREATE OR REPLACE FUNCTION check_document_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_dept_tenant_id UUID;
    v_uploader_tenant_id UUID;
    v_version_tenant_id UUID;
    v_version_doc_id UUID;
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

    IF NEW."current_version_id" IS NOT NULL THEN
        SELECT "tenant_id", "document_id" INTO v_version_tenant_id, v_version_doc_id
        FROM "document_versions" WHERE "id" = NEW."current_version_id";
        IF v_version_tenant_id IS NOT NULL AND v_version_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Document Violation: Document tenant % does not match current version tenant %',
                NEW."tenant_id", v_version_tenant_id;
        END IF;
        IF v_version_doc_id IS NOT NULL AND v_version_doc_id != NEW."id" THEN
            RAISE EXCEPTION 'Document Version Mismatch: current_version_id % does not belong to document %',
                NEW."current_version_id", NEW."id";
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 6. AUTOMATIC CURRENT VERSION SYNCHRONIZATION TRIGGERS
-- ============================================================================

-- Step A: BEFORE trigger demotes existing current version to FALSE before new current version is inserted/updated
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

DROP TRIGGER IF EXISTS trg_sync_document_version_current_status_before ON "document_versions";
CREATE TRIGGER trg_sync_document_version_current_status_before
BEFORE INSERT OR UPDATE OF "is_current" ON "document_versions"
FOR EACH ROW
WHEN (NEW."is_current" = TRUE)
EXECUTE FUNCTION sync_document_version_current_status_before();

-- Step B: AFTER trigger synchronizes parent document's current_version_id and current attributes
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

DROP TRIGGER IF EXISTS trg_sync_document_version_current_status ON "document_versions";
DROP TRIGGER IF EXISTS trg_sync_document_version_current_status_after ON "document_versions";
CREATE TRIGGER trg_sync_document_version_current_status_after
AFTER INSERT OR UPDATE OF "is_current" ON "document_versions"
FOR EACH ROW
WHEN (NEW."is_current" = TRUE)
EXECUTE FUNCTION sync_document_version_current_status_after();

-- Reverse sync: When documents.current_version_id is updated directly
CREATE OR REPLACE FUNCTION sync_document_current_version_pointer()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW."current_version_id" IS DISTINCT FROM OLD."current_version_id" THEN
        IF NEW."current_version_id" IS NOT NULL THEN
            UPDATE "document_versions"
            SET "is_current" = TRUE
            WHERE "id" = NEW."current_version_id"
              AND "is_current" = FALSE;

            UPDATE "document_versions"
            SET "is_current" = FALSE
            WHERE "document_id" = NEW."id"
              AND "id" != NEW."current_version_id"
              AND "is_current" = TRUE;
        ELSE
            UPDATE "document_versions"
            SET "is_current" = FALSE
            WHERE "document_id" = NEW."id"
              AND "is_current" = TRUE;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_document_current_version_pointer ON "documents";
CREATE TRIGGER trg_sync_document_current_version_pointer
AFTER UPDATE OF "current_version_id" ON "documents"
FOR EACH ROW EXECUTE FUNCTION sync_document_current_version_pointer();
