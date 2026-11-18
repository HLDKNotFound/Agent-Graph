# Enterprise Knowledge Agent – Multi-Tenant Isolation & Zero-Trust Security Architecture

**Phase**: 1 – Database Infrastructure & Foundations  
**Task**: 10 – Database Security, Performance Optimization & Final Validation  
**Security Level**: Zero-Trust Multi-Tenant Isolation  
**Database**: PostgreSQL 16+ with Row-Level Security (RLS)  

---

## 1. Executive Summary & Architectural Overview

The **Enterprise Knowledge Agent** enforces a **Three-Tier Zero-Trust Isolation Architecture** to guarantee that tenant data is never exposed across organizational boundaries under any circumstances (including developer error, ORM query bugs, SQL injection, or stale connection pooling).

Multi-tenancy is not treated as a mere `WHERE tenant_id = ?` clause in application code; rather, it is anchored in the database engine itself through:
1. **Tier 1: Explicit Relational Tenancy & Cascading Foreign Keys** on all tenant-owned models.
2. **Tier 2: Relational Foreign Key Integrity Triggers** blocking cross-tenant references on insert/update.
3. **Tier 3: PostgreSQL-Native Row-Level Security (RLS)** filtering queries and enforcing insertion constraints directly in the PostgreSQL execution planner.

---

## 2. Multi-Tenant Entity Matrix

Every tenant-owned model in the platform explicitly persists its `tenant_id` foreign key:

| Entity | Model Name | `tenant_id` Nullable? | Foreign Key Reference | Cross-Tenant Validation Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| **Tenant** | `tenants` | N/A (Root) | Primary Key `id` | Root partition identifier |
| **Department** | `departments` | No | `tenants(id)` | Trigger: validates `parent_id` & `manager_id` tenant match |
| **User** | `users` | No | `tenants(id)` | Trigger: validates `department_id` tenant match |
| **Role** | `roles` | Yes (`SYSTEM` scope only) | `tenants(id)` | Trigger: prevents assigning tenant role across tenants |
| **UserRole** | `user_roles` | Derived via user | N/A | Trigger: validates `user`, `role`, and `assigned_by` tenant match |
| **RolePermission** | `role_permissions`| Derived via role | N/A | Policy: validates role tenant scope |
| **Session** | `sessions` | No | `tenants(id)` | Trigger: validates `user.tenant_id == session.tenant_id` |
| **AuditLog** | `audit_logs` | No | `tenants(id)` | Trigger: validates `user.tenant_id == audit_log.tenant_id` |
| **Document** | `documents` | No | `tenants(id)` | Trigger: validates `department_id`, `uploaded_by_id`, `current_version_id` |
| **DocumentVersion**| `document_versions`| No | `tenants(id)` | Trigger: validates `document_id` & `created_by_id` tenant match |
| **DocumentChunk** | `document_chunks` | No | `tenants(id)` | Trigger: validates `document_id` & `document_version_id` tenant match |

---

## 3. Database Triggers for Cross-Tenant Prevention

Prisma schema alone cannot represent multi-column cross-table foreign key equality (e.g., ensuring `departments.parent_id` belongs to the same tenant as `departments.tenant_id`). The platform employs dedicated PostgreSQL PL/pgSQL validation triggers executed `BEFORE INSERT OR UPDATE`:

### 3.1. Department Cross-Tenant Protection (`enforce_department_tenant_isolation`)
```sql
CREATE OR REPLACE FUNCTION check_department_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_parent_tenant_id UUID;
    v_manager_tenant_id UUID;
BEGIN
    IF NEW."parent_id" IS NOT NULL THEN
        SELECT "tenant_id" INTO v_parent_tenant_id FROM "departments" WHERE "id" = NEW."parent_id";
        IF v_parent_tenant_id IS NOT NULL AND v_parent_tenant_id != NEW."tenant_id" THEN
            RAISE EXCEPTION 'Cross-Tenant Department Violation: Parent department tenant % does not match department tenant %',
                v_parent_tenant_id, NEW."tenant_id";
        END IF;
    END IF;

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
```

### 3.2. User Department Protection (`enforce_user_department_tenant_isolation`)
```sql
CREATE OR REPLACE FUNCTION check_user_department_tenant_isolation()
RETURNS TRIGGER AS $$
DECLARE
    v_dept_tenant_id UUID;
BEGIN
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
```

### 3.3. UserRole Assigner Protection (`enforce_user_role_tenant_isolation`)
Validates that:
1. Target user belongs to a valid tenant.
2. If role is `TENANT`-scoped, its `tenant_id` strictly matches the user's `tenant_id`.
3. If `assigned_by_id` is specified, the assigning administrator belongs to the identical tenant.

### 3.4. Document Lineage Protection (`enforce_document_chunk_tenant_isolation`)
Validates that:
1. Target `document_id` shares the identical `tenant_id`.
2. Target `document_version_id` shares the identical `tenant_id` and belongs to the specified `document_id`.

---

## 4. PostgreSQL Row-Level Security (RLS) Deep Dive

### 4.1. The Zero-Trust Default
All 12 tables in the database have Row-Level Security enabled (`ALTER TABLE ... ENABLE ROW LEVEL SECURITY`). When connecting as the application database role (`agentgraph_app`), PostgreSQL rejects all SELECT, INSERT, UPDATE, and DELETE queries unless an explicit policy permits the operation.

If an application query fails to set a tenant context (`app.current_tenant_id` is null or empty) and does not specify administrative bypass, **zero rows are returned** and **inserts are rejected with an RLS check violation**.

### 4.2. RLS Policy Taxonomy (15 Policies)

1. **`tenants_isolation_policy`**: Allows tenant inspection/modification if `id = current_setting('app.current_tenant_id')::uuid` or admin bypass.
2. **`departments_isolation_policy`**: Filtered by `tenant_id`.
3. **`users_isolation_policy`**: Filtered by `tenant_id`.
4. **`roles_isolation_select_policy`**: Grants visibility into `SYSTEM` roles OR roles matching the current tenant.
5. **`roles_isolation_modify_policy`**: Modifying/creating roles restricted to `TENANT` scope with matching `tenant_id`.
6. **`permissions_select_policy`**: Canonical system permissions readable by all (`USING (true)`).
7. **`permissions_admin_policy`**: Only system admins can alter system permissions.
8. **`role_permissions_select_policy`**: Permitted for system roles or roles owned by tenant.
9. **`role_permissions_modify_policy`**: Restricted to tenant-owned custom roles.
10. **`user_roles_isolation_policy`**: Evaluates `EXISTS (SELECT 1 FROM users WHERE users.id = user_roles.user_id AND users.tenant_id = current_tenant)`.
11. **`sessions_isolation_policy`**: Filtered by `tenant_id`.
12. **`audit_logs_isolation_policy`**: Filtered by `tenant_id`.
13. **`documents_isolation_policy`**: Filtered by `tenant_id`.
14. **`document_versions_isolation_policy`**: Filtered by `tenant_id`.
15. **`document_chunks_isolation_policy`**: Filtered by `tenant_id`.

---

## 5. Prisma Transaction Pattern & Connection Pooling Safety

### 5.1. The Connection Pooling Threat
In high-throughput Node.js applications, Prisma Client uses an internal connection pool (and frequently connects through PgBouncer). If an application sets a session-level configuration:
```sql
-- DANGEROUS IN CONNECTION POOLING:
SET app.current_tenant_id = '11111111-1111-1111-1111-111111111111';
```
The parameter persists on that TCP socket after the HTTP request completes. When Request #2 from a different tenant acquires that connection, it would inherit Tenant #1's permissions, leading to a catastrophic cross-tenant data leak!

### 5.2. The Solution: Transaction-Scoped `SET LOCAL`
The Enterprise Knowledge Agent mandates `SET LOCAL` within Prisma interactive transactions:
```sql
-- TRANSACTION-SCOPED SAFE IMPLEMENTATION:
SET LOCAL app.current_tenant_id = '11111111-1111-1111-1111-111111111111';
```
In PostgreSQL, `SET LOCAL` only takes effect for the current transaction (`BEGIN ... COMMIT / ROLLBACK`). As soon as the transaction ends, PostgreSQL **automatically reverts** the parameter to its previous state. The connection returned to the pool is completely clean.

### 5.3. Verified Prisma Transaction Pattern

```typescript
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient({
  datasources: {
    db: { url: process.env.APP_DATABASE_URL }, // Connects as 'agentgraph_app'
  },
});

/**
 * Executes a database operation scoped strictly to the authenticated tenant.
 */
export async function withTenantContext<T>(
  tenantId: string,
  operation: (tx: Prisma.TransactionClient) => Promise<T>
): Promise<T> {
  return await prisma.$transaction(async (tx) => {
    // 1. Establish transaction-scoped tenant identity in PostgreSQL
    await tx.$executeRaw`SET LOCAL app.current_tenant_id = ${tenantId};`;

    // 2. Execute business logic; PostgreSQL RLS automatically filters all queries
    return await operation(tx);
  });
}

/**
 * Executes an administrative or background maintenance task bypassing tenant RLS.
 */
export async function withAdminContext<T>(
  operation: (tx: Prisma.TransactionClient) => Promise<T>
): Promise<T> {
  return await prisma.$transaction(async (tx) => {
    await tx.$executeRaw`SET LOCAL app.bypass_rls = 'on';`;
    return await operation(tx);
  });
}
```

---

## 6. Verification & Automated Test Coverage

The test suite in [`backend/test_database_security_performance.js`](file:///home/mercury/AgentGraph/backend/test_database_security_performance.js) executes automated tests against:
1. **Uncontextualized App Query**: Confirms 0 rows returned for `agentgraph_app`.
2. **Contextualized Tenant A Query**: Confirms Tenant A records visible, Tenant B records completely hidden.
3. **Cross-Tenant Insert Prevention**: Confirms RLS WITH CHECK violation when inserting Tenant B data inside Tenant A context.
4. **Pool Leak Prevention**: Confirms parameter reverts on commit and subsequent query returns 0 rows.
5. **Administrative Bypass**: Confirms `app.bypass_rls = 'on'` grants access across all tenant records for auditing and backup jobs.
