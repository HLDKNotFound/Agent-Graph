-- Migration: Authentication & Session Management
-- Target Database: PostgreSQL 16+ with pgvector extension enabled

-- ============================================================================
-- 1. CREATE SESSIONS TABLE
-- ============================================================================

-- CreateTable: sessions
CREATE TABLE IF NOT EXISTS "sessions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "tenant_id" UUID NOT NULL,
    "user_id" UUID NOT NULL,
    "token_hash" VARCHAR(255) NOT NULL,
    "expires_at" TIMESTAMPTZ(6) NOT NULL,
    "ip_address" INET,
    "user_agent" TEXT,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_activity_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "revoked_at" TIMESTAMPTZ(6),

    CONSTRAINT "sessions_pkey" PRIMARY KEY ("id")
);

-- ============================================================================
-- 2. CREATE UNIQUE & PERFORMANCE INDEXES
-- ============================================================================

-- Unique Index on token_hash (O(1) token verification)
CREATE UNIQUE INDEX IF NOT EXISTS "sessions_token_hash_key" ON "sessions"("token_hash");

-- Foreign key lookup indexes
CREATE INDEX IF NOT EXISTS "sessions_user_id_idx" ON "sessions"("user_id");
CREATE INDEX IF NOT EXISTS "sessions_tenant_id_idx" ON "sessions"("tenant_id");

-- Lifecycle & status indexes
CREATE INDEX IF NOT EXISTS "sessions_expires_at_idx" ON "sessions"("expires_at");
CREATE INDEX IF NOT EXISTS "sessions_revoked_at_idx" ON "sessions"("revoked_at");

-- Partial Indexes for high-performance active session queries
-- Only indexes active (unrevoked) sessions, drastically reducing index size and lookup latency
CREATE INDEX IF NOT EXISTS "sessions_active_user_idx"
ON "sessions" ("user_id", "expires_at")
WHERE "revoked_at" IS NULL;

CREATE INDEX IF NOT EXISTS "sessions_active_tenant_idx"
ON "sessions" ("tenant_id", "expires_at")
WHERE "revoked_at" IS NULL;

-- Cleanup index for identifying expired or revoked sessions for purge routines
CREATE INDEX IF NOT EXISTS "sessions_cleanup_idx"
ON "sessions" ("expires_at", "revoked_at");

-- ============================================================================
-- 3. FOREIGN KEY CONSTRAINTS WITH CASCADE DELETION
-- ============================================================================

-- Foreign key linking session to tenant (Cascade delete on tenant removal)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'sessions_tenant_id_fkey' AND table_name = 'sessions'
    ) THEN
        ALTER TABLE "sessions"
            ADD CONSTRAINT "sessions_tenant_id_fkey"
            FOREIGN KEY ("tenant_id")
            REFERENCES "tenants"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- Foreign key linking session to user (Cascade delete on user removal)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'sessions_user_id_fkey' AND table_name = 'sessions'
    ) THEN
        ALTER TABLE "sessions"
            ADD CONSTRAINT "sessions_user_id_fkey"
            FOREIGN KEY ("user_id")
            REFERENCES "users"("id")
            ON DELETE CASCADE
            ON UPDATE CASCADE;
    END IF;
END $$;

-- ============================================================================
-- 4. ZERO-TRUST CROSS-TENANT ISOLATION TRIGGER
-- ============================================================================

-- Database-level trigger to strictly prevent cross-tenant session references
-- Guarantees that session.tenant_id ALWAYS matches the user's tenant_id
CREATE OR REPLACE FUNCTION check_session_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_user_tenant_id UUID;
BEGIN
    SELECT "tenant_id" INTO v_user_tenant_id FROM "users" WHERE "id" = NEW."user_id";

    IF v_user_tenant_id IS NULL THEN
        RAISE EXCEPTION 'Session Isolation Violation: Target user % does not exist', NEW."user_id";
    END IF;

    IF v_user_tenant_id != NEW."tenant_id" THEN
        RAISE EXCEPTION 'Cross-Tenant Session Violation: Session tenant % does not match user tenant % for user %',
            NEW."tenant_id", v_user_tenant_id, NEW."user_id";
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_session_tenant_isolation ON "sessions";
CREATE TRIGGER enforce_session_tenant_isolation
BEFORE INSERT OR UPDATE ON "sessions"
FOR EACH ROW EXECUTE FUNCTION check_session_tenant_isolation();
