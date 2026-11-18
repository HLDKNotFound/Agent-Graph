-- Migration: Enterprise Audit Logging System
-- Target Database: PostgreSQL 16+ with pgvector extension enabled

-- ============================================================================
-- 1. CREATE AUDIT LOGS TABLE
-- ============================================================================

-- CreateTable: audit_logs
CREATE TABLE IF NOT EXISTS "audit_logs" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenant_id" UUID NOT NULL,
    "user_id" UUID,
    "actor_type" VARCHAR(50) NOT NULL DEFAULT 'USER',
    "action" VARCHAR(100) NOT NULL,
    "target_type" VARCHAR(100) NOT NULL,
    "target_id" VARCHAR(255),
    "metadata" JSONB DEFAULT '{}',
    "ip_address" INET,
    "request_id" VARCHAR(255),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "audit_logs_pkey" PRIMARY KEY ("id")
);

-- ============================================================================
-- 2. CREATE PERFORMANCE & COMPOSITE QUERY INDEXES
-- ============================================================================

-- Primary Tenant-Scoped Timeline Index (Cursor-based pagination & audit history)
CREATE INDEX IF NOT EXISTS "audit_logs_tenant_created_idx"
ON "audit_logs" ("tenant_id", "created_at" DESC);

-- Actor / User within Tenant Filtering Index
CREATE INDEX IF NOT EXISTS "audit_logs_tenant_user_created_idx"
ON "audit_logs" ("tenant_id", "user_id", "created_at" DESC);

-- Action Type Filtering Index
CREATE INDEX IF NOT EXISTS "audit_logs_tenant_action_created_idx"
ON "audit_logs" ("tenant_id", "action", "created_at" DESC);

-- Target Entity Lifecycle Tracking Index (Target Type + Identifier)
CREATE INDEX IF NOT EXISTS "audit_logs_tenant_target_created_idx"
ON "audit_logs" ("tenant_id", "target_type", "target_id", "created_at" DESC);

-- Direct Actor Index (Global user activity queries across tenants by super-admin)
CREATE INDEX IF NOT EXISTS "audit_logs_user_created_idx"
ON "audit_logs" ("user_id", "created_at" DESC);

-- Low-cardinality action index
CREATE INDEX IF NOT EXISTS "audit_logs_action_idx"
ON "audit_logs" ("action");

-- Direct Target Entity Lookup
CREATE INDEX IF NOT EXISTS "audit_logs_target_type_target_id_idx"
ON "audit_logs" ("target_type", "target_id");

-- Distributed Request Tracing Lookup Index
CREATE INDEX IF NOT EXISTS "audit_logs_request_id_idx"
ON "audit_logs" ("request_id");

-- Time-range & Retention Cleanup Index
CREATE INDEX IF NOT EXISTS "audit_logs_created_at_idx"
ON "audit_logs" ("created_at" DESC);

-- ============================================================================
-- 3. FOREIGN KEY CONSTRAINTS (PRESERVE AUDIT TRAIL ON USER DELETION)
-- ============================================================================

-- Foreign key linking audit log to tenant (Cascade delete on tenant removal)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'audit_logs_tenant_id_fkey' AND table_name = 'audit_logs'
    ) THEN
        ALTER TABLE "audit_logs"
            ADD CONSTRAINT "audit_logs_tenant_id_fkey"
            FOREIGN KEY ("tenant_id")
            REFERENCES "tenants"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- Foreign key linking audit log to user (ON DELETE SET NULL: Historical records NEVER deleted with user)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'audit_logs_user_id_fkey' AND table_name = 'audit_logs'
    ) THEN
        ALTER TABLE "audit_logs"
            ADD CONSTRAINT "audit_logs_user_id_fkey"
            FOREIGN KEY ("user_id")
            REFERENCES "users"("id")
            ON DELETE SET NULL
            ON UPDATE CASCADE;
    END IF;
END $$;

-- ============================================================================
-- 4. ZERO-TRUST CROSS-TENANT ISOLATION TRIGGER
-- ============================================================================

-- Database-level trigger to strictly prevent cross-tenant audit log forgery
-- If a user is specified, guarantees audit_log.tenant_id ALWAYS matches the user's tenant_id
CREATE OR REPLACE FUNCTION check_audit_log_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_user_tenant_id UUID;
BEGIN
    IF NEW."user_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_user_tenant_id FROM "users" WHERE "id" = NEW."user_id";

        IF v_user_tenant_id IS NOT NULL AND v_user_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Audit Violation: Audit log tenant % does not match user tenant % for actor %',
                NEW."tenant_id", v_user_tenant_id, NEW."user_id";
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_audit_log_tenant_isolation ON "audit_logs";
CREATE TRIGGER enforce_audit_log_tenant_isolation
BEFORE INSERT OR UPDATE ON "audit_logs"
FOR EACH ROW EXECUTE FUNCTION check_audit_log_tenant_isolation();

-- ============================================================================
-- 5. APPEND-ONLY IMMUTABILITY TRIGGER (APPLICATION-LEVEL & DB INTEGRITY)
-- ============================================================================

-- Prevents modification of audit records once inserted
-- Allows foreign key ON DELETE SET NULL when parent user is removed
CREATE OR REPLACE FUNCTION enforce_audit_log_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Allow foreign key cascade SET NULL on user_id when user is deleted
        IF OLD."user_id" IS NOT NULL AND NEW."user_id" IS NULL 
           AND OLD."id" = NEW."id" 
           AND OLD."tenant_id" = NEW."tenant_id" 
           AND OLD."action" = NEW."action" 
           AND OLD."created_at" = NEW."created_at" THEN
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'Audit Immutability Violation: Audit log entries are append-only and cannot be modified (id: %)', OLD."id";
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS protect_audit_log_immutability ON "audit_logs";
CREATE TRIGGER protect_audit_log_immutability
BEFORE UPDATE ON "audit_logs"
FOR EACH ROW EXECUTE FUNCTION enforce_audit_log_immutability();
