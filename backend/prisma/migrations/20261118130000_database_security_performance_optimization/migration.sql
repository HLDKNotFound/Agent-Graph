-- Migration: Database Security, Performance Optimization & Row-Level Security (RLS)
-- Target Database: PostgreSQL 16+ with pgvector & GIN Indexing

-- ============================================================================
-- 1. DATA INTEGRITY CHECK CONSTRAINTS
-- ============================================================================

-- Documents: Non-negative file size
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'documents_file_size_bytes_check' AND table_name = 'documents'
    ) THEN
        ALTER TABLE "documents"
            ADD CONSTRAINT "documents_file_size_bytes_check"
            CHECK ("file_size_bytes" >= 0);
    END IF;
END $$;

-- Document Versions: Non-negative file size
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'document_versions_file_size_bytes_check' AND table_name = 'document_versions'
    ) THEN
        ALTER TABLE "document_versions"
            ADD CONSTRAINT "document_versions_file_size_bytes_check"
            CHECK ("file_size_bytes" >= 0);
    END IF;
END $$;

-- Users: Non-empty email format check
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'users_email_format_check' AND table_name = 'users'
    ) THEN
        ALTER TABLE "users"
            ADD CONSTRAINT "users_email_format_check"
            CHECK (length(trim("email")) >= 3 AND "email" LIKE '%@%');
    END IF;
END $$;

-- Tenants: Valid slug check (alphanumeric and hyphens only, lowercase)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'tenants_slug_format_check' AND table_name = 'tenants'
    ) THEN
        ALTER TABLE "tenants"
            ADD CONSTRAINT "tenants_slug_format_check"
            CHECK ("slug" ~ '^[a-z0-9-]+$');
    END IF;
END $$;

-- Departments: Prevent self-referential hierarchy loop (parent_id != id)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'departments_parent_not_self_check' AND table_name = 'departments'
    ) THEN
        ALTER TABLE "departments"
            ADD CONSTRAINT "departments_parent_not_self_check"
            CHECK ("parent_id" IS NULL OR "parent_id" != "id");
    END IF;
END $$;

-- Sessions: Expiration strictly after creation
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'sessions_expiration_check' AND table_name = 'sessions'
    ) THEN
        ALTER TABLE "sessions"
            ADD CONSTRAINT "sessions_expiration_check"
            CHECK ("expires_at" > "created_at");
    END IF;
END $$;

-- ============================================================================
-- 2. ZERO-TRUST MULTI-TENANT ISOLATION TRIGGERS
-- ============================================================================

-- Trigger 1: Departments cross-tenant prevention (parent_id, manager_id)
CREATE OR REPLACE FUNCTION check_department_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_parent_tenant_id UUID;
    v_manager_tenant_id UUID;
BEGIN
    -- Verify parent department belongs to the same tenant
    IF NEW."parent_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_parent_tenant_id FROM "departments" WHERE "id" = NEW."parent_id";
        IF v_parent_tenant_id IS NOT NULL AND v_parent_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Department Violation: Parent department tenant % does not match department tenant %',
                v_parent_tenant_id, NEW."tenant_id";
        END IF;
    END IF;

    -- Verify manager user belongs to the same tenant
    IF NEW."manager_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_manager_tenant_id FROM "users" WHERE "id" = NEW."manager_id";
        IF v_manager_tenant_id IS NOT NULL AND v_manager_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Department Violation: Manager user tenant % does not match department tenant %',
                v_manager_tenant_id, NEW."tenant_id";
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_department_tenant_isolation ON "departments";
CREATE TRIGGER enforce_department_tenant_isolation
BEFORE INSERT OR UPDATE ON "departments"
FOR EACH ROW EXECUTE FUNCTION check_department_tenant_isolation();

-- Trigger 2: Users cross-tenant prevention (department_id)
CREATE OR REPLACE FUNCTION check_user_department_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_dept_tenant_id UUID;
BEGIN
    -- Verify assigned department belongs to the same tenant
    IF NEW."department_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_dept_tenant_id FROM "departments" WHERE "id" = NEW."department_id";
        IF v_dept_tenant_id IS NOT NULL AND v_dept_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant User Violation: Department tenant % does not match user tenant %',
                v_dept_tenant_id, NEW."tenant_id";
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_user_department_tenant_isolation ON "users";
CREATE TRIGGER enforce_user_department_tenant_isolation
BEFORE INSERT OR UPDATE ON "users"
FOR EACH ROW EXECUTE FUNCTION check_user_department_tenant_isolation();

-- Trigger 3: UserRoles assigned_by cross-tenant prevention
CREATE OR REPLACE FUNCTION check_user_role_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_user_tenant_id UUID;
    v_role_tenant_id UUID;
    v_role_scope "RoleScope";
    v_assigned_by_tenant_id UUID;
BEGIN
    SELECT "tenant_id" INTO v_user_tenant_id FROM "users" WHERE "id" = NEW."user_id";
    SELECT "tenant_id", "scope" INTO v_role_tenant_id, v_role_scope FROM "roles" WHERE "id" = NEW."role_id";

    IF v_user_tenant_id IS NULL THEN
        RAISE EXCEPTION 'RBAC Violation: Target user % does not belong to any valid tenant', NEW."user_id";
    END IF;

    -- If role is TENANT-scoped, it cannot be assigned to users in another tenant
    IF v_role_scope = 'TENANT' AND (v_role_tenant_id IS NULL OR v_user_tenant_id != v_role_tenant_id) THEN
        RAISE EXCEPTION 'Cross-Tenant RBAC Violation: User % (tenant %) cannot be assigned role % (tenant %)',
            NEW."user_id", v_user_tenant_id, NEW."role_id", v_role_tenant_id;
    END IF;

    -- Verify assigner belongs to the same tenant if specified
    IF NEW."assigned_by_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_assigned_by_tenant_id FROM "users" WHERE "id" = NEW."assigned_by_id";
        IF v_assigned_by_tenant_id IS NOT NULL AND v_assigned_by_tenant_id != v_user_tenant_id THEN
            RAISE EXCEPTION 'Cross-Tenant RBAC Violation: Assigning user tenant % does not match target user tenant %',
                v_assigned_by_tenant_id, v_user_tenant_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS enforce_user_role_tenant_isolation ON "user_roles";
CREATE TRIGGER enforce_user_role_tenant_isolation
BEFORE INSERT OR UPDATE ON "user_roles"
FOR EACH ROW EXECUTE FUNCTION check_user_role_tenant_isolation();

-- ============================================================================
-- 3. UNIQUE CONSTRAINTS FOR SYSTEM ROLES & UNVERSIONED CHUNKS
-- ============================================================================

-- Uniqueness for SYSTEM-scoped roles where tenant_id is NULL
CREATE UNIQUE INDEX IF NOT EXISTS "system_roles_name_key"
ON "roles" ("name")
WHERE "tenant_id" IS NULL;

-- Uniqueness for standalone document chunks where document_version_id is NULL
CREATE UNIQUE INDEX IF NOT EXISTS "document_chunks_standalone_chunk_index_unique"
ON "document_chunks" ("document_id", "chunk_index")
WHERE "document_version_id" IS NULL;

-- ============================================================================
-- 4. PERFORMANCE OPTIMIZATION & INDEX CONSOLIDATION
-- ============================================================================

-- Deduplicate redundant GIN index: drop document_chunks_content_fts_idx in favor of canonical idx_chunks_fts
DROP INDEX IF EXISTS "document_chunks_content_fts_idx";

-- Partial index for active documents retrieval (filtering out soft-deleted records)
CREATE INDEX IF NOT EXISTS "idx_documents_tenant_active"
ON "documents" ("tenant_id", "created_at" DESC)
WHERE "deleted_at" IS NULL;

-- Partial index for active document versions
CREATE INDEX IF NOT EXISTS "idx_document_versions_active_lookup"
ON "document_versions" ("tenant_id", "document_id", "created_at" DESC)
WHERE "deleted_at" IS NULL;

-- ============================================================================
-- 5. POSTGRESQL ROW-LEVEL SECURITY (RLS) POLICIES
-- ============================================================================

-- Enable RLS on all tenant-owned and sensitive tables
ALTER TABLE "tenants" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "departments" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "users" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "roles" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "permissions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "role_permissions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "user_roles" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "sessions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "audit_logs" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "documents" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "document_versions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "document_chunks" ENABLE ROW LEVEL SECURITY;

-- 5.1. Tenants RLS Policies
DROP POLICY IF EXISTS "tenants_isolation_policy" ON "tenants";
CREATE POLICY "tenants_isolation_policy" ON "tenants"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- 5.2. Departments RLS Policies
DROP POLICY IF EXISTS "departments_isolation_policy" ON "departments";
CREATE POLICY "departments_isolation_policy" ON "departments"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- 5.3. Users RLS Policies
DROP POLICY IF EXISTS "users_isolation_policy" ON "users";
CREATE POLICY "users_isolation_policy" ON "users"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- 5.4. Roles RLS Policies (Allow viewing SYSTEM roles or tenant's own roles)
DROP POLICY IF EXISTS "roles_isolation_select_policy" ON "roles";
CREATE POLICY "roles_isolation_select_policy" ON "roles"
FOR SELECT
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR scope = 'SYSTEM'
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

DROP POLICY IF EXISTS "roles_isolation_modify_policy" ON "roles";
CREATE POLICY "roles_isolation_modify_policy" ON "roles"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR (scope = 'TENANT' AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR (scope = 'TENANT' AND tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
);

-- 5.5. Permissions RLS Policies (System permissions readable by all, modified only by admin)
DROP POLICY IF EXISTS "permissions_select_policy" ON "permissions";
CREATE POLICY "permissions_select_policy" ON "permissions"
FOR SELECT
USING (true);

DROP POLICY IF EXISTS "permissions_admin_policy" ON "permissions";
CREATE POLICY "permissions_admin_policy" ON "permissions"
FOR ALL
USING (current_setting('app.bypass_rls', true) IN ('on', 'true', '1'))
WITH CHECK (current_setting('app.bypass_rls', true) IN ('on', 'true', '1'));

-- 5.6. Role Permissions RLS Policies
DROP POLICY IF EXISTS "role_permissions_select_policy" ON "role_permissions";
CREATE POLICY "role_permissions_select_policy" ON "role_permissions"
FOR SELECT
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR EXISTS (
        SELECT 1 FROM "roles" r
        WHERE r."id" = "role_permissions"."role_id"
          AND (r."scope" = 'SYSTEM' OR r."tenant_id" = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
    )
);

DROP POLICY IF EXISTS "role_permissions_modify_policy" ON "role_permissions";
CREATE POLICY "role_permissions_modify_policy" ON "role_permissions"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR EXISTS (
        SELECT 1 FROM "roles" r
        WHERE r."id" = "role_permissions"."role_id"
          AND r."scope" = 'TENANT'
          AND r."tenant_id" = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
    )
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR EXISTS (
        SELECT 1 FROM "roles" r
        WHERE r."id" = "role_permissions"."role_id"
          AND r."scope" = 'TENANT'
          AND r."tenant_id" = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
    )
);

-- 5.7. User Roles RLS Policies
DROP POLICY IF EXISTS "user_roles_isolation_policy" ON "user_roles";
CREATE POLICY "user_roles_isolation_policy" ON "user_roles"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR EXISTS (
        SELECT 1 FROM "users" u
        WHERE u."id" = "user_roles"."user_id"
          AND u."tenant_id" = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
    )
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR EXISTS (
        SELECT 1 FROM "users" u
        WHERE u."id" = "user_roles"."user_id"
          AND u."tenant_id" = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
    )
);

-- 5.8. Sessions RLS Policies
DROP POLICY IF EXISTS "sessions_isolation_policy" ON "sessions";
CREATE POLICY "sessions_isolation_policy" ON "sessions"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- 5.9. Audit Logs RLS Policies
DROP POLICY IF EXISTS "audit_logs_isolation_policy" ON "audit_logs";
CREATE POLICY "audit_logs_isolation_policy" ON "audit_logs"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- 5.10. Documents RLS Policies
DROP POLICY IF EXISTS "documents_isolation_policy" ON "documents";
CREATE POLICY "documents_isolation_policy" ON "documents"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- 5.11. Document Versions RLS Policies
DROP POLICY IF EXISTS "document_versions_isolation_policy" ON "document_versions";
CREATE POLICY "document_versions_isolation_policy" ON "document_versions"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- 5.12. Document Chunks RLS Policies
DROP POLICY IF EXISTS "document_chunks_isolation_policy" ON "document_chunks";
CREATE POLICY "document_chunks_isolation_policy" ON "document_chunks"
FOR ALL
USING (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
)
WITH CHECK (
    current_setting('app.bypass_rls', true) IN ('on', 'true', '1')
    OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
);

-- ============================================================================
-- 6. APPLICATION DATABASE ROLE FOR RLS ENFORCEMENT
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentgraph_app') THEN
        CREATE ROLE agentgraph_app WITH LOGIN PASSWORD 'app_secure_password';
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO agentgraph_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO agentgraph_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO agentgraph_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO agentgraph_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO agentgraph_app;
