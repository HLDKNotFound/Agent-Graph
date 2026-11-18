-- Migration: Role-Based Access Control (RBAC) & Permission Management
-- Target Database: PostgreSQL 16+ with pgvector extension enabled

-- ============================================================================
-- 1. CLEANUP LEGACY IMPLICIT JOIN TABLE
-- ============================================================================

-- Drop implicit join table _UserRoles if it exists
DO $$
BEGIN
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_schema = 'public' AND table_name = '_UserRoles') THEN
        DROP TABLE IF EXISTS "_UserRoles" CASCADE;
    END IF;
END $$;

-- ============================================================================
-- 2. CREATE RBAC TABLES
-- ============================================================================

-- CreateTable: permissions
CREATE TABLE "permissions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "name" VARCHAR(100) NOT NULL,
    "key" VARCHAR(100) NOT NULL,
    "description" TEXT,
    "resource" VARCHAR(100) NOT NULL,
    "action" VARCHAR(100) NOT NULL,
    "scope" "RoleScope" DEFAULT 'SYSTEM',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "permissions_pkey" PRIMARY KEY ("id")
);

-- CreateTable: role_permissions (explicit many-to-many between roles and permissions)
CREATE TABLE "role_permissions" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "role_id" UUID NOT NULL,
    "permission_id" UUID NOT NULL,
    "assigned_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "role_permissions_pkey" PRIMARY KEY ("id")
);

-- CreateTable: user_roles (explicit many-to-many between users and roles with audit history)
CREATE TABLE "user_roles" (
    "id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "user_id" UUID NOT NULL,
    "role_id" UUID NOT NULL,
    "assigned_by_id" UUID,
    "assigned_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "metadata" JSONB DEFAULT '{}',

    CONSTRAINT "user_roles_pkey" PRIMARY KEY ("id")
);

-- ============================================================================
-- 3. CREATE INDEXES & UNIQUE CONSTRAINTS
-- ============================================================================

-- Permission Indexes & Uniqueness
CREATE UNIQUE INDEX "permissions_key_key" ON "permissions"("key");
CREATE INDEX "permissions_resource_idx" ON "permissions"("resource");
CREATE INDEX "permissions_action_idx" ON "permissions"("action");
CREATE INDEX "permissions_resource_action_idx" ON "permissions"("resource", "action");

-- RolePermission Composite Uniqueness & Lookup Indexes
CREATE UNIQUE INDEX "role_permissions_role_id_permission_id_key" ON "role_permissions"("role_id", "permission_id");
CREATE INDEX "role_permissions_role_id_idx" ON "role_permissions"("role_id");
CREATE INDEX "role_permissions_permission_id_idx" ON "role_permissions"("permission_id");

-- UserRole Composite Uniqueness & Lookup Indexes
CREATE UNIQUE INDEX "user_roles_user_id_role_id_key" ON "user_roles"("user_id", "role_id");
CREATE INDEX "user_roles_user_id_idx" ON "user_roles"("user_id");
CREATE INDEX "user_roles_role_id_idx" ON "user_roles"("role_id");
CREATE INDEX "user_roles_assigned_by_id_idx" ON "user_roles"("assigned_by_id");

-- ============================================================================
-- 4. ADD FOREIGN KEY CONSTRAINTS
-- ============================================================================

-- AddForeignKey: role_permissions -> roles
ALTER TABLE "role_permissions"
    ADD CONSTRAINT "role_permissions_role_id_fkey"
    FOREIGN KEY ("role_id") REFERENCES "roles"("id")
    ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey: role_permissions -> permissions
ALTER TABLE "role_permissions"
    ADD CONSTRAINT "role_permissions_permission_id_fkey"
    FOREIGN KEY ("permission_id") REFERENCES "permissions"("id")
    ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey: user_roles -> users (assigned user)
ALTER TABLE "user_roles"
    ADD CONSTRAINT "user_roles_user_id_fkey"
    FOREIGN KEY ("user_id") REFERENCES "users"("id")
    ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey: user_roles -> roles (assigned role)
ALTER TABLE "user_roles"
    ADD CONSTRAINT "user_roles_role_id_fkey"
    FOREIGN KEY ("role_id") REFERENCES "roles"("id")
    ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey: user_roles -> users (assigned_by administrator)
ALTER TABLE "user_roles"
    ADD CONSTRAINT "user_roles_assigned_by_id_fkey"
    FOREIGN KEY ("assigned_by_id") REFERENCES "users"("id")
    ON DELETE SET NULL ON UPDATE CASCADE;

-- ============================================================================
-- 5. POSTGRESQL TRIGGERS FOR ZERO-TRUST TENANT ISOLATION
-- ============================================================================

-- Enforce Tenant Isolation on User-Role Assignments
CREATE OR REPLACE FUNCTION check_user_role_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_user_tenant_id UUID;
    v_role_tenant_id UUID;
    v_role_scope "RoleScope";
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

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER enforce_user_role_tenant_isolation
BEFORE INSERT OR UPDATE ON "user_roles"
FOR EACH ROW EXECUTE FUNCTION check_user_role_tenant_isolation();

-- ============================================================================
-- 6. SEED SYSTEM-DEFINED CANONICAL PERMISSIONS
-- ============================================================================

INSERT INTO "permissions" ("id", "name", "key", "description", "resource", "action", "scope")
VALUES
    (gen_random_uuid(), 'Create Document', 'document:create', 'Create and upload new enterprise documents', 'document', 'create', 'SYSTEM'),
    (gen_random_uuid(), 'Read Document', 'document:read', 'View and retrieve enterprise documents', 'document', 'read', 'SYSTEM'),
    (gen_random_uuid(), 'Update Document', 'document:update', 'Edit metadata or classifications of documents', 'document', 'update', 'SYSTEM'),
    (gen_random_uuid(), 'Delete Document', 'document:delete', 'Soft-delete or purge enterprise documents', 'document', 'delete', 'SYSTEM'),
    (gen_random_uuid(), 'Download Document', 'document:download', 'Download raw binary documents and attachments', 'document', 'download', 'SYSTEM'),
    (gen_random_uuid(), 'Share Document', 'document:share', 'Grant cross-department or external access to documents', 'document', 'share', 'SYSTEM'),
    (gen_random_uuid(), 'Manage Users', 'user:manage', 'Invite, suspend, and configure platform user accounts', 'user', 'manage', 'SYSTEM'),
    (gen_random_uuid(), 'Manage Departments', 'department:manage', 'Create and reorganize organizational departments', 'department', 'manage', 'SYSTEM'),
    (gen_random_uuid(), 'Read Audit Logs', 'audit:read', 'Inspect tamper-evident compliance audit trail logs', 'audit', 'read', 'SYSTEM')
ON CONFLICT ("key") DO NOTHING;
