/**
 * Enterprise Knowledge Agent – PostgreSQL Native Full-Text Search Service
 *
 * Implements high-throughput, parameterized, SQL-injection-safe lexical search
 * leveraging PostgreSQL 16+ GIN expression indexing on `document_chunks`.
 *
 * Capabilities:
 * 1. Expression Index: `idx_chunks_fts` USING GIN (to_tsvector('simple', content))
 * 2. Parsers: to_tsvector, plainto_tsquery, websearch_to_tsquery
 * 3. Ranking: ts_rank (TF-based) and ts_rank_cd (Cover Density proximity) with length normalization
 * 4. Multi-Dimensional Filtering: Tenant ID, Document ID, Document Version, Classification, Department, Status
 * 5. Zero-Trust RBAC: Pre-retrieval authorization barrier evaluating roles, permissions, department boundaries, and clearance levels
 * 6. Hybrid RRF Extensibility: Provides rank indexing (1..K) and normalized score calculations ready for Reciprocal Rank Fusion
 *
 * Architectural Note:
 * PostgreSQL native FTS is a lexical search index, NOT a true Okapi BM25 implementation.
 * ts_rank/ts_rank_cd computes term frequency and cover density without maintaining corpus-wide
 * inverse document frequency (IDF) or non-linear term saturation (k1, b).
 */

const { PrismaClient } = require('@prisma/client');

/**
 * Standard SQL queries and parameter bindings
 */
class FullTextSearchService {
  constructor(prisma = new PrismaClient()) {
    this.prisma = prisma;
  }

  /**
   * 1. Basic Full-Text Search
   * Parameterized search using plainto_tsquery or websearch_to_tsquery.
   *
   * @param {Object} params
   * @param {string} params.tenantId - Owning tenant UUID
   * @param {string} params.queryText - User search query
   * @param {'plain'|'websearch'} [params.queryMode='plain'] - Parser mode
   * @param {number} [params.limit=10] - Result limit
   * @returns {Promise<Array>}
   */
  async searchBasic({ tenantId, queryText, queryMode = 'plain', limit = 10 }) {
    if (!tenantId || !queryText) {
      throw new Error('tenantId and queryText are required for full-text search');
    }

    if (queryMode === 'websearch') {
      return await this.prisma.$queryRaw`
        SELECT 
          c."id" AS chunk_id,
          c."tenant_id",
          c."document_id",
          c."document_version_id",
          c."chunk_index",
          c."content",
          c."token_count",
          c."page_number",
          c."section_heading",
          c."sheet_name",
          c."row_range",
          d."original_filename",
          d."classification",
          ts_rank_cd(to_tsvector('simple', c."content"), websearch_to_tsquery('simple', ${queryText}), 1) AS relevance_score
        FROM "document_chunks" c
        JOIN "documents" d ON d."id" = c."document_id"
        WHERE c."tenant_id" = ${tenantId}::uuid
          AND d."tenant_id" = ${tenantId}::uuid
          AND d."deleted_at" IS NULL
          AND to_tsvector('simple', c."content") @@ websearch_to_tsquery('simple', ${queryText})
        ORDER BY relevance_score DESC, c."id" ASC
        LIMIT ${limit}::int;
      `;
    }

    return await this.prisma.$queryRaw`
      SELECT 
        c."id" AS chunk_id,
        c."tenant_id",
        c."document_id",
        c."document_version_id",
        c."chunk_index",
        c."content",
        c."token_count",
        c."page_number",
        c."section_heading",
        c."sheet_name",
        c."row_range",
        d."original_filename",
        d."classification",
        ts_rank_cd(to_tsvector('simple', c."content"), plainto_tsquery('simple', ${queryText}), 1) AS relevance_score
      FROM "document_chunks" c
      JOIN "documents" d ON d."id" = c."document_id"
      WHERE c."tenant_id" = ${tenantId}::uuid
        AND d."tenant_id" = ${tenantId}::uuid
        AND d."deleted_at" IS NULL
        AND to_tsvector('simple', c."content") @@ plainto_tsquery('simple', ${queryText})
      ORDER BY relevance_score DESC, c."id" ASC
      LIMIT ${limit}::int;
    `;
  }

  /**
   * 2. Advanced Filtered Search
   * Supports filtering by tenant, document, version, classification, department, and status.
   *
   * @param {Object} params
   * @param {string} params.tenantId - Tenant UUID
   * @param {string} params.queryText - Query string
   * @param {string} [params.documentId] - Filter by specific document UUID
   * @param {string} [params.documentVersionId] - Filter by specific version UUID
   * @param {boolean} [params.currentVersionOnly=false] - Only return chunks from the current version
   * @param {string[]} [params.classifications] - Array of allowed classifications (e.g. ['PUBLIC', 'INTERNAL'])
   * @param {string} [params.departmentId] - Filter by department UUID
   * @param {string} [params.ingestionStatus='COMPLETED'] - Document status filter
   * @param {'ts_rank'|'ts_rank_cd'} [params.rankMethod='ts_rank_cd'] - Ranking function
   * @param {number} [params.normalizationFlag=1] - Length normalization flag
   * @param {number} [params.limit=10]
   * @param {number} [params.offset=0]
   * @returns {Promise<{items: Array, totalCount: number}>}
   */
  async searchFiltered({
    tenantId,
    queryText,
    documentId = null,
    documentVersionId = null,
    currentVersionOnly = false,
    classifications = null,
    departmentId = null,
    ingestionStatus = 'COMPLETED',
    rankMethod = 'ts_rank_cd',
    normalizationFlag = 1,
    limit = 10,
    offset = 0,
  }) {
    const hasClassifications = Boolean(classifications && classifications.length > 0);
    const classificationsArr = hasClassifications ? classifications : [];
    const isCurrentOnly = Boolean(currentVersionOnly);

    // Parameterized query executing with full dynamic filters
    const rawResults = await this.prisma.$queryRaw`
      WITH matched_chunks AS (
        SELECT 
          c."id" AS chunk_id,
          c."tenant_id",
          c."document_id",
          c."document_version_id",
          c."chunk_index",
          c."content",
          c."token_count",
          c."page_number",
          c."section_heading",
          c."sheet_name",
          c."row_range",
          c."char_start",
          c."char_end",
          d."original_filename",
          d."classification",
          d."department_id",
          dept."name" AS department_name,
          v."version_number",
          v."is_current",
          CASE 
            WHEN ${rankMethod}::text = 'ts_rank' 
              THEN ts_rank(to_tsvector('simple', c."content"), websearch_to_tsquery('simple', ${queryText}), ${normalizationFlag}::int)
            ELSE ts_rank_cd(to_tsvector('simple', c."content"), websearch_to_tsquery('simple', ${queryText}), ${normalizationFlag}::int)
          END AS relevance_score,
          COUNT(*) OVER() AS full_count
        FROM "document_chunks" c
        JOIN "documents" d ON d."id" = c."document_id"
        LEFT JOIN "document_versions" v ON v."id" = c."document_version_id"
        LEFT JOIN "departments" dept ON dept."id" = d."department_id"
        WHERE c."tenant_id" = ${tenantId}::uuid
          AND d."tenant_id" = ${tenantId}::uuid
          AND d."deleted_at" IS NULL
          AND (v."id" IS NULL OR v."deleted_at" IS NULL)
          AND (${documentId}::uuid IS NULL OR c."document_id" = ${documentId}::uuid)
          AND (${documentVersionId}::uuid IS NULL OR c."document_version_id" = ${documentVersionId}::uuid)
          AND (${isCurrentOnly}::boolean = false OR v."is_current" = true OR (c."document_version_id" IS NULL AND d."current_version_id" IS NULL))
          AND (${departmentId}::uuid IS NULL OR d."department_id" = ${departmentId}::uuid)
          AND (${ingestionStatus}::text IS NULL OR d."status"::text = ${ingestionStatus})
          AND (${hasClassifications}::boolean = false OR d."classification"::text = ANY(${classificationsArr}::text[]))
          AND to_tsvector('simple', c."content") @@ websearch_to_tsquery('simple', ${queryText})
      )
      SELECT * FROM matched_chunks
      ORDER BY relevance_score DESC, chunk_id ASC
      LIMIT ${limit}::int OFFSET ${offset}::int;
    `;

    const totalCount = rawResults.length > 0 ? Number(rawResults[0].full_count) : 0;
    const items = rawResults.map((r) => {
      const { full_count, ...cleanRow } = r;
      return cleanRow;
    });

    return { items, totalCount };
  }

  /**
   * 3. Zero-Trust RBAC Permission-Enforced Search
   * Enforces role-based capabilities, classification clearance, and department boundaries
   * BEFORE returning document content to unauthorized users.
   *
   * @param {Object} params
   * @param {string} params.tenantId - Tenant UUID
   * @param {string} params.userId - Authenticated User UUID
   * @param {string} params.queryText - Query string
   * @param {Object} [params.filters={}] - Additional filters
   * @param {number} [params.limit=10]
   * @param {number} [params.offset=0]
   * @returns {Promise<{items: Array, totalCount: number}>}
   */
  async searchWithPermissions({
    tenantId,
    userId,
    queryText,
    filters = {},
    limit = 10,
    offset = 0,
  }) {
    if (!tenantId || !userId || !queryText) {
      throw new Error('tenantId, userId, and queryText are required for permission-aware search');
    }

    const {
      classification = null,
      departmentId = null,
      documentId = null,
      currentVersionOnly = true,
    } = filters;

    const rawResults = await this.prisma.$queryRaw`
      WITH user_context AS (
        SELECT 
          u."id" AS user_id,
          u."tenant_id",
          u."department_id" AS user_dept_id,
          COALESCE(array_agg(p."key") FILTER (WHERE p."key" IS NOT NULL), '{}') AS permission_keys
        FROM "users" u
        LEFT JOIN "user_roles" ur ON ur."user_id" = u."id"
        LEFT JOIN "role_permissions" rp ON rp."role_id" = ur."role_id"
        LEFT JOIN "permissions" p ON p."id" = rp."permission_id"
        WHERE u."id" = ${userId}::uuid
          AND u."tenant_id" = ${tenantId}::uuid
          AND u."status" = 'ACTIVE'
        GROUP BY u."id", u."tenant_id", u."department_id"
      ),
      authorized_chunks AS (
        SELECT 
          c."id" AS chunk_id,
          c."tenant_id",
          c."document_id",
          c."document_version_id",
          c."chunk_index",
          c."content",
          c."token_count",
          c."page_number",
          c."section_heading",
          c."sheet_name",
          c."row_range",
          d."original_filename",
          d."classification",
          d."department_id",
          dept."name" AS department_name,
          v."version_number",
          v."is_current",
          ts_rank_cd(to_tsvector('simple', c."content"), websearch_to_tsquery('simple', ${queryText}), 1) AS relevance_score,
          COUNT(*) OVER() AS full_count
        FROM "document_chunks" c
        JOIN "documents" d ON d."id" = c."document_id"
        LEFT JOIN "document_versions" v ON v."id" = c."document_version_id"
        LEFT JOIN "departments" dept ON dept."id" = d."department_id"
        CROSS JOIN user_context uc
        WHERE c."tenant_id" = ${tenantId}::uuid
          AND d."tenant_id" = ${tenantId}::uuid
          AND d."deleted_at" IS NULL
          AND d."status" = 'COMPLETED'
          AND (v."id" IS NULL OR (v."deleted_at" IS NULL AND v."status" = 'COMPLETED'))
          AND (${documentId}::uuid IS NULL OR c."document_id" = ${documentId}::uuid)
          AND (${Boolean(currentVersionOnly)}::boolean = false OR v."is_current" = true OR (c."document_version_id" IS NULL AND d."current_version_id" IS NULL))
          AND (${departmentId}::uuid IS NULL OR d."department_id" = ${departmentId}::uuid)
          AND (${classification}::text IS NULL OR d."classification"::text = ${classification})
          -- Lexical match via GIN index:
          AND to_tsvector('simple', c."content") @@ websearch_to_tsquery('simple', ${queryText})
          -- ZERO-TRUST PERMISSION ENFORCEMENT PREDICATE:
          AND (
            -- Tier 1: PUBLIC documents require basic read capability or public accessibility
            (d."classification" = 'PUBLIC' AND ('document:read' = ANY(uc.permission_keys) OR 'documents:read' = ANY(uc.permission_keys)))
            OR
            -- Tier 2: Tenant-wide administrator bypass
            ('documents:admin' = ANY(uc.permission_keys) OR 'documents:read_all' = ANY(uc.permission_keys))
            OR
            -- Tier 3: Standard employee read with department & classification isolation
            (
              ('document:read' = ANY(uc.permission_keys) OR 'documents:read' = ANY(uc.permission_keys))
              AND (
                -- INTERNAL: Accessible if same department or global internal
                (
                  d."classification" = 'INTERNAL'
                  AND (d."department_id" IS NULL OR d."department_id" = uc.user_dept_id OR 'departments:read_all' = ANY(uc.permission_keys))
                )
                OR
                -- CONFIDENTIAL: Requires confidential permission + department match
                (
                  d."classification" = 'CONFIDENTIAL'
                  AND ('documents:read_confidential' = ANY(uc.permission_keys) OR 'document:read_confidential' = ANY(uc.permission_keys))
                  AND (d."department_id" IS NULL OR d."department_id" = uc.user_dept_id OR 'departments:read_all' = ANY(uc.permission_keys))
                )
                OR
                -- RESTRICTED: Requires uploader ownership OR explicit restricted permission
                (
                  d."classification" = 'RESTRICTED'
                  AND (d."uploaded_by_id" = uc.user_id OR 'documents:read_restricted' = ANY(uc.permission_keys))
                )
              )
            )
          )
      )
      SELECT * FROM authorized_chunks
      ORDER BY relevance_score DESC, chunk_id ASC
      LIMIT ${limit}::int OFFSET ${offset}::int;
    `;

    const totalCount = rawResults.length > 0 ? Number(rawResults[0].full_count) : 0;
    const items = rawResults.map((r) => {
      const { full_count, ...cleanRow } = r;
      return cleanRow;
    });

    return { items, totalCount };
  }

  /**
   * 4. RRF-Extensible Candidate Retrieval
   * Retrieves candidate chunks formatted for Reciprocal Rank Fusion (RRF) with dense vector candidates.
   * Outputs 1-indexed rank order, raw relevance score, and pre-calculated sparse RRF contribution.
   *
   * Formula: RRF_Score = w_sparse / (k + rank)
   * Default: k = 60, w_sparse = 0.3
   *
   * @param {Object} params
   * @param {string} params.tenantId
   * @param {string} params.queryText
   * @param {number} [params.topK=40]
   * @param {number} [params.rrfK=60]
   * @param {number} [params.sparseWeight=0.3]
   * @returns {Promise<Array>}
   */
  async searchForRRF({
    tenantId,
    queryText,
    topK = 40,
    rrfK = 60,
    sparseWeight = 0.3,
  }) {
    return await this.prisma.$queryRaw`
      WITH ranked_lexical AS (
        SELECT 
          c."id" AS chunk_id,
          c."document_id",
          c."document_version_id",
          c."chunk_index",
          c."content",
          c."token_count",
          c."page_number",
          c."section_heading",
          c."embedding_id",
          c."vector_storage_id",
          d."original_filename",
          d."classification",
          ts_rank_cd(to_tsvector('simple', c."content"), websearch_to_tsquery('simple', ${queryText}), 1) AS raw_score,
          ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(to_tsvector('simple', c."content"), websearch_to_tsquery('simple', ${queryText}), 1) DESC, c."id" ASC
          )::int AS rank_position
        FROM "document_chunks" c
        JOIN "documents" d ON d."id" = c."document_id"
        LEFT JOIN "document_versions" v ON v."id" = c."document_version_id"
        WHERE c."tenant_id" = ${tenantId}::uuid
          AND d."tenant_id" = ${tenantId}::uuid
          AND d."deleted_at" IS NULL
          AND d."status" = 'COMPLETED'
          AND (v."id" IS NULL OR (v."deleted_at" IS NULL AND v."status" = 'COMPLETED'))
          AND (v."is_current" = true OR (c."document_version_id" IS NULL AND d."current_version_id" IS NULL))
          AND to_tsvector('simple', c."content") @@ websearch_to_tsquery('simple', ${queryText})
        LIMIT ${topK}::int
      )
      SELECT 
        chunk_id,
        document_id,
        document_version_id,
        chunk_index,
        content,
        token_count,
        page_number,
        section_heading,
        embedding_id,
        vector_storage_id,
        original_filename,
        classification,
        raw_score,
        rank_position,
        -- Calculate pre-computed sparse reciprocal rank contribution
        (${sparseWeight}::float / (${rrfK}::float + rank_position::float)) AS sparse_rrf_score
      FROM ranked_lexical
      ORDER BY rank_position ASC;
    `;
  }

  /**
   * 5. Ranker Comparison Helper
   * Compares `ts_rank` (Term Frequency) vs `ts_rank_cd` (Cover Density proximity) on identical content.
   *
   * @param {string} tenantId
   * @param {string} queryText
   * @returns {Promise<Array>}
   */
  async compareRankers(tenantId, queryText) {
    return await this.prisma.$queryRaw`
      SELECT 
        c."id" AS chunk_id,
        c."chunk_index",
        c."section_heading",
        SUBSTRING(c."content", 1, 120) AS content_preview,
        ts_rank(to_tsvector('simple', c."content"), plainto_tsquery('simple', ${queryText}), 1) AS score_ts_rank_tf,
        ts_rank_cd(to_tsvector('simple', c."content"), plainto_tsquery('simple', ${queryText}), 1) AS score_ts_rank_cd_proximity
      FROM "document_chunks" c
      WHERE c."tenant_id" = ${tenantId}::uuid
        AND to_tsvector('simple', c."content") @@ plainto_tsquery('simple', ${queryText})
      ORDER BY score_ts_rank_cd_proximity DESC, score_ts_rank_tf DESC
      LIMIT 10;
    `;
  }
}

module.exports = {
  FullTextSearchService,
};
