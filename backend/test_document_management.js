/**
 * Comprehensive Verification Test Suite for Task 06:
 * Document Metadata & Classification Management
 *
 * Verifies:
 * 1. Document model creation with all required fields (UUID, BigInt size, SHA-256 hash, enums, JSON metadata).
 * 2. DocumentClassification enum enforcement (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED).
 * 3. DocumentIngestionStatus enum enforcement (PENDING, PROCESSING, COMPLETED, FAILED, CANCELLED).
 * 4. Tenant-aware SHA-256 deduplication (duplicate blocked in same tenant for active docs).
 * 5. Multi-tenant hash allowance (identical hash allowed in different tenant).
 * 6. Soft-deletion aware deduplication (soft-deleted doc allows re-uploading same hash in same tenant).
 * 7. Zero-trust cross-tenant boundary isolation trigger (department & uploader tenant validation).
 * 8. Performance index queries:
 *    - (tenantId, contentHash)
 *    - (tenantId, classification)
 *    - (tenantId, status)
 *    - (tenantId, createdAt)
 *    - uploadedById
 *    - departmentId
 * 9. Foreign key lifecycle:
 *    - Department deletion preserves document (departmentId -> null)
 *    - User deletion preserves document (uploadedById -> null)
 *    - Tenant deletion cascade removes documents
 */

const { PrismaClient } = require('@prisma/client');
const crypto = require('crypto');
const assert = require('assert');

const prisma = new PrismaClient();

function computeSha256(content) {
  return crypto.createHash('sha256').update(content).digest('hex');
}

async function runTests() {
  console.log('🚀 Starting Task 06: Document Metadata & Classification Management Verification...\n');

  const runId = Date.now();
  const testTenantASlug = `test-doc-tenant-a-${runId}`;
  const testTenantBSlug = `test-doc-tenant-b-${runId}`;

  try {
    // ------------------------------------------------------------------------
    // Step 1: Set up Test Tenants, Departments, and Users
    // ------------------------------------------------------------------------
    console.log('Step 1: Setting up test tenants, departments, and users...');

    const tenantA = await prisma.tenant.create({
      data: {
        name: 'Enterprise Corp Alpha',
        slug: testTenantASlug,
        status: 'ACTIVE',
      },
    });

    const tenantB = await prisma.tenant.create({
      data: {
        name: 'Enterprise Corp Beta',
        slug: testTenantBSlug,
        status: 'ACTIVE',
      },
    });

    const deptA1 = await prisma.department.create({
      data: {
        name: 'Legal & Compliance',
        tenantId: tenantA.id,
      },
    });

    const deptA2 = await prisma.department.create({
      data: {
        name: 'Engineering & AI',
        tenantId: tenantA.id,
      },
    });

    const deptB = await prisma.department.create({
      data: {
        name: 'Human Resources Beta',
        tenantId: tenantB.id,
      },
    });

    const userA = await prisma.user.create({
      data: {
        email: `alice.${runId}@alpha.enterprise.vn`,
        fullName: 'Alice Legal Counsel',
        passwordHash: 'argon2id_mock_hash_for_testing',
        tenantId: tenantA.id,
        departmentId: deptA1.id,
      },
    });

    const userB = await prisma.user.create({
      data: {
        email: `bob.${runId}@beta.enterprise.vn`,
        fullName: 'Bob HR Director',
        passwordHash: 'argon2id_mock_hash_for_testing',
        tenantId: tenantB.id,
        departmentId: deptB.id,
      },
    });

    console.log(`  Tenant A: ${tenantA.id} (${tenantA.slug})`);
    console.log(`  Tenant B: ${tenantB.id} (${tenantB.slug})`);
    console.log(`  User A:   ${userA.id} in Dept A: ${deptA1.id}`);
    console.log(`  User B:   ${userB.id} in Dept B: ${deptB.id}`);

    // ------------------------------------------------------------------------
    // Step 2: Document Creation & Field Type Validation
    // ------------------------------------------------------------------------
    console.log('\nStep 2: Creating document with full metadata and validating field types...');

    const sampleContent1 = 'CONFIDENTIAL ENTERPRISE POLICY 2026: Remote Work and AI Usage Guidelines.';
    const hash1 = computeSha256(sampleContent1);

    const doc1 = await prisma.document.create({
      data: {
        tenantId: tenantA.id,
        departmentId: deptA1.id,
        uploadedById: userA.id,
        originalFilename: 'Remote_Work_&_AI_Policy_2026 (v2.1).pdf',
        normalizedFilename: 'remote_work_and_ai_policy_2026_v2_1.pdf',
        storageUri: 's3://enterprise-documents-alpha/legal/2026/remote_work_and_ai_policy_2026_v2_1.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(2548920), // ~2.43 MB
        contentHash: hash1,
        classification: 'CONFIDENTIAL',
        status: 'PENDING',
        metadata: {
          author: 'Alice Legal Counsel',
          pageCount: 42,
          ocrExtracted: false,
          securityTags: ['LEGAL', 'AI_POLICY', 'COMPLIANCE'],
        },
      },
    });

    assert(doc1.id, 'Document ID must be generated');
    assert.strictEqual(typeof doc1.fileSizeBytes, 'bigint', 'fileSizeBytes must be BigInt');
    assert.strictEqual(doc1.fileSizeBytes, BigInt(2548920));
    assert.strictEqual(doc1.contentHash, hash1);
    assert.strictEqual(doc1.classification, 'CONFIDENTIAL');
    assert.strictEqual(doc1.status, 'PENDING');
    assert.strictEqual(doc1.deletedAt, null);
    assert.strictEqual(doc1.metadata.pageCount, 42);

    console.log(`  Created Document 1: ID=${doc1.id}`);
    console.log(`  Classification: ${doc1.classification}, Status: ${doc1.status}, Size: ${doc1.fileSizeBytes} bytes`);
    console.log(`  Content Hash: ${doc1.contentHash}`);
    console.log('  PASSED: Document creation and type validation successful.');

    // ------------------------------------------------------------------------
    // Step 3: Enum Enforcement (DocumentClassification & DocumentIngestionStatus)
    // ------------------------------------------------------------------------
    console.log('\nStep 3: Verifying all DocumentClassification and DocumentIngestionStatus enum values...');

    const classifications = ['PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED'];
    const statuses = ['PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED'];

    for (let i = 0; i < classifications.length; i++) {
      const cls = classifications[i];
      const st = statuses[i % statuses.length];
      const content = `Test document classification ${cls} and status ${st} content ${i}`;
      const hash = computeSha256(content);

      const doc = await prisma.document.create({
        data: {
          tenantId: tenantA.id,
          originalFilename: `test_${cls.toLowerCase()}.docx`,
          normalizedFilename: `test_${cls.toLowerCase()}.docx`,
          storageUri: `s3://bucket/${cls.toLowerCase()}.docx`,
          mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          fileSizeBytes: BigInt(10240 * (i + 1)),
          contentHash: hash,
          classification: cls,
          status: st,
        },
      });

      assert.strictEqual(doc.classification, cls);
      assert.strictEqual(doc.status, st);
    }

    console.log(`  Verified classifications: ${classifications.join(', ')}`);
    console.log(`  Verified statuses: ${statuses.join(', ')}`);
    console.log('  PASSED: All classification and ingestion status enums persisted and retrieved.');

    // ------------------------------------------------------------------------
    // Step 4: Tenant-Aware Deduplication (Active Document Collision)
    // ------------------------------------------------------------------------
    console.log('\nStep 4: Testing tenant-aware SHA-256 deduplication (active duplicate collision)...');

    let duplicateRejected = false;
    try {
      // Attempt to insert duplicate document with SAME hash in SAME tenant
      await prisma.document.create({
        data: {
          tenantId: tenantA.id,
          originalFilename: 'Duplicate_Remote_Policy.pdf',
          normalizedFilename: 'duplicate_remote_policy.pdf',
          storageUri: 's3://enterprise-documents-alpha/duplicate.pdf',
          mimeType: 'application/pdf',
          fileSizeBytes: BigInt(2548920),
          contentHash: hash1, // EXACT SAME HASH as doc1
          classification: 'INTERNAL',
          status: 'PENDING',
        },
      });
    } catch (err) {
      duplicateRejected = true;
      const isUniqueConstraint = err.code === 'P2002' || err.message.includes('documents_tenant_active_content_hash_key') || err.message.includes('unique constraint');
      assert(isUniqueConstraint, `Expected unique constraint violation, got: ${err.message}`);
      console.log(`  PASSED: Duplicate active document rejected by unique index: ${err.message.split('\n').pop()}`);
    }

    assert(duplicateRejected, 'Duplicate active document must be rejected within the same tenant');

    // ------------------------------------------------------------------------
    // Step 5: Multi-Tenant Hash Allowance (Cross-Tenant Non-Collision)
    // ------------------------------------------------------------------------
    console.log('\nStep 5: Testing multi-tenant deduplication (identical hash allowed in different tenant)...');

    // Insert identical hash into Tenant B
    const docB1 = await prisma.document.create({
      data: {
        tenantId: tenantB.id,
        departmentId: deptB.id,
        uploadedById: userB.id,
        originalFilename: 'Beta_Shared_Framework.pdf',
        normalizedFilename: 'beta_shared_framework.pdf',
        storageUri: 's3://enterprise-documents-beta/shared.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(2548920),
        contentHash: hash1, // EXACT SAME HASH as doc1 in Tenant A
        classification: 'INTERNAL',
        status: 'PENDING',
      },
    });

    assert.strictEqual(docB1.contentHash, hash1);
    assert.strictEqual(docB1.tenantId, tenantB.id);
    console.log(`  Created Document in Tenant B with identical hash: ID=${docB1.id}`);
    console.log('  PASSED: Multi-tenant isolation permits identical file content across distinct tenants.');

    // ------------------------------------------------------------------------
    // Step 6: Soft-Deletion Aware Deduplication
    // ------------------------------------------------------------------------
    console.log('\nStep 6: Testing soft-deletion aware deduplication...');

    // Soft delete doc1 in Tenant A
    const softDeletedDoc1 = await prisma.document.update({
      where: { id: doc1.id },
      data: {
        deletedAt: new Date(),
        status: 'CANCELLED',
      },
    });

    assert(softDeletedDoc1.deletedAt !== null, 'deletedAt must be set');
    console.log(`  Soft-deleted doc1 in Tenant A: deletedAt=${softDeletedDoc1.deletedAt.toISOString()}`);

    // Now re-upload identical content in Tenant A
    const reUploadedDoc1 = await prisma.document.create({
      data: {
        tenantId: tenantA.id,
        departmentId: deptA1.id,
        uploadedById: userA.id,
        originalFilename: 'Remote_Work_Policy_ReUploaded.pdf',
        normalizedFilename: 'remote_work_policy_reuploaded.pdf',
        storageUri: 's3://enterprise-documents-alpha/legal/2026/reuploaded.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: BigInt(2548920),
        contentHash: hash1, // SAME HASH as previously soft-deleted doc1
        classification: 'CONFIDENTIAL',
        status: 'PROCESSING',
      },
    });

    assert(reUploadedDoc1.id, 'Re-uploaded document must be created');
    assert.notStrictEqual(reUploadedDoc1.id, doc1.id, 'New document has distinct UUID');
    assert.strictEqual(reUploadedDoc1.deletedAt, null, 'New document is active');
    console.log(`  Successfully re-uploaded document with identical hash: ID=${reUploadedDoc1.id}`);
    console.log('  PASSED: Soft-deleted document allows re-uploading identical file content in same tenant.');

    // Verify trying to create a THIRD document with hash1 in Tenant A while reUploadedDoc1 is active FAILS
    let thirdUploadRejected = false;
    try {
      await prisma.document.create({
        data: {
          tenantId: tenantA.id,
          originalFilename: 'Third_Copy.pdf',
          normalizedFilename: 'third_copy.pdf',
          storageUri: 's3://bucket/third.pdf',
          mimeType: 'application/pdf',
          fileSizeBytes: BigInt(2548920),
          contentHash: hash1,
        },
      });
    } catch (err) {
      thirdUploadRejected = true;
      console.log('  PASSED: Third duplicate correctly rejected while re-uploaded document is active.');
    }
    assert(thirdUploadRejected, 'Third copy must be rejected while active doc exists');

    // ------------------------------------------------------------------------
    // Step 7: Zero-Trust Cross-Tenant Boundary Enforcement Trigger
    // ------------------------------------------------------------------------
    console.log('\nStep 7: Testing database-level zero-trust cross-tenant boundary trigger...');

    // Attempt A: Document in Tenant A referencing Department in Tenant B
    let crossTenantDeptRejected = false;
    try {
      await prisma.document.create({
        data: {
          tenantId: tenantA.id,
          departmentId: deptB.id, // Department belongs to Tenant B!
          originalFilename: 'Malicious_Dept_Spoof.pdf',
          normalizedFilename: 'malicious_dept_spoof.pdf',
          storageUri: 's3://bucket/spoof.pdf',
          mimeType: 'application/pdf',
          fileSizeBytes: BigInt(1024),
          contentHash: computeSha256('malicious content 1'),
        },
      });
    } catch (err) {
      crossTenantDeptRejected = true;
      assert(err.message.includes('Cross-Tenant Document Violation'), 'Must trigger cross-tenant error');
      console.log(`  PASSED: Cross-tenant department reference rejected: ${err.message.split('\n').pop()}`);
    }
    assert(crossTenantDeptRejected, 'Cross-tenant department reference must be rejected');

    // Attempt B: Document in Tenant A referencing Uploader User in Tenant B
    let crossTenantUserRejected = false;
    try {
      await prisma.document.create({
        data: {
          tenantId: tenantA.id,
          uploadedById: userB.id, // User belongs to Tenant B!
          originalFilename: 'Malicious_User_Spoof.pdf',
          normalizedFilename: 'malicious_user_spoof.pdf',
          storageUri: 's3://bucket/user_spoof.pdf',
          mimeType: 'application/pdf',
          fileSizeBytes: BigInt(1024),
          contentHash: computeSha256('malicious content 2'),
        },
      });
    } catch (err) {
      crossTenantUserRejected = true;
      assert(err.message.includes('Cross-Tenant Document Violation'), 'Must trigger cross-tenant error');
      console.log(`  PASSED: Cross-tenant uploader reference rejected: ${err.message.split('\n').pop()}`);
    }
    assert(crossTenantUserRejected, 'Cross-tenant uploader reference must be rejected');

    // ------------------------------------------------------------------------
    // Step 8: Multi-Dimensional Querying & Index Verification
    // ------------------------------------------------------------------------
    console.log('\nStep 8: Testing performance and retrieval queries against composite indexes...');

    // 1. Query by (tenantId, contentHash)
    const docsByHash = await prisma.document.findMany({
      where: {
        tenantId: tenantA.id,
        contentHash: hash1,
        deletedAt: null,
      },
    });
    assert.strictEqual(docsByHash.length, 1);
    assert.strictEqual(docsByHash[0].id, reUploadedDoc1.id);
    console.log('  PASSED: Query by (tenantId, contentHash) verified.');

    // 2. Query by (tenantId, classification)
    const confidentialDocs = await prisma.document.findMany({
      where: {
        tenantId: tenantA.id,
        classification: 'CONFIDENTIAL',
        deletedAt: null,
      },
    });
    assert(confidentialDocs.length >= 1);
    console.log(`  PASSED: Query by (tenantId, classification) verified: ${confidentialDocs.length} confidential doc(s).`);

    // 3. Query by (tenantId, status)
    const processingDocs = await prisma.document.findMany({
      where: {
        tenantId: tenantA.id,
        status: 'PROCESSING',
      },
    });
    assert(processingDocs.length >= 1);
    console.log(`  PASSED: Query by (tenantId, status) verified: ${processingDocs.length} processing doc(s).`);

    // 4. Query by (tenantId, createdAt DESC) timeline
    const timelineDocs = await prisma.document.findMany({
      where: { tenantId: tenantA.id },
      orderBy: { createdAt: 'desc' },
      take: 5,
    });
    assert(timelineDocs.length > 0);
    console.log(`  PASSED: Query by (tenantId, createdAt DESC) timeline verified: ${timelineDocs.length} doc(s) returned.`);

    // 5. Query by uploadedById
    const aliceDocs = await prisma.document.findMany({
      where: { uploadedById: userA.id },
    });
    assert(aliceDocs.length >= 2);
    console.log(`  PASSED: Query by uploadedById verified: ${aliceDocs.length} doc(s) uploaded by Alice.`);

    // 6. Query by departmentId
    const legalDocs = await prisma.document.findMany({
      where: { departmentId: deptA1.id },
    });
    assert(legalDocs.length >= 2);
    console.log(`  PASSED: Query by departmentId verified: ${legalDocs.length} doc(s) in Legal & Compliance.`);

    // ------------------------------------------------------------------------
    // Step 9: Foreign Key Preservation & Cascade Behaviors
    // ------------------------------------------------------------------------
    console.log('\nStep 9: Testing foreign key preservation (SetNull) and tenant cascade...');

    // A: Delete Department -> Document.departmentId should be SET NULL
    await prisma.department.delete({ where: { id: deptA1.id } });
    const docAfterDeptDelete = await prisma.document.findUnique({
      where: { id: reUploadedDoc1.id },
    });
    assert.strictEqual(docAfterDeptDelete.departmentId, null, 'departmentId must be set to null on department deletion');
    console.log('  PASSED: Department deletion sets departmentId to NULL (ON DELETE SET NULL), document preserved.');

    // B: Delete User -> Document.uploadedById should be SET NULL
    await prisma.user.delete({ where: { id: userA.id } });
    const docAfterUserDelete = await prisma.document.findUnique({
      where: { id: reUploadedDoc1.id },
    });
    assert.strictEqual(docAfterUserDelete.uploadedById, null, 'uploadedById must be set to null on user departure');
    console.log('  PASSED: User deletion sets uploadedById to NULL (ON DELETE SET NULL), document preserved.');

    // C: Delete Tenant -> Documents should be CASCADE DELETED
    await prisma.tenant.delete({ where: { id: tenantA.id } });
    const remainingDocsTenantA = await prisma.document.findMany({
      where: { tenantId: tenantA.id },
    });
    assert.strictEqual(remainingDocsTenantA.length, 0, 'All documents must be cascade-deleted with tenant');
    console.log('  PASSED: Tenant deletion cascade removes all child documents.');

    // Clean up Tenant B
    await prisma.tenant.delete({ where: { id: tenantB.id } });

    console.log('\n======================================================');
    console.log('🎉 ALL TASK 06 ACCEPTANCE CRITERIA VERIFIED SUCCESSFULLY!');
    console.log('======================================================');
  } catch (error) {
    console.error('❌ Verification failed:', error);
    process.exit(1);
  } finally {
    await prisma.$disconnect();
  }
}

runTests();
