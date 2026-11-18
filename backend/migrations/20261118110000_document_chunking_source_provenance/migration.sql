-- Migration: Document Chunking & Source Provenance
-- Target Database: PostgreSQL 16+ with pgvector extension enabled

-- ============================================================================
-- 1. CREATE DOCUMENT_CHUNKS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS "document_chunks" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenant_id" UUID NOT NULL,
    "document_id" UUID NOT NULL,
    "document_version_id" UUID,
    "chunk_index" INTEGER NOT NULL,
    "content" TEXT NOT NULL,
    "content_hash" VARCHAR(64) NOT NULL,
    "token_count" INTEGER NOT NULL,
    "embedding_id" VARCHAR(255),
    "vector_storage_id" VARCHAR(255),
    "metadata" JSONB DEFAULT '{}',
    "page_number" INTEGER,
    "section_heading" TEXT,
    "sheet_name" VARCHAR(255),
    "row_range" VARCHAR(100),
    "char_start" INTEGER,
    "char_end" INTEGER,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "document_chunks_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "document_chunks_chunk_index_check" CHECK ("chunk_index" >= 0),
    CONSTRAINT "document_chunks_token_count_check" CHECK ("token_count" >= 0),
    CONSTRAINT "document_chunks_page_number_check" CHECK ("page_number" IS NULL OR "page_number" > 0),
    CONSTRAINT "document_chunks_char_start_check" CHECK ("char_start" IS NULL OR "char_start" >= 0),
    CONSTRAINT "document_chunks_char_end_check" CHECK ("char_end" IS NULL OR "char_end" >= 0),
    CONSTRAINT "document_chunks_char_offset_check" CHECK ("char_start" IS NULL OR "char_end" IS NULL OR "char_start" <= "char_end")
);

-- ============================================================================
-- 2. FOREIGN KEY CONSTRAINTS
-- ============================================================================

-- document_chunks.tenant_id -> tenants.id (CASCADE on tenant deprovisioning)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'document_chunks_tenant_id_fkey' AND table_name = 'document_chunks'
    ) THEN
        ALTER TABLE "document_chunks"
            ADD CONSTRAINT "document_chunks_tenant_id_fkey"
            FOREIGN KEY ("tenant_id")
            REFERENCES "tenants"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- document_chunks.document_id -> documents.id (CASCADE on document hard delete)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'document_chunks_document_id_fkey' AND table_name = 'document_chunks'
    ) THEN
        ALTER TABLE "document_chunks"
            ADD CONSTRAINT "document_chunks_document_id_fkey"
            FOREIGN KEY ("document_id")
            REFERENCES "documents"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- document_chunks.document_version_id -> document_versions.id (CASCADE on version hard delete)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'document_chunks_document_version_id_fkey' AND table_name = 'document_chunks'
    ) THEN
        ALTER TABLE "document_chunks"
            ADD CONSTRAINT "document_chunks_document_version_id_fkey"
            FOREIGN KEY ("document_version_id")
            REFERENCES "document_versions"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- ============================================================================
-- 3. UNIQUE CONSTRAINTS & PERFORMANCE INDEXES
-- ============================================================================

-- Deterministic chunk index ordering unique per document version
CREATE UNIQUE INDEX IF NOT EXISTS "document_chunks_version_chunk_index_unique"
ON "document_chunks" ("document_version_id", "chunk_index")
WHERE "document_version_id" IS NOT NULL;

-- Ordered retrieval indexes
CREATE INDEX IF NOT EXISTS "document_chunks_document_id_chunk_index_idx"
ON "document_chunks" ("document_id", "chunk_index");

CREATE INDEX IF NOT EXISTS "document_chunks_document_version_id_chunk_index_idx"
ON "document_chunks" ("document_version_id", "chunk_index");

-- Multi-tenant security isolation indexes
CREATE INDEX IF NOT EXISTS "document_chunks_tenant_id_idx"
ON "document_chunks" ("tenant_id");

CREATE INDEX IF NOT EXISTS "document_chunks_tenant_id_document_id_idx"
ON "document_chunks" ("tenant_id", "document_id");

CREATE INDEX IF NOT EXISTS "document_chunks_tenant_id_document_version_id_idx"
ON "document_chunks" ("tenant_id", "document_version_id");

-- Source lineage coordinates indexes for citation and evidence inspection
CREATE INDEX IF NOT EXISTS "document_chunks_document_id_page_number_idx"
ON "document_chunks" ("document_id", "page_number");

CREATE INDEX IF NOT EXISTS "document_chunks_document_id_section_heading_idx"
ON "document_chunks" ("document_id", "section_heading");

CREATE INDEX IF NOT EXISTS "document_chunks_document_id_sheet_name_idx"
ON "document_chunks" ("document_id", "sheet_name");

-- Content hash deduplication index
CREATE INDEX IF NOT EXISTS "document_chunks_content_hash_idx"
ON "document_chunks" ("content_hash");

-- Vector storage references for external vector DB sync (e.g. Qdrant point IDs)
CREATE INDEX IF NOT EXISTS "document_chunks_embedding_id_idx"
ON "document_chunks" ("embedding_id");

CREATE INDEX IF NOT EXISTS "document_chunks_vector_storage_id_idx"
ON "document_chunks" ("vector_storage_id");

-- Chronological ordering index
CREATE INDEX IF NOT EXISTS "document_chunks_created_at_idx"
ON "document_chunks" ("created_at" DESC);

-- ============================================================================
-- 4. FULL-TEXT SEARCH (GIN) INDEX FOR BM25 SPARSE RETRIEVAL
-- ============================================================================

CREATE INDEX IF NOT EXISTS "document_chunks_content_fts_idx"
ON "document_chunks"
USING gin (to_tsvector('simple', "content"));

-- ============================================================================
-- 5. ZERO-TRUST CROSS-TENANT BOUNDARY VALIDATION TRIGGER
-- ============================================================================

CREATE OR REPLACE FUNCTION check_document_chunk_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_doc_tenant_id UUID;
    v_version_tenant_id UUID;
    v_version_doc_id UUID;
BEGIN
    -- 1. Verify parent document belongs to the same tenant
    SELECT "tenant_id" INTO v_doc_tenant_id FROM "documents" WHERE "id" = NEW."document_id";
    IF v_doc_tenant_id IS NOT NULL AND v_doc_tenant_id != NEW."tenant_id" THEN
        RAISE EXCEPTION 'Cross-Tenant Document Chunk Violation: DocumentChunk tenant % does not match document tenant %',
            NEW."tenant_id", v_doc_tenant_id;
    END IF;

    -- 2. If document_version_id is specified, verify it belongs to the same tenant and same document
    IF NEW."document_version_id" IS NOT NULL THEN
        SELECT "tenant_id", "document_id" INTO v_version_tenant_id, v_version_doc_id
        FROM "document_versions" WHERE "id" = NEW."document_version_id";
        
        IF v_version_tenant_id IS NOT NULL AND v_version_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Document Chunk Violation: DocumentChunk tenant % does not match document version tenant %',
                NEW."tenant_id", v_version_tenant_id;
        END IF;

        IF v_version_doc_id IS NOT NULL AND v_version_doc_id != NEW."document_id" THEN
            RAISE EXCEPTION 'Document Version Chunk Mismatch: document_version_id % does not belong to document %',
                NEW."document_version_id", NEW."document_id";
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_document_chunk_tenant_isolation ON "document_chunks";
CREATE TRIGGER enforce_document_chunk_tenant_isolation
BEFORE INSERT OR UPDATE ON "document_chunks"
FOR EACH ROW EXECUTE FUNCTION check_document_chunk_tenant_isolation();
