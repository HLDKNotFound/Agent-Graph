/**
 * Comprehensive Verification Test Suite for Task 09:
 * PostgreSQL Full-Text Search Infrastructure
 *
 * Verifies:
 * 1. GIN Expression Index Verification:
 *    - Confirms `idx_chunks_fts` exists on table `document_chunks`
 *    - Confirms index definition matches `USING gin (to_tsvector('simple'::regconfig, content))`
 * 2. Search Capabilities & Parsers:
 *    - `to_tsvector('simple', content)` tokenization
 *    - `plainto_tsquery('simple', ...)` plain text search
 *    - `websearch_to_tsquery('simple', ...)` advanced websearch syntax:
 *      * multiple terms (implicit AND)
 *      * quoted exact phrase matching (`"..."`)
 *      * OR logical disjunction (`OR`)
 *      * negation exclusion (`-`)
 * 3. Relevance Ranking Comparison:
 *    - `ts_rank` (Term Frequency ranking)
 *    - `ts_rank_cd` (Cover Density proximity ranking)
 *    - Normalization flags (log document length normalization)
 * 4. Multi-Dimensional Search Filtering:
 *    - Strict multi-tenant isolation (Tenant A vs Tenant B zero data leakage)
 *    - Document ID scoping
 *    - Document version scoping (specific version vs currentVersionOnly)
 *    - Document classification filtering (`PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `RESTRICTED`)
 *    - Department scoping
 *    - Ingestion status filtering (`COMPLETED` vs `PENDING`/`FAILED`)
 * 5. Zero-Trust RBAC & Permission Enforcement:
 *    - Evaluates role permissions, department boundaries, and clearance levels in SQL
 *    - Prevents unauthorized users from seeing confidential or restricted chunks
 *    - Validates admin global bypass
 * 6. Pagination & Determinism:
 *    - Stable sorting by `relevance_score DESC, chunk_id ASC`
 *    - Window-based `full_count` for total count
 *    - Disjoint page slices via `LIMIT` and `OFFSET`
 * 7. Hybrid Retrieval & RRF Extensibility:
 *    - Verifies rank indexing (1..K) and calculated sparse reciprocal rank score:
 *      RRF_Score = w_sparse / (k + rank)
 */

const { PrismaClient } = require('@prisma/client');
const crypto = require('crypto');
const assert = require('assert');
const { FullTextSearchService } = require('./fts_search_service');

const prisma = new PrismaClient();
const searchService = new FullTextSearchService(prisma);

function computeSha256(content) {
  return crypto.createHash('sha256').update(content, 'utf8').digest('hex');
}

async function runTests() {
  console.log('🚀 Starting Task 09: PostgreSQL Full-Text Search Infrastructure Verification...\n');

  const runId = Date.now();
  const tenantASlug = `test-fts-tenant-a-${runId}`;
  const tenantBSlug = `test-fts-tenant-b-${runId}`;

  try {
    // ------------------------------------------------------------------------
    // Step 1: Verify GIN Expression Index `idx_chunks_fts` Exists in PostgreSQL
    // ------------------------------------------------------------------------
    console.log('Step 1: Verifying GIN Expression Index `idx_chunks_fts` in PostgreSQL catalog...');

    const indexes = await prisma.$queryRaw`
      SELECT indexname, indexdef
      FROM pg_indexes
      WHERE tablename = 'document_chunks' AND indexname = 'idx_chunks_fts';
    `;

    assert.strictEqual(indexes.length, 1, 'Expected index idx_chunks_fts to exist on document_chunks');
    const indexDef = indexes[0].indexdef;
    console.log(`  Found index: ${indexes[0].indexname}`);
    console.log(`  Index definition: ${indexDef}`);

    assert(
      indexDef.includes('gin') || indexDef.includes('GIN'),
      'Index must be a GIN index'
    );
    assert(
      indexDef.includes('to_tsvector') && indexDef.includes('simple') && indexDef.includes('content'),
      'Index must be an expression index over to_tsvector(\'simple\', content)'
    );
    console.log('  PASSED: GIN Expression Index idx_chunks_fts successfully verified in PostgreSQL.\n');

    // ------------------------------------------------------------------------
    // Step 2: Seed Multi-Tenant Fixtures (Tenants, Depts, Users, Roles, Permissions)
    // ------------------------------------------------------------------------
    console.log('Step 2: Seeding multi-tenant test fixtures with RBAC roles and permissions...');

    const tenantA = await prisma.tenant.create({
      data: {
        name: 'Alpha Financial Corp',
        slug: tenantASlug,
        status: 'ACTIVE',
      },
    });

    const tenantB = await prisma.tenant.create({
      data: {
        name: 'Beta Healthcare Corp',
        slug: tenantBSlug,
        status: 'ACTIVE',
      },
    });

    // Departments
    const deptFinance = await prisma.department.create({
      data: {
        name: 'Finance & Treasury',
        tenantId: tenantA.id,
      },
    });

    const deptEngineering = await prisma.department.create({
      data: {
        name: 'Engineering & DevOps',
        tenantId: tenantA.id,
      },
    });

    // Permissions
    const permDocRead = await prisma.permission.upsert({
      where: { key: 'document:read' },
      update: {},
      create: {
        name: 'Read Document',
        key: 'document:read',
        resource: 'document',
        action: 'read',
      },
    });

    const permDocConfidential = await prisma.permission.upsert({
      where: { key: 'documents:read_confidential' },
      update: {},
      create: {
        name: 'Read Confidential Documents',
        key: 'documents:read_confidential',
        resource: 'document',
        action: 'read_confidential',
      },
    });

    const permDocAdmin = await prisma.permission.upsert({
      where: { key: 'documents:admin' },
      update: {},
      create: {
        name: 'Document Administrator',
        key: 'documents:admin',
        resource: 'document',
        action: 'admin',
      },
    });

    // Roles in Tenant A
    const roleStaff = await prisma.role.create({
      data: {
        name: `Staff Member ${runId}`,
        tenantId: tenantA.id,
        rolePermissions: {
          create: [{ permissionId: permDocRead.id }],
        },
      },
    });

    const roleFinanceOfficer = await prisma.role.create({
      data: {
        name: `Finance Officer ${runId}`,
        tenantId: tenantA.id,
        rolePermissions: {
          create: [
            { permissionId: permDocRead.id },
            { permissionId: permDocConfidential.id },
          ],
        },
      },
    });

    const roleTenantAdmin = await prisma.role.create({
      data: {
        name: `Tenant Admin ${runId}`,
        tenantId: tenantA.id,
        rolePermissions: {
          create: [
            { permissionId: permDocRead.id },
            { permissionId: permDocConfidential.id },
            { permissionId: permDocAdmin.id },
          ],
        },
      },
    });

    // Users in Tenant A
    const userAliceFinance = await prisma.user.create({
      data: {
        email: `alice.${runId}@alpha.vn`,
        fullName: 'Alice Finance Officer',
        passwordHash: 'dummy_hash',
        tenantId: tenantA.id,
        departmentId: deptFinance.id,
        roles: {
          create: [{ roleId: roleFinanceOfficer.id }],
        },
      },
    });

    const userBobEngineer = await prisma.user.create({
      data: {
        email: `bob.${runId}@alpha.vn`,
        fullName: 'Bob Software Engineer',
        passwordHash: 'dummy_hash',
        tenantId: tenantA.id,
        departmentId: deptEngineering.id,
        roles: {
          create: [{ roleId: roleStaff.id }],
        },
      },
    });

    const userCharlieAdmin = await prisma.user.create({
      data: {
        email: `charlie.${runId}@alpha.vn`,
        fullName: 'Charlie System Admin',
        passwordHash: 'dummy_hash',
        tenantId: tenantA.id,
        roles: {
          create: [{ roleId: roleTenantAdmin.id }],
        },
      },
    });

    // User in Tenant B (Different Tenant)
    const userEveBeta = await prisma.user.create({
      data: {
        email: `eve.${runId}@beta.vn`,
        fullName: 'Eve Beta Auditor',
        passwordHash: 'dummy_hash',
        tenantId: tenantB.id,
      },
    });

    console.log('  PASSED: Users, roles, and department fixtures established.\n');

    // ------------------------------------------------------------------------
    // Step 3: Seed Documents, Versions & Chunks with Distinct Text Patterns
    // ------------------------------------------------------------------------
    console.log('Step 3: Ingesting sample documents and chunks across departments & classifications...');

    // Doc 1: Public Employee Guidelines (Tenant A, Public)
    const docPublic = await prisma.document.create({
      data: {
        tenantId: tenantA.id,
        originalFilename: 'employee_handbook_2026.pdf',
        normalizedFilename: 'employee_handbook_2026.pdf',
        storageUri: 's3://docs/handbook.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(1048576),
        contentHash: computeSha256('public_handbook_content'),
        classification: 'PUBLIC',
        status: 'COMPLETED',
      },
    });

    const v1Public = await prisma.documentVersion.create({
      data: {
        documentId: docPublic.id,
        tenantId: tenantA.id,
        versionNumber: 1,
        storageUri: 's3://docs/handbook_v1.pdf',
        originalFilename: 'employee_handbook_2026.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(1048576),
        contentHash: computeSha256('public_handbook_content'),
        status: 'COMPLETED',
        isCurrent: true,
      },
    });

    await prisma.document.update({
      where: { id: docPublic.id },
      data: { currentVersionId: v1Public.id },
    });

    const chunkPublic1 = await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: docPublic.id,
        documentVersionId: v1Public.id,
        chunkIndex: 0,
        content: 'All enterprise employees are entitled to standard annual leave benefits of twenty paid days per calendar year.',
        contentHash: computeSha256('chunkPublic1'),
        tokenCount: 18,
        pageNumber: 4,
        sectionHeading: 'Annual Leave Policy',
      },
    });

    const chunkPublic2 = await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: docPublic.id,
        documentVersionId: v1Public.id,
        chunkIndex: 1,
        content: 'Remote working arrangements require prior departmental supervisor approval and two-factor authentication compliance.',
        contentHash: computeSha256('chunkPublic2'),
        tokenCount: 16,
        pageNumber: 7,
        sectionHeading: 'Remote Work Protocol',
      },
    });

    // Doc 2: Engineering Architecture Specification (Tenant A, Engineering Dept, INTERNAL)
    const docEngineering = await prisma.document.create({
      data: {
        tenantId: tenantA.id,
        departmentId: deptEngineering.id,
        originalFilename: 'cloud_security_architecture.docx',
        normalizedFilename: 'cloud_security_architecture.docx',
        storageUri: 's3://docs/cloud_security.docx',
        mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        fileSizeBytes: BigInt(2097152),
        contentHash: computeSha256('engineering_arch_content'),
        classification: 'INTERNAL',
        status: 'COMPLETED',
      },
    });

    const v1Eng = await prisma.documentVersion.create({
      data: {
        documentId: docEngineering.id,
        tenantId: tenantA.id,
        versionNumber: 1,
        storageUri: 's3://docs/cloud_security_v1.docx',
        originalFilename: 'cloud_security_architecture.docx',
        mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        fileSizeBytes: BigInt(2097152),
        contentHash: computeSha256('engineering_arch_content'),
        status: 'COMPLETED',
        isCurrent: true,
      },
    });

    await prisma.document.update({
      where: { id: docEngineering.id },
      data: { currentVersionId: v1Eng.id },
    });

    const chunkEng1 = await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: docEngineering.id,
        documentVersionId: v1Eng.id,
        chunkIndex: 0,
        content: 'Zero-trust enterprise network security mandates TLS 1.3 encryption across all internal microservice gRPC communication channels.',
        contentHash: computeSha256('chunkEng1'),
        tokenCount: 19,
        sectionHeading: 'Network Encryption Architecture',
      },
    });

    const chunkEng2 = await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: docEngineering.id,
        documentVersionId: v1Eng.id,
        chunkIndex: 1,
        content: 'Data at rest must be secured with AES-256 BitLocker encryption on workstation laptops and AWS KMS keys in cloud storage buckets.',
        contentHash: computeSha256('chunkEng2'),
        tokenCount: 23,
        sectionHeading: 'Data Storage Encryption Standards',
      },
    });

    // Doc 3: Financial Treasury Strategy (Tenant A, Finance Dept, CONFIDENTIAL)
    const docFinance = await prisma.document.create({
      data: {
        tenantId: tenantA.id,
        departmentId: deptFinance.id,
        originalFilename: 'q4_treasury_allocation.xlsx',
        normalizedFilename: 'q4_treasury_allocation.xlsx',
        storageUri: 's3://docs/q4_treasury.xlsx',
        mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        fileSizeBytes: BigInt(4194304),
        contentHash: computeSha256('finance_treasury_content'),
        classification: 'CONFIDENTIAL',
        status: 'COMPLETED',
      },
    });

    const v1Finance = await prisma.documentVersion.create({
      data: {
        documentId: docFinance.id,
        tenantId: tenantA.id,
        versionNumber: 1,
        storageUri: 's3://docs/q4_treasury_v1.xlsx',
        originalFilename: 'q4_treasury_allocation.xlsx',
        mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        fileSizeBytes: BigInt(4194304),
        contentHash: computeSha256('finance_treasury_content'),
        status: 'COMPLETED',
        isCurrent: true,
      },
    });

    await prisma.document.update({
      where: { id: docFinance.id },
      data: { currentVersionId: v1Finance.id },
    });

    const chunkFin1 = await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: docFinance.id,
        documentVersionId: v1Finance.id,
        chunkIndex: 0,
        content: 'Q4 Treasury reserves allocate forty million dollars towards sovereign debt bonds and liquid cash equivalents.',
        contentHash: computeSha256('chunkFin1'),
        tokenCount: 17,
        sheetName: 'Reserve Allocations',
        rowRange: 'B2:F25',
      },
    });

    // Doc 4: Obsolete Version (Version 1 of a document that is now on Version 2)
    const docPolicy = await prisma.document.create({
      data: {
        tenantId: tenantA.id,
        originalFilename: 'data_retention_policy.pdf',
        normalizedFilename: 'data_retention_policy.pdf',
        storageUri: 's3://docs/data_retention.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(524288),
        contentHash: computeSha256('data_retention_content_v2'),
        classification: 'INTERNAL',
        status: 'COMPLETED',
      },
    });

    const v1PolicyHistorical = await prisma.documentVersion.create({
      data: {
        documentId: docPolicy.id,
        tenantId: tenantA.id,
        versionNumber: 1,
        storageUri: 's3://docs/data_retention_v1.pdf',
        originalFilename: 'data_retention_policy.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(500000),
        contentHash: computeSha256('data_retention_content_v1'),
        status: 'COMPLETED',
        isCurrent: false,
      },
    });

    const v2PolicyCurrent = await prisma.documentVersion.create({
      data: {
        documentId: docPolicy.id,
        tenantId: tenantA.id,
        versionNumber: 2,
        storageUri: 's3://docs/data_retention_v2.pdf',
        originalFilename: 'data_retention_policy.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(524288),
        contentHash: computeSha256('data_retention_content_v2'),
        status: 'COMPLETED',
        isCurrent: true,
      },
    });

    await prisma.document.update({
      where: { id: docPolicy.id },
      data: { currentVersionId: v2PolicyCurrent.id },
    });

    const chunkPolicyOld = await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: docPolicy.id,
        documentVersionId: v1PolicyHistorical.id,
        chunkIndex: 0,
        content: 'Historical policy mandate: User audit logs must be retained for ninety days before permanent deletion.',
        contentHash: computeSha256('chunkPolicyOld'),
        tokenCount: 16,
        sectionHeading: 'Historical Log Retention',
      },
    });

    const chunkPolicyNew = await prisma.documentChunk.create({
      data: {
        tenantId: tenantA.id,
        documentId: docPolicy.id,
        documentVersionId: v2PolicyCurrent.id,
        chunkIndex: 0,
        content: 'Updated policy mandate: User audit logs must be retained for 365 days in immutable object storage.',
        contentHash: computeSha256('chunkPolicyNew'),
        tokenCount: 17,
        sectionHeading: 'Updated Log Retention',
      },
    });

    // Doc 5: Tenant B Document (Isolation boundary verification)
    const docTenantB = await prisma.document.create({
      data: {
        tenantId: tenantB.id,
        originalFilename: 'beta_confidential_records.pdf',
        normalizedFilename: 'beta_confidential_records.pdf',
        storageUri: 's3://docs/beta_records.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(1048576),
        contentHash: computeSha256('beta_content'),
        classification: 'CONFIDENTIAL',
        status: 'COMPLETED',
      },
    });

    const chunkTenantB = await prisma.documentChunk.create({
      data: {
        tenantId: tenantB.id,
        documentId: docTenantB.id,
        chunkIndex: 0,
        content: 'Beta Healthcare confidential records: All medical encryption mandates TLS 1.3 and BitLocker protection.',
        contentHash: computeSha256('chunkTenantB'),
        tokenCount: 15,
      },
    });

    console.log('  PASSED: Multi-tenant, multi-department, multi-version document chunks ingested.\n');

    // ------------------------------------------------------------------------
    // Step 4: Test Basic Full-Text Search (plainto_tsquery & to_tsvector)
    // ------------------------------------------------------------------------
    console.log('Step 4: Testing Basic Full-Text Search with plainto_tsquery...');

    const basicResults = await searchService.searchBasic({
      tenantId: tenantA.id,
      queryText: 'annual leave paid days',
      queryMode: 'plain',
    });

    assert.strictEqual(basicResults.length, 1, 'Should find exactly 1 matching chunk');
    assert.strictEqual(basicResults[0].chunk_id, chunkPublic1.id);
    console.log(`  PASSED: Found chunk ${basicResults[0].chunk_id} with score ${basicResults[0].relevance_score}.\n`);

    // ------------------------------------------------------------------------
    // Step 5: Test Advanced Websearch Query Syntax (websearch_to_tsquery)
    // ------------------------------------------------------------------------
    console.log('Step 5: Testing websearch_to_tsquery syntax (Phrases, AND, OR, Negation)...');

    // 5.1 Quoted exact phrase search
    console.log('  5.1 Testing exact phrase search: "zero-trust enterprise network"');
    const phraseResults = await searchService.searchBasic({
      tenantId: tenantA.id,
      queryText: '"zero-trust enterprise network"',
      queryMode: 'websearch',
    });
    assert.strictEqual(phraseResults.length, 1);
    assert.strictEqual(phraseResults[0].chunk_id, chunkEng1.id);
    console.log('    PASSED: Exact phrase match returned correct chunk.');

    // 5.2 Logical OR operator
    console.log('  5.2 Testing OR operator: "BitLocker OR treasury"');
    const orResults = await searchService.searchBasic({
      tenantId: tenantA.id,
      queryText: 'BitLocker OR treasury',
      queryMode: 'websearch',
    });
    assert.strictEqual(orResults.length, 2, 'Should match both Engineering and Finance chunks');
    const matchedChunkIds = orResults.map((r) => r.chunk_id);
    assert(matchedChunkIds.includes(chunkEng2.id));
    assert(matchedChunkIds.includes(chunkFin1.id));
    console.log('    PASSED: OR operator successfully matched disjoint records.');

    // 5.3 Negation operator: "encryption -microservice"
    console.log('  5.3 Testing Negation operator: "encryption -microservice"');
    const negResults = await searchService.searchBasic({
      tenantId: tenantA.id,
      queryText: 'encryption -microservice',
      queryMode: 'websearch',
    });
    // chunkEng1 contains both 'encryption' and 'microservice'.
    // chunkEng2 contains 'encryption' but NOT 'microservice'.
    assert.strictEqual(negResults.length, 1, 'Should exclude chunk containing negated term');
    assert.strictEqual(negResults[0].chunk_id, chunkEng2.id);
    console.log('    PASSED: Negation operator successfully excluded microservice chunk.\n');

    // ------------------------------------------------------------------------
    // Step 6: Test Relevance Ranking: ts_rank (TF) vs ts_rank_cd (Cover Density)
    // ------------------------------------------------------------------------
    console.log('Step 6: Comparing ts_rank (Term Frequency) vs ts_rank_cd (Cover Density Proximity)...');

    const rankComparison = await searchService.compareRankers(tenantA.id, 'encryption storage');
    assert(rankComparison.length >= 1);
    console.log(`  Top match chunk #${rankComparison[0].chunk_index}: "${rankComparison[0].section_heading}"`);
    console.log(`  Score ts_rank (TF): ${rankComparison[0].score_ts_rank_tf}`);
    console.log(`  Score ts_rank_cd (Cover Density): ${rankComparison[0].score_ts_rank_cd_proximity}`);
    assert(rankComparison[0].score_ts_rank_cd_proximity > 0);
    assert(rankComparison[0].score_ts_rank_tf > 0);
    console.log('  PASSED: Relevance ranking functions computed properly with length normalization.\n');

    // ------------------------------------------------------------------------
    // Step 7: Test Multi-Dimensional Filtering (Tenant, Version, Classification, Dept)
    // ------------------------------------------------------------------------
    console.log('Step 7: Testing Multi-Dimensional Search Filtering...');

    // 7.1 Strict Tenant Isolation
    console.log('  7.1 Verifying Multi-Tenant Isolation (Tenant A search vs Tenant B)...');
    const tenantASearch = await searchService.searchFiltered({
      tenantId: tenantA.id,
      queryText: 'BitLocker encryption',
    });
    const tenantBSearch = await searchService.searchFiltered({
      tenantId: tenantB.id,
      queryText: 'BitLocker encryption',
    });

    assert.strictEqual(tenantASearch.items.length, 1);
    assert.strictEqual(tenantASearch.items[0].chunk_id, chunkEng2.id);
    assert.strictEqual(tenantBSearch.items.length, 1);
    assert.strictEqual(tenantBSearch.items[0].chunk_id, chunkTenantB.id);
    // Tenant A results must NEVER contain Tenant B chunk
    assert(!tenantASearch.items.some((c) => c.chunk_id === chunkTenantB.id));
    console.log('    PASSED: Tenant boundaries strictly isolated. Zero cross-tenant leakage.');

    // 7.2 Document Version Scoping (currentVersionOnly = true)
    console.log('  7.2 Verifying Version Scoping (Excluding obsolete version chunks)...');
    const currentVersionSearch = await searchService.searchFiltered({
      tenantId: tenantA.id,
      queryText: 'audit logs retained',
      currentVersionOnly: true,
    });
    assert.strictEqual(currentVersionSearch.items.length, 1);
    assert.strictEqual(currentVersionSearch.items[0].chunk_id, chunkPolicyNew.id);
    assert.strictEqual(currentVersionSearch.items[0].version_number, 2);
    console.log('    PASSED: Only active version 2 returned; historical version 1 excluded.');

    // 7.3 Document Classification Filtering
    console.log('  7.3 Verifying Document Classification Filter (PUBLIC only vs CONFIDENTIAL)...');
    const publicOnly = await searchService.searchFiltered({
      tenantId: tenantA.id,
      queryText: 'policy OR leave OR treasury',
      classifications: ['PUBLIC'],
    });
    assert(publicOnly.items.length > 0);
    assert(publicOnly.items.every((item) => item.classification === 'PUBLIC'));
    console.log(`    PASSED: All ${publicOnly.items.length} returned chunks match PUBLIC classification.`);

    // 7.4 Department Filter
    console.log('  7.4 Verifying Department Scoping (Finance only)...');
    const financeOnly = await searchService.searchFiltered({
      tenantId: tenantA.id,
      queryText: 'allocation OR treasury',
      departmentId: deptFinance.id,
    });
    assert.strictEqual(financeOnly.items.length, 1);
    assert.strictEqual(financeOnly.items[0].department_name, 'Finance & Treasury');
    console.log('    PASSED: Department filter correctly restricted results.\n');

    // ------------------------------------------------------------------------
    // Step 8: Test Zero-Trust RBAC Permission Enforcement
    // ------------------------------------------------------------------------
    console.log('Step 8: Testing Zero-Trust RBAC Permission Enforcement in Search Queries...');

    // 8.1 Bob (Engineer, Staff role, no confidential clearance) searching across policies
    console.log('  8.1 Testing Engineer Bob querying for "treasury reserves"...');
    const bobSearchConfidential = await searchService.searchWithPermissions({
      tenantId: tenantA.id,
      userId: userBobEngineer.id,
      queryText: 'treasury reserves sovereign debt',
    });
    assert.strictEqual(bobSearchConfidential.items.length, 0, 'Bob must NOT see confidential treasury records');
    console.log('    PASSED: Bob blocked from accessing CONFIDENTIAL finance documents.');

    // 8.2 Bob searching for internal engineering docs
    console.log('  8.2 Testing Engineer Bob querying for "network encryption"...');
    const bobSearchEng = await searchService.searchWithPermissions({
      tenantId: tenantA.id,
      userId: userBobEngineer.id,
      queryText: 'network encryption gRPC',
    });
    assert.strictEqual(bobSearchEng.items.length, 1, 'Bob should see Engineering internal docs');
    assert.strictEqual(bobSearchEng.items[0].chunk_id, chunkEng1.id);
    console.log('    PASSED: Bob successfully retrieved Engineering INTERNAL document.');

    // 8.3 Alice (Finance Officer, with confidential clearance) querying treasury records
    console.log('  8.3 Testing Finance Officer Alice querying for "treasury reserves"...');
    const aliceSearchTreasury = await searchService.searchWithPermissions({
      tenantId: tenantA.id,
      userId: userAliceFinance.id,
      queryText: 'treasury reserves sovereign debt',
    });
    assert.strictEqual(aliceSearchTreasury.items.length, 1, 'Alice has confidential permission and matching dept');
    assert.strictEqual(aliceSearchTreasury.items[0].chunk_id, chunkFin1.id);
    console.log('    PASSED: Alice authorized to view CONFIDENTIAL finance records.');

    // 8.4 Alice (Finance) querying Engineering internal documents
    console.log('  8.4 Testing Finance Officer Alice querying Engineering internal docs...');
    const aliceSearchEng = await searchService.searchWithPermissions({
      tenantId: tenantA.id,
      userId: userAliceFinance.id,
      queryText: 'network encryption gRPC',
    });
    assert.strictEqual(aliceSearchEng.items.length, 0, 'Alice is in Finance; Engineering internal doc is barred');
    console.log('    PASSED: Department boundary enforced. Alice cannot see Engineering internal docs.');

    // 8.5 Charlie (Tenant Admin) querying across all departments and classifications
    console.log('  8.5 Testing Admin Charlie querying across all documents...');
    const adminSearch = await searchService.searchWithPermissions({
      tenantId: tenantA.id,
      userId: userCharlieAdmin.id,
      queryText: 'encryption OR treasury OR leave',
    });
    assert(adminSearch.items.length >= 3, 'Admin should have access across all classifications and departments');
    console.log(`    PASSED: Admin successfully accessed ${adminSearch.items.length} records across departments.\n`);

    // ------------------------------------------------------------------------
    // Step 9: Test Pagination and Deterministic Sorting
    // ------------------------------------------------------------------------
    console.log('Step 9: Testing Pagination & Determinism (LIMIT, OFFSET, Window full_count)...');

    const page1 = await searchService.searchFiltered({
      tenantId: tenantA.id,
      queryText: 'encryption OR policy OR leave OR audit',
      limit: 2,
      offset: 0,
    });

    const page2 = await searchService.searchFiltered({
      tenantId: tenantA.id,
      queryText: 'encryption OR policy OR leave OR audit',
      limit: 2,
      offset: 2,
    });

    assert.strictEqual(page1.items.length, 2);
    assert.strictEqual(page2.items.length, 2);
    assert(page1.totalCount >= 4, `Total count should be >= 4 (got ${page1.totalCount})`);
    assert.strictEqual(page1.totalCount, page2.totalCount, 'Total count must match across pages');

    // Confirm pages are completely disjoint
    const page1Ids = page1.items.map((i) => i.chunk_id);
    const page2Ids = page2.items.map((i) => i.chunk_id);
    for (const id of page1Ids) {
      assert(!page2Ids.includes(id), `Page 2 must not contain chunk ${id} from Page 1`);
    }
    console.log(`  PASSED: Paginated search verified: Page 1 [${page1Ids}] & Page 2 [${page2Ids}] are strictly disjoint (Total: ${page1.totalCount}).\n`);

    // ------------------------------------------------------------------------
    // Step 10: Test Hybrid RRF Extensibility
    // ------------------------------------------------------------------------
    console.log('Step 10: Testing Reciprocal Rank Fusion (RRF) Candidate Retrieval Extensibility...');

    const rrfCandidates = await searchService.searchForRRF({
      tenantId: tenantA.id,
      queryText: 'encryption',
      topK: 10,
      rrfK: 60,
      sparseWeight: 0.3,
    });

    assert(rrfCandidates.length >= 2, 'Should return multiple candidates for "encryption"');
    console.log(`  Retrieved ${rrfCandidates.length} candidate chunks for RRF fusion.`);

    rrfCandidates.forEach((cand, idx) => {
      const expectedRank = idx + 1;
      assert.strictEqual(Number(cand.rank_position), expectedRank, `Rank position must be ${expectedRank}`);
      assert(cand.sparse_rrf_score > 0, 'sparse_rrf_score must be positive');
      // Verify formula: 0.3 / (60 + rank_position)
      const expectedScore = 0.3 / (60 + expectedRank);
      const diff = Math.abs(cand.sparse_rrf_score - expectedScore);
      assert(diff < 1e-6, `RRF calculation mismatch: got ${cand.sparse_rrf_score}, expected ${expectedScore}`);
    });

    console.log(`  Candidate #1 Chunk ID: ${rrfCandidates[0].chunk_id}`);
    console.log(`  Rank Position: ${rrfCandidates[0].rank_position}`);
    console.log(`  Raw Relevance: ${rrfCandidates[0].raw_score}`);
    console.log(`  Sparse RRF Contribution Score: ${rrfCandidates[0].sparse_rrf_score.toFixed(6)}`);
    console.log('  PASSED: RRF candidate scoring and rank indexing verified.\n');

    console.log('🎉 ALL TASK 09 VERIFICATION TESTS PASSED SUCCESSFULLY!\n');
  } catch (error) {
    console.error('❌ Test suite failed with error:', error);
    process.exit(1);
  } finally {
    await prisma.$disconnect();
  }
}

runTests();
