-- Migration: 20261119170000_vector_indexing_transactional_outbox
-- Description: Add OutboxEventStatus, OutboxEventType enums and vector_indexing_outbox table for transactional vector consistency

-- CreateEnum: OutboxEventStatus
DO $$ BEGIN
    CREATE TYPE "OutboxEventStatus" AS ENUM ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'DEAD_LETTER');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- CreateEnum: OutboxEventType
DO $$ BEGIN
    CREATE TYPE "OutboxEventType" AS ENUM ('UPSERT_VECTOR', 'DELETE_VECTOR');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- CreateTable: vector_indexing_outbox
CREATE TABLE IF NOT EXISTS "vector_indexing_outbox" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenant_id" UUID NOT NULL,
    "document_id" UUID NOT NULL,
    "chunk_id" UUID,
    "event_type" "OutboxEventType" NOT NULL DEFAULT 'UPSERT_VECTOR',
    "status" "OutboxEventStatus" NOT NULL DEFAULT 'PENDING',
    "payload" JSONB NOT NULL DEFAULT '{}',
    "retry_count" INTEGER NOT NULL DEFAULT 0,
    "max_retries" INTEGER NOT NULL DEFAULT 3,
    "last_error" TEXT,
    "processed_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "vector_indexing_outbox_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX IF NOT EXISTS "vector_indexing_outbox_tenant_id_status_idx" ON "vector_indexing_outbox"("tenant_id", "status");
CREATE INDEX IF NOT EXISTS "vector_indexing_outbox_status_created_at_idx" ON "vector_indexing_outbox"("status", "created_at");
CREATE INDEX IF NOT EXISTS "vector_indexing_outbox_document_id_idx" ON "vector_indexing_outbox"("document_id");

-- AddForeignKey
DO $$ BEGIN
    ALTER TABLE "vector_indexing_outbox" 
    ADD CONSTRAINT "vector_indexing_outbox_tenant_id_fkey" 
    FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE "vector_indexing_outbox" 
    ADD CONSTRAINT "vector_indexing_outbox_document_id_fkey" 
    FOREIGN KEY ("document_id") REFERENCES "documents"("id") ON DELETE CASCADE ON UPDATE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;
