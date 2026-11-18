-- Migration: Enterprise Knowledge Agent – Permission-Aware Advanced RAG Platform
-- Database: PostgreSQL 16+ with pgvector extension enabled

-- 1. Enable pgvector and uuid extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 2. HNSW Vector Indexes (TRD 3.2: m = 16, ef_construct = 128)
-- Using vector_cosine_ops for cosine similarity search (<=> operator)

-- HNSW Index for ChatMessage conversation embedding
CREATE INDEX IF NOT EXISTS chat_messages_embedding_hnsw_idx
ON chat_messages
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 128);

-- HNSW Index for UserMemory semantic retrieval
CREATE INDEX IF NOT EXISTS user_memories_embedding_hnsw_idx
ON user_memories
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 128);

-- HNSW Index for LearnedUserInfo dynamic profiling
CREATE INDEX IF NOT EXISTS learned_user_info_embedding_hnsw_idx
ON learned_user_info
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 128);

-- 3. Full-Text Search (GIN) Index for Document Chunks (TRD 4.1: Sparse BM25 / Lexical Search)
CREATE INDEX IF NOT EXISTS document_chunks_content_fts_idx
ON document_chunks
USING gin (to_tsvector('simple', content));
