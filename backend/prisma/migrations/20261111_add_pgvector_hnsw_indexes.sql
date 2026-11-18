-- Migration: Add HNSW Vector Indexes for pgvector
-- Target DB: PostgreSQL with pgvector extension enabled

-- 1. Enable pgvector extension (if not already enabled)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. HNSW Index for UserMemory (semantic memory retrieval)
-- Using vector_cosine_ops for cosine similarity search (<=> operator)
CREATE INDEX IF NOT EXISTS user_memories_embedding_hnsw_idx
ON user_memories
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 3. HNSW Index for FeedbackLearning (few-shot / similar question matching)
CREATE INDEX IF NOT EXISTS feedback_learnings_query_embedding_hnsw_idx
ON feedback_learnings
USING hnsw (query_embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 4. HNSW Index for ChatMessage (conversation context similarity)
CREATE INDEX IF NOT EXISTS chat_messages_embedding_hnsw_idx
ON chat_messages
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 5. HNSW Index for LearnedUserInfo (dynamic user profile semantic search)
CREATE INDEX IF NOT EXISTS learned_user_info_embedding_hnsw_idx
ON learned_user_info
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
