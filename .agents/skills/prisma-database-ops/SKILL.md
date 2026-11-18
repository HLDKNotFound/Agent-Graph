---
name: prisma-database-ops
description: >-
  Manages Prisma schema validation, migrations, pgvector HNSW vector indexes, and PostgreSQL GIN full-text search
  indexes for the Enterprise Knowledge Agent. Use this skill when modifying database models, generating Prisma clients,
  verifying vector similarity indexes, or managing multi-tenant relational persistence.
---

# Prisma Database & Vector Store Operations

This skill outlines operational procedures for managing the PostgreSQL relational database and pgvector extension using Prisma ORM without hardcoded connection strings or credentials.

---

## 1. Schema Validation & Formatting

Always format and validate the schema before creating migrations or committing changes:
```bash
# Format schema across both canonical locations
npx prisma format --schema backend/schema.prisma
cp backend/schema.prisma backend/prisma/schema.prisma

# Validate schema structure
npx prisma validate --schema backend/schema.prisma
```

---

## 2. Generating Migrations & Applying Schema

Ensure `DATABASE_URL` and `DIRECT_URL` are provided dynamically via environment variables or `.env`:

```bash
# Create a new named migration in development
npx prisma migrate dev --name <migration_name> --schema backend/schema.prisma

# Apply existing migrations in staging/production
npx prisma migrate deploy --schema backend/schema.prisma

# Generate Prisma Client TypeScript/JavaScript types
npx prisma generate --schema backend/schema.prisma
```

---

## 3. Managing pgvector HNSW & Full-Text Search (GIN) Indexes

Prisma does not manage HNSW or GIN index parameters directly within `@index` annotations. Always verify that migration SQL scripts contain the required PostgreSQL index statements:

### HNSW Index Configuration (TRD 3.2: $m=16, ef\_construction=128$):
```sql
-- Enable vector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- HNSW Vector Index for ChatMessage conversation embedding
CREATE INDEX IF NOT EXISTS chat_messages_embedding_hnsw_idx
ON chat_messages
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 128);

-- HNSW Vector Index for UserMemory semantic retrieval
CREATE INDEX IF NOT EXISTS user_memories_embedding_hnsw_idx
ON user_memories
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 128);

-- HNSW Vector Index for LearnedUserInfo dynamic profiling
CREATE INDEX IF NOT EXISTS learned_user_info_embedding_hnsw_idx
ON learned_user_info
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 128);
```

### Full-Text Search (GIN) Index for BM25 Sparse Search (TRD 4.1):
```sql
-- GIN Index on DocumentChunk content for fast lexical matching
CREATE INDEX IF NOT EXISTS document_chunks_content_fts_idx
ON document_chunks
USING gin (to_tsvector('simple', content));
```

---

## 4. Multi-Tenant Relational Isolation Verification

When executing database queries or seeding test data:
1. Always scope queries to `tenantId`. Never execute queries that span across tenants without administrative privileges.
2. Verify composite foreign keys (e.g. `UserDepartment(userId, departmentId)`, `MessageCitation(messageId, marker)`).
3. Soft delete check: Always verify `is_deleted = false` when retrieving documents and chunks.
