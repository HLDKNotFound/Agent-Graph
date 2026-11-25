"""PostgreSQL 16+ GIN full-text lexical search service (sparse BM25 retrieval)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Sequence

from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.exceptions import (
    RetrievalSecurityViolationError,
    SparseRetrievalError,
)
from backend.retrieval.types.chunk import (
    ChunkLocatorCoordinates,
    RetrievedChunk,
    RetrievedChunkSource,
)
from backend.retrieval.types.context import RetrievalContext

logger = logging.getLogger(__name__)


class PostgresBM25Retriever:
    """Stage-1 Sparse Lexical Retrieval Service backed by PostgreSQL 16+ GIN Expression Index.

    Capabilities:
    1. PostgreSQL GIN Lexical Index: Leverages `to_tsvector('simple', content)` and `websearch_to_tsquery`.
    2. Proximity-Aware Ranking: Utilizes `ts_rank_cd` with length normalization flag 32.
    3. Multi-Tenant Isolation & RBAC Pre-Filtering: Scoped strictly to `tenant_id`, verifying
       document soft-deletion status, classification tiers, and departmental boundaries.
    4. Format Provenance Mapping: Extracts page numbers, headings, and spreadsheet row ranges.
    5. Pluggable Connection Pool: Supports asyncpg connection pools with connection reuse.
    """

    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        pool: Any = None,
        connection_url: str | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.connection_url = connection_url or self.settings.database_url
        self._pool = pool
        self._pool_lock = asyncio.Lock()

    async def get_pool(self) -> Any:
        """Provide or lazily initialize shared asyncpg connection pool."""
        if self._pool is not None:
            return self._pool

        async with self._pool_lock:
            if self._pool is None:
                try:
                    import asyncpg
                except ImportError as exc:
                    raise SparseRetrievalError(
                        "asyncpg package is required for PostgresBM25Retriever",
                        details={"error": str(exc)},
                    ) from exc

                # Strip Prisma query parameters (e.g. ?schema=public) for asyncpg
                clean_url = self.connection_url.split("?")[0]
                try:
                    self._pool = await asyncpg.create_pool(
                        dsn=clean_url,
                        min_size=1,
                        max_size=10,
                    )
                    logger.info("Initialized asyncpg pool for sparse BM25 retrieval")
                except Exception as exc:
                    logger.error("Failed to connect to PostgreSQL for BM25: %s", exc)
                    raise SparseRetrievalError(
                        f"Failed to initialize PostgreSQL connection pool: {exc}",
                        details={"url": clean_url, "error": str(exc)},
                    ) from exc

        return self._pool

    async def close(self) -> None:
        """Close connection pool if owned."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def build_sql_query(
        self,
        context: RetrievalContext,
        query_text: str,
        limit: int,
    ) -> tuple[str, list[Any]]:
        """Construct parameterized, SQL-injection safe SQL query with zero-trust RBAC filters.

        Args:
            context: Validated RetrievalContext for caller.
            query_text: User search phrase.
            limit: Maximum candidate passages (K1).

        Returns:
            Tuple of (parameterized_sql_string, parameter_values_list).
        """
        if not context.tenant_id:
            raise RetrievalSecurityViolationError("Tenant ID is required for sparse retrieval")

        clean_classifications = [c.strip().upper() for c in context.classifications] if context.classifications else ["PUBLIC", "INTERNAL"]
        clean_roles = [r.strip() for r in context.roles if r.strip()]
        clean_depts = [d.strip() for d in context.departments if d.strip()]

        # Parameter order:
        # $1: query_text
        # $2: tenant_id (UUID)
        # $3: classifications array (text[])
        # $4: roles array (text[])
        # $5: departments array (text[])
        # $6: limit (int)

        sql = """
        SELECT 
            c.id AS chunk_id,
            c.tenant_id,
            c.document_id,
            c.document_version_id,
            c.chunk_index,
            c.content,
            c.token_count,
            c.page_number,
            c.section_heading,
            c.sheet_name,
            c.row_range,
            c.char_start,
            c.char_end,
            c.metadata AS chunk_metadata,
            d.original_filename,
            d.classification,
            d.department_id,
            ts_rank_cd(to_tsvector('simple', c.content), websearch_to_tsquery('simple', $1), 32) AS relevance_score
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.tenant_id = $2::uuid
          AND d.tenant_id = $2::uuid
          AND d.deleted_at IS NULL
          AND to_tsvector('simple', c.content) @@ websearch_to_tsquery('simple', $1)
          AND (
              d.classification::text = 'PUBLIC'
              OR d.classification::text = ANY($3::text[])
              OR (
                  c.metadata->'allowed_roles' IS NOT NULL 
                  AND EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(COALESCE(c.metadata->'allowed_roles', '[]'::jsonb)) r 
                      WHERE r = ANY($4::text[])
                  )
              )
              OR (
                  c.metadata->'allowed_departments' IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(COALESCE(c.metadata->'allowed_departments', '[]'::jsonb)) dept
                      WHERE dept = ANY($5::text[])
                  )
              )
          )
        ORDER BY relevance_score DESC, c.id ASC
        LIMIT $6::int;
        """

        params: list[Any] = [
            query_text,
            str(context.tenant_id),
            clean_classifications,
            clean_roles,
            clean_depts,
            limit,
        ]
        return sql, params

    async def search(
        self,
        query_text: str,
        context: RetrievalContext,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Execute sparse lexical keyword search against PostgreSQL GIN index.

        Args:
            query_text: User search phrase.
            context: Zero-trust RetrievalContext.
            top_k: Candidate ceiling (defaults to settings.sparse_top_k = 40).

        Returns:
            Ranked list of RetrievedChunk instances with source=SPARSE.
        """
        if not query_text or not query_text.strip():
            return []

        limit = top_k or self.settings.sparse_top_k
        sql, params = self.build_sql_query(context, query_text.strip(), limit)

        pool = await self.get_pool()
        try:
            async with pool.acquire() as conn:
                try:
                    records = await conn.fetch(sql, *params)
                except Exception as inner_exc:
                    # Fallback to plainto_tsquery if websearch_to_tsquery encountered syntax error
                    logger.warning("websearch_to_tsquery failed, falling back to plainto_tsquery: %s", inner_exc)
                    fallback_sql = sql.replace("websearch_to_tsquery", "plainto_tsquery")
                    records = await conn.fetch(fallback_sql, *params)
        except Exception as exc:
            logger.error("PostgreSQL BM25 sparse search failed: %s", exc, exc_info=True)
            raise SparseRetrievalError(
                f"PostgreSQL sparse lexical search failed: {exc}",
                details={"query": query_text, "tenant_id": str(context.tenant_id), "error": str(exc)},
            ) from exc

        chunks: list[RetrievedChunk] = []
        for rank, row in enumerate(records, start=1):
            chunk = self._map_row_to_retrieved_chunk(row, rank)
            chunks.append(chunk)

        return chunks

    def _map_row_to_retrieved_chunk(
        self,
        row: Any,
        rank: int,
    ) -> RetrievedChunk:
        """Map PostgreSQL record to immutable RetrievedChunk contract."""
        chunk_id = row["chunk_id"] if isinstance(row["chunk_id"], uuid.UUID) else uuid.UUID(str(row["chunk_id"]))
        doc_id = row["document_id"] if isinstance(row["document_id"], uuid.UUID) else uuid.UUID(str(row["document_id"]))

        doc_version_id = None
        if row.get("document_version_id"):
            val = row["document_version_id"]
            doc_version_id = val if isinstance(val, uuid.UUID) else uuid.UUID(str(val))

        department_id = None
        if row.get("department_id"):
            dept_val = row["department_id"]
            department_id = dept_val if isinstance(dept_val, uuid.UUID) else uuid.UUID(str(dept_val))

        raw_meta = row.get("chunk_metadata") or {}
        if isinstance(raw_meta, str):
            try:
                metadata = json.loads(raw_meta)
            except Exception:
                metadata = {}
        else:
            metadata = dict(raw_meta)

        locator = ChunkLocatorCoordinates(
            page_number=row.get("page_number"),
            section_heading=row.get("section_heading"),
            sheet_name=row.get("sheet_name"),
            row_range=row.get("row_range"),
            char_start=row.get("char_start"),
            char_end=row.get("char_end"),
        )

        return RetrievedChunk(
            chunk_id=chunk_id,
            document_id=doc_id,
            document_version_id=doc_version_id,
            content=row["content"],
            chunk_index=row.get("chunk_index", 0),
            token_count=row.get("token_count", 0),
            locator=locator,
            sparse_score=float(row.get("relevance_score") or 0.0),
            sparse_rank=rank,
            source=RetrievedChunkSource.SPARSE,
            classification=str(row.get("classification") or "INTERNAL").upper(),
            department_id=department_id,
            document_title=row.get("original_filename"),
            metadata=metadata,
        )
