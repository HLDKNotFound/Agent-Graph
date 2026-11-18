// ============================================================================
// Task 10: Database Security, Performance Optimization & Final Validation
// Test Suite: Multi-Tenant Isolation, RLS, Check Constraints & Performance
// ============================================================================

const { PrismaClient } = require("@prisma/client");
const assert = require("assert");

const prisma = new PrismaClient();

// Secondary client connecting as the application role 'agentgraph_app'
// to verify true PostgreSQL Row-Level Security (RLS) enforcement
const appRoleDbUrl = "postgresql://agentgraph_app:app_secure_password@localhost:5432/agentgraph_db?schema=public";
const appPrisma = new PrismaClient({
  datasources: {
    db: { url: appRoleDbUrl },
  },
});

async function runTask10Verification() {
  console.log("====================================================================");
  console.log("🚀 Running Task 10: Database Security & Final Validation Verification");
  console.log("====================================================================\n");

  const runId = Date.now();
  let tenantA, tenantB, deptA, deptB, userA, userB;

  try {
    // ------------------------------------------------------------------------
    // Step 1: Multi-Tenant Test Fixture Setup
    // ------------------------------------------------------------------------
    console.log("Step 1: Setting up multi-tenant test fixtures...");
    tenantA = await prisma.tenant.create({
      data: {
        name: `Security Alpha Corp ${runId}`,
        slug: `sec-alpha-${runId}`,
        status: "ACTIVE",
      },
    });

    tenantB = await prisma.tenant.create({
      data: {
        name: `Security Beta Corp ${runId}`,
        slug: `sec-beta-${runId}`,
        status: "ACTIVE",
      },
    });

    deptA = await prisma.department.create({
      data: {
        tenantId: tenantA.id,
        name: "Engineering",
      },
    });

    deptB = await prisma.department.create({
      data: {
        tenantId: tenantB.id,
        name: "Marketing",
      },
    });

    userA = await prisma.user.create({
      data: {
        tenantId: tenantA.id,
        email: `alice-${runId}@sec-alpha.com`,
        fullName: "Alice Alpha",
        passwordHash: "$2b$12$eX4mpleH4shForSecur1tyT3st1ngOnlyAlphaAlice",
        departmentId: deptA.id,
      },
    });

    userB = await prisma.user.create({
      data: {
        tenantId: tenantB.id,
        email: `bob-${runId}@sec-beta.com`,
        fullName: "Bob Beta",
        passwordHash: "$2b$12$eX4mpleH4shForSecur1tyT3st1ngOnlyBetaBob",
        departmentId: deptB.id,
      },
    });

    console.log("  PASSED: Multi-tenant fixtures established (Tenant A, Tenant B).\n");

    // ------------------------------------------------------------------------
    // Step 2: Testing Cross-Tenant Foreign Key Prevention Triggers
    // ------------------------------------------------------------------------
    console.log("Step 2: Testing Cross-Tenant Foreign Key Prevention Triggers...");

    // 2.1. Prevent user in Tenant A from referencing Department in Tenant B
    let userDeptBlocked = false;
    try {
      await prisma.user.create({
        data: {
          tenantId: tenantA.id,
          email: `charlie-${runId}@sec-alpha.com`,
          fullName: "Charlie Cross",
          passwordHash: "$2b$12$eX4mpleH4shForSecur1tyT3st1ngCharlie",
          departmentId: deptB.id, // Department belongs to Tenant B!
        },
      });
    } catch (err) {
      userDeptBlocked = err.message.includes("Cross-Tenant User Violation");
    }
    assert.strictEqual(userDeptBlocked, true, "User department cross-tenant violation must be blocked by trigger");
    console.log("  PASSED: User assignment to cross-tenant department strictly blocked.");

    // 2.2. Prevent department parent_id belonging to another tenant
    let parentDeptBlocked = false;
    try {
      await prisma.department.create({
        data: {
          tenantId: tenantA.id,
          name: "Sub-Marketing Cross",
          parentId: deptB.id, // Parent belongs to Tenant B!
        },
      });
    } catch (err) {
      parentDeptBlocked = err.message.includes("Cross-Tenant Department Violation");
    }
    assert.strictEqual(parentDeptBlocked, true, "Department parent cross-tenant violation must be blocked");
    console.log("  PASSED: Department hierarchy cross-tenant parent strictly blocked.");

    // 2.3. Prevent department manager belonging to another tenant
    let managerDeptBlocked = false;
    try {
      await prisma.department.create({
        data: {
          tenantId: tenantA.id,
          name: "Operations",
          managerId: userB.id, // User belongs to Tenant B!
        },
      });
    } catch (err) {
      managerDeptBlocked = err.message.includes("Cross-Tenant Department Violation");
    }
    assert.strictEqual(managerDeptBlocked, true, "Department manager cross-tenant violation must be blocked");
    console.log("  PASSED: Department manager cross-tenant assignment strictly blocked.");

    // 2.4. Prevent self-parenting on department (parent_id == id)
    let selfParentBlocked = false;
    try {
      await prisma.department.update({
        where: { id: deptA.id },
        data: { parentId: deptA.id },
      });
    } catch (err) {
      selfParentBlocked = err.message.includes("departments_parent_not_self_check") ||
                          err.message.includes("check constraint");
    }
    assert.strictEqual(selfParentBlocked, true, "Self-parenting department hierarchy cycle must be blocked");
    console.log("  PASSED: Department self-parenting cycle prevented by CHECK constraint.");

    // 2.5. Prevent role assignment with assigned_by_id from another tenant
    const roleA = await prisma.role.create({
      data: {
        tenantId: tenantA.id,
        name: `TENANT_ADMIN_${runId}`,
        scope: "TENANT",
      },
    });

    let assignedByBlocked = false;
    try {
      await prisma.userRole.create({
        data: {
          userId: userA.id,
          roleId: roleA.id,
          assignedById: userB.id, // Assigner belongs to Tenant B!
        },
      });
    } catch (err) {
      assignedByBlocked = err.message.includes("Cross-Tenant RBAC Violation");
    }
    assert.strictEqual(assignedByBlocked, true, "UserRole assigned_by cross-tenant violation must be blocked");
    console.log("  PASSED: UserRole assigner cross-tenant violation strictly blocked.\n");

    // ------------------------------------------------------------------------
    // Step 3: Testing Data Integrity CHECK Constraints
    // ------------------------------------------------------------------------
    console.log("Step 3: Testing Data Integrity CHECK Constraints...");

    // 3.1. Document negative file size
    let docNegSizeBlocked = false;
    try {
      await prisma.$executeRawUnsafe(`
        INSERT INTO documents (
          id, tenant_id, original_filename, normalized_filename, storage_uri, mime_type, file_size_bytes, content_hash
        ) VALUES (
          gen_random_uuid(), '${tenantA.id}', 'test.pdf', 'test.pdf', 's3://bucket/test.pdf', 'application/pdf', -500, 'hash123'
        );
      `);
    } catch (err) {
      docNegSizeBlocked = err.message.includes("documents_file_size_bytes_check") ||
                          err.message.includes("check constraint");
    }
    assert.strictEqual(docNegSizeBlocked, true, "Negative file size on documents must be rejected");
    console.log("  PASSED: Document negative file size rejected by CHECK constraint.");

    // 3.2. DocumentVersion negative file size
    let verNegSizeBlocked = false;
    const validDoc = await prisma.document.create({
      data: {
        tenantId: tenantA.id,
        originalFilename: "contract.pdf",
        normalizedFilename: "contract.pdf",
        storageUri: "s3://contracts/contract.pdf",
        mimeType: "application/pdf",
        fileSizeBytes: 2048n,
        contentHash: `hash-valid-${runId}`,
      },
    });

    try {
      await prisma.$executeRawUnsafe(`
        INSERT INTO document_versions (
          id, document_id, tenant_id, version_number, storage_uri, original_filename, mime_type, file_size_bytes, content_hash
        ) VALUES (
          gen_random_uuid(), '${validDoc.id}', '${tenantA.id}', 1, 's3://v1.pdf', 'contract.pdf', 'application/pdf', -1024, 'v1hash'
        );
      `);
    } catch (err) {
      verNegSizeBlocked = err.message.includes("document_versions_file_size_bytes_check") ||
                          err.message.includes("check constraint");
    }
    assert.strictEqual(verNegSizeBlocked, true, "Negative file size on document_versions must be rejected");
    console.log("  PASSED: DocumentVersion negative file size rejected by CHECK constraint.");

    // 3.3. User invalid email format
    let userEmailBlocked = false;
    try {
      await prisma.user.create({
        data: {
          tenantId: tenantA.id,
          email: "invalid_email_without_at_symbol",
          fullName: "Invalid Email User",
          passwordHash: "hash123",
        },
      });
    } catch (err) {
      userEmailBlocked = err.message.includes("users_email_format_check") ||
                         err.message.includes("check constraint");
    }
    assert.strictEqual(userEmailBlocked, true, "User email format must contain '@'");
    console.log("  PASSED: User invalid email format rejected by CHECK constraint.");

    // 3.4. Tenant invalid slug format (uppercase / special characters)
    let tenantSlugBlocked = false;
    try {
      await prisma.tenant.create({
        data: {
          name: "Invalid Slug Corp",
          slug: "INVALID_SLUG_UPPERCASE!",
        },
      });
    } catch (err) {
      tenantSlugBlocked = err.message.includes("tenants_slug_format_check") ||
                          err.message.includes("check constraint");
    }
    assert.strictEqual(tenantSlugBlocked, true, "Tenant slug must be lowercase alphanumeric with hyphens");
    console.log("  PASSED: Tenant invalid slug rejected by CHECK constraint.");

    // 3.5. Session expiration in the past relative to creation
    let sessionExpBlocked = false;
    try {
      await prisma.$executeRawUnsafe(`
        INSERT INTO sessions (
          id, tenant_id, user_id, token_hash, expires_at, created_at
        ) VALUES (
          gen_random_uuid(), '${tenantA.id}', '${userA.id}', 'tokenhash-${runId}', '2020-01-01 00:00:00+00', '2026-01-01 00:00:00+00'
        );
      `);
    } catch (err) {
      sessionExpBlocked = err.message.includes("sessions_expiration_check") ||
                          err.message.includes("check constraint");
    }
    assert.strictEqual(sessionExpBlocked, true, "Session expires_at must be strictly after created_at");
    console.log("  PASSED: Session expiration before creation rejected by CHECK constraint.\n");

    // ------------------------------------------------------------------------
    // Step 4: Testing Composite & Partial Unique Constraints
    // ------------------------------------------------------------------------
    console.log("Step 4: Testing Composite & Partial Unique Constraints...");

    // 4.1. SYSTEM Role unique name when tenant_id IS NULL
    const sysRoleName = `GLOBAL_SYS_ADMIN_${runId}`;
    await prisma.role.create({
      data: {
        tenantId: null,
        name: sysRoleName,
        scope: "SYSTEM",
      },
    });

    let dupSysRoleBlocked = false;
    try {
      await prisma.role.create({
        data: {
          tenantId: null,
          name: sysRoleName,
          scope: "SYSTEM",
        },
      });
    } catch (err) {
      dupSysRoleBlocked = err.message.includes("system_roles_name_key") ||
                          err.message.includes("Unique constraint failed");
    }
    assert.strictEqual(dupSysRoleBlocked, true, "Duplicate SYSTEM role name with NULL tenant must be blocked");
    console.log("  PASSED: Duplicate SYSTEM role name blocked by system_roles_name_key partial index.");

    // 4.2. Standalone unversioned DocumentChunk chunkIndex uniqueness
    await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: validDoc.id,
        documentVersionId: null, // Standalone unversioned chunk
        chunkIndex: 0,
        content: "Standalone Chunk Zero",
        contentHash: `chunk-hash-0-${runId}`,
        tokenCount: 4,
      },
    });

    let dupChunkIndexBlocked = false;
    try {
      await prisma.documentChunk.create({
        data: {
          tenantId: tenantA.id,
          documentId: validDoc.id,
          documentVersionId: null,
          chunkIndex: 0, // Duplicate chunkIndex 0!
          content: "Duplicate Chunk Zero",
          contentHash: `chunk-hash-0-dup-${runId}`,
          tokenCount: 4,
        },
      });
    } catch (err) {
      dupChunkIndexBlocked = err.message.includes("document_chunks_standalone_chunk_index_unique") ||
                             err.message.includes("Unique constraint failed");
    }
    assert.strictEqual(dupChunkIndexBlocked, true, "Duplicate chunk_index for unversioned chunks must be blocked");
    console.log("  PASSED: Duplicate standalone chunk index blocked by unique partial index.\n");

    // ------------------------------------------------------------------------
    // Step 5: Testing PostgreSQL Row-Level Security (RLS) & Connection Pooling Isolation
    // ------------------------------------------------------------------------
    console.log("Step 5: Testing PostgreSQL Row-Level Security (RLS) & Connection Pooling Isolation...");

    // Create a document in Tenant B for RLS comparison
    const docB = await prisma.document.create({
      data: {
        tenantId: tenantB.id,
        originalFilename: "confidential_b.pdf",
        normalizedFilename: "confidential_b.pdf",
        storageUri: "s3://beta/confidential_b.pdf",
        mimeType: "application/pdf",
        fileSizeBytes: 4096n,
        contentHash: `hash-beta-${runId}`,
      },
    });

    // 5.1. Uncontextualized App Query (Zero-Trust Default)
    // Connecting via agentgraph_app role with no tenant context and no bypass
    const uncontextualizedDocs = await appPrisma.$queryRawUnsafe(`
      SELECT id, tenant_id FROM documents WHERE id IN ('${validDoc.id}', '${docB.id}');
    `);
    assert.strictEqual(uncontextualizedDocs.length, 0, "Uncontextualized app role query must see 0 tenant records");
    console.log("  PASSED: Uncontextualized query returns 0 rows (Zero-Trust default enforced).");

    // 5.2. Transaction-Scoped Context: Tenant A
    await appPrisma.$transaction(async (tx) => {
      await tx.$executeRawUnsafe(`SET LOCAL app.current_tenant_id = '${tenantA.id}';`);

      const docsInContextA = await tx.$queryRawUnsafe(`
        SELECT id, tenant_id FROM documents WHERE id IN ('${validDoc.id}', '${docB.id}');
      `);
      assert.strictEqual(docsInContextA.length, 1, "Context A must only see Tenant A's document");
      assert.strictEqual(docsInContextA[0].id, validDoc.id);

      // Attempt to cross-insert into Tenant B while in Tenant A's context
      let crossInsertBlocked = false;
      try {
        await tx.$executeRawUnsafe(`
          INSERT INTO documents (
            id, tenant_id, original_filename, normalized_filename, storage_uri, mime_type, file_size_bytes, content_hash
          ) VALUES (
            gen_random_uuid(), '${tenantB.id}', 'illegal.pdf', 'illegal.pdf', 's3://illegal.pdf', 'application/pdf', 100, 'hash-illegal'
          );
        `);
      } catch (err) {
        crossInsertBlocked = err.message.includes("row-level security policy") ||
                             err.message.includes("RLS");
      }
      assert.strictEqual(crossInsertBlocked, true, "Inserting cross-tenant record must be rejected by RLS WITH CHECK");
    });
    console.log("  PASSED: Transaction-scoped Tenant A context isolated (Tenant B invisible, cross-insert rejected).");

    // 5.3. Connection Pool Leak Prevention Verification
    // After transaction commits, verify app.current_tenant_id was automatically reset
    const postTxDocs = await appPrisma.$queryRawUnsafe(`
      SELECT id, tenant_id FROM documents WHERE id IN ('${validDoc.id}', '${docB.id}');
    `);
    assert.strictEqual(postTxDocs.length, 0, "Tenant context must NOT leak across connection pool reuse");
    console.log("  PASSED: Connection pooling tenant context leak prevented (SET LOCAL auto-reverted on commit).");

    // 5.4. Administrative Bypass Verification
    await appPrisma.$transaction(async (tx) => {
      await tx.$executeRawUnsafe(`SET LOCAL app.bypass_rls = 'on';`);

      const adminDocs = await tx.$queryRawUnsafe(`
        SELECT id, tenant_id FROM documents WHERE id IN ('${validDoc.id}', '${docB.id}') ORDER BY original_filename;
      `);
      assert.strictEqual(adminDocs.length, 2, "Admin bypass must be able to inspect documents across all tenants");
    });
    console.log("  PASSED: Administrative access path verified (app.bypass_rls = 'on' grants multi-tenant access).\n");

    // ------------------------------------------------------------------------
    // Step 6: Testing Performance Optimization & Partial Index Utilization
    // ------------------------------------------------------------------------
    console.log("Step 6: Testing Performance Optimization & Partial Index Utilization...");

    // 6.1. Verify idx_documents_tenant_active is utilized in EXPLAIN for active document queries
    await prisma.$executeRawUnsafe("SET enable_seqscan = off;");
    const explainPlan = await prisma.$queryRawUnsafe(`
      EXPLAIN (FORMAT JSON)
      SELECT id, original_filename FROM documents
      WHERE tenant_id = '${tenantA.id}' AND deleted_at IS NULL
      ORDER BY created_at DESC;
    `);
    await prisma.$executeRawUnsafe("SET enable_seqscan = on;");
    const planText = JSON.stringify(explainPlan);
    const usesIndex = planText.includes("idx_documents_tenant_active");
    assert.strictEqual(usesIndex, true, "Active documents query must utilize idx_documents_tenant_active partial index");
    console.log("  PASSED: Active documents query verified against optimized index (idx_documents_tenant_active).");

    // 6.2. Verify duplicate GIN index is removed and canonical idx_chunks_fts is preserved
    const ftsIndexes = await prisma.$queryRawUnsafe(`
      SELECT indexname FROM pg_indexes
      WHERE tablename = 'document_chunks' AND indexname LIKE '%fts%';
    `);
    const ftsNames = ftsIndexes.map(i => i.indexname);
    assert.strictEqual(ftsNames.includes("idx_chunks_fts"), true, "Canonical idx_chunks_fts must exist");
    assert.strictEqual(ftsNames.includes("document_chunks_content_fts_idx"), false, "Redundant index must be dropped");
    console.log("  PASSED: Duplicate GIN index dropped; canonical idx_chunks_fts maintained for BM25 retrieval.\n");

    // ------------------------------------------------------------------------
    // Step 7: Cleanup
    // ------------------------------------------------------------------------
    console.log("Step 7: Cleaning up test fixtures...");
    await prisma.tenant.deleteMany({
      where: { id: { in: [tenantA.id, tenantB.id] } },
    });
    console.log("  PASSED: Cascading deprovisioning completed.\n");

    console.log("====================================================================");
    console.log("🎉 ALL TASK 10 ACCEPTANCE CRITERIA VERIFIED SUCCESSFULLY!");
    console.log("====================================================================");

  } catch (error) {
    console.error("❌ TASK 10 VERIFICATION FAILED:", error);
    throw error;
  } finally {
    await prisma.$disconnect();
    await appPrisma.$disconnect();
  }
}

runTask10Verification().catch((err) => {
  console.error("Fatal error during Task 10 verification:", err);
  process.exit(1);
});
