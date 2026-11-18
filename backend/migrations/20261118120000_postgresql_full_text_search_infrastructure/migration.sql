-- Migration: PostgreSQL Full-Text Search Infrastructure
-- Target Database: PostgreSQL 16+ with GIN Index for Document Chunks

-- ============================================================================
-- 1. GIN EXPRESSION INDEX ON DOCUMENT_CHUNKS CONTENT
-- ============================================================================
-- Creates the GIN index on to_tsvector('simple', content) for fast lexical retrieval.
-- Using 'simple' dictionary configuration ensures consistent, language-agnostic tokenization
-- across multilingual enterprise text (Vietnamese, English, mixed technical acronyms, codes)
-- without destructive stemming or accidental stopword omission.

CREATE INDEX IF NOT EXISTS "idx_chunks_fts"
ON "document_chunks"
USING GIN (to_tsvector('simple', "content"));

COMMENT ON INDEX "idx_chunks_fts" IS 'PostgreSQL GIN full-text search index for DocumentChunk lexical retrieval using simple dictionary';
