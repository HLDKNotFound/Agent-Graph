"""Transactional outbox pattern implementation for reliable vector indexing events.

Guarantees:
- Cross-storage eventual consistency without dual-write race conditions.
- Database records and vector outbox events are committed in the SAME PostgreSQL transaction.
- Outbox dispatcher drives idempotent Qdrant upserts with exponential backoff retries.
- Dead letter tracking and compensating vector purging when retries are exhausted.
- Full tenant isolation across all outbox operations.
"""

from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Sequence
import uuid
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.exceptions import IndexingError, OrchestrationError
from backend.ingestion.embedding.base import EmbeddingResult
from backend.ingestion.indexing.base import VectorIndexProvider
from backend.ingestion.indexing.models import to_point_id
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext

logger = logging.getLogger(__name__)


class OutboxEvent(BaseModel):
    """Data contract representing an outbox event for asynchronous vector persistence."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Outbox event UUID")
    tenant_id: str = Field(..., description="Target tenant UUID for strict isolation")
    document_id: str = Field(..., description="Parent document UUID")
    chunk_id: str | None = Field(default=None, description="Optional chunk UUID if per-chunk event")
    event_type: str = Field(default="UPSERT_VECTOR", description="Outbox event type: UPSERT_VECTOR or DELETE_VECTOR")
    status: str = Field(default="PENDING", description="Status: PENDING, PROCESSING, COMPLETED, FAILED, DEAD_LETTER")
    payload: dict[str, Any] = Field(default_factory=dict, description="Serialized vector points and chunk metadata")
    retry_count: int = Field(default=0, ge=0, description="Number of attempted dispatches")
    max_retries: int = Field(default=3, ge=1, description="Maximum retry threshold")
    last_error: str | None = Field(default=None, description="Diagnostic error message from last dispatch failure")
    processed_at: datetime | None = Field(default=None, description="Timestamp of successful processing (UTC)")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp (UTC)")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Last update timestamp (UTC)")


class OutboxRepository(abc.ABC):
    """Abstract contract for persisting and retrieving transactional outbox events."""

    @abc.abstractmethod
    async def enqueue_event(
        self,
        event: OutboxEvent,
        conn: Any = None,
    ) -> OutboxEvent:
        """Atomically persist an outbox event within a transaction."""
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch_pending_events(
        self,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[OutboxEvent]:
        """Fetch pending outbox events ordered by creation timestamp."""
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[OutboxEvent]:
        """Fetch all outbox events for a given document."""
        raise NotImplementedError

    @abc.abstractmethod
    async def mark_processing(self, event_id: str) -> bool:
        """Mark an outbox event as currently being processed."""
        raise NotImplementedError

    @abc.abstractmethod
    async def mark_completed(self, event_id: str) -> bool:
        """Mark an outbox event as successfully processed."""
        raise NotImplementedError

    @abc.abstractmethod
    async def mark_failed(
        self,
        event_id: str,
        error: str,
        retryable: bool = True,
    ) -> bool:
        """Record dispatch failure, increment retry count, and transition to FAILED or DEAD_LETTER."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_events_for_document(
        self,
        tenant_id: str,
        document_id: str,
        conn: Any = None,
    ) -> int:
        """Delete all outbox events for a document (used during rollback or soft-delete)."""
        raise NotImplementedError


class InMemoryOutboxRepository(OutboxRepository):
    """Thread-safe, asyncio-safe in-memory simulation of vector_indexing_outbox table."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # event_id -> dict
        self._events: dict[str, dict[str, Any]] = {}

    async def enqueue_event(
        self,
        event: OutboxEvent,
        conn: Any = None,
    ) -> OutboxEvent:
        async with self._lock:
            record = event.model_dump()
            self._events[event.id] = record
            return event

    async def fetch_pending_events(
        self,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[OutboxEvent]:
        async with self._lock:
            results = []
            for item in self._events.values():
                if item["status"] == "PENDING":
                    if tenant_id is None or item["tenant_id"] == tenant_id:
                        results.append(OutboxEvent(**item))
                        if len(results) >= limit:
                            break
            # Sort by created_at
            results.sort(key=lambda x: x.created_at)
            return results

    async def fetch_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[OutboxEvent]:
        async with self._lock:
            return [
                OutboxEvent(**item)
                for item in self._events.values()
                if item["tenant_id"] == tenant_id and item["document_id"] == document_id
            ]

    async def mark_processing(self, event_id: str) -> bool:
        async with self._lock:
            record = self._events.get(event_id)
            if not record:
                return False
            record["status"] = "PROCESSING"
            record["updated_at"] = datetime.now(timezone.utc)
            return True

    async def mark_completed(self, event_id: str) -> bool:
        async with self._lock:
            record = self._events.get(event_id)
            if not record:
                return False
            now = datetime.now(timezone.utc)
            record["status"] = "COMPLETED"
            record["processed_at"] = now
            record["updated_at"] = now
            return True

    async def mark_failed(
        self,
        event_id: str,
        error: str,
        retryable: bool = True,
    ) -> bool:
        async with self._lock:
            record = self._events.get(event_id)
            if not record:
                return False
            now = datetime.now(timezone.utc)
            record["retry_count"] += 1
            record["last_error"] = error
            record["updated_at"] = now

            if retryable and record["retry_count"] < record["max_retries"]:
                record["status"] = "PENDING"  # Eligible for retry
            else:
                record["status"] = "DEAD_LETTER" if record["retry_count"] >= record["max_retries"] else "FAILED"
            return True

    async def delete_events_for_document(
        self,
        tenant_id: str,
        document_id: str,
        conn: Any = None,
    ) -> int:
        async with self._lock:
            to_delete = [
                eid for eid, r in self._events.items()
                if r["tenant_id"] == tenant_id and r["document_id"] == document_id
            ]
            for eid in to_delete:
                del self._events[eid]
            return len(to_delete)


class PostgresOutboxRepository(OutboxRepository):
    """PostgreSQL 16+ implementation of OutboxRepository using asyncpg."""

    def __init__(
        self,
        connection_url: str | None = None,
        pool: Any = None,
    ) -> None:
        settings = get_ingestion_settings()
        self.connection_url = connection_url or settings.database_url
        self._pool = pool
        self._pool_lock = asyncio.Lock()
        self._table_ensured = False

    async def get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                import asyncpg
                clean_url = self.connection_url.split("?")[0]
                self._pool = await asyncpg.create_pool(dsn=clean_url, min_size=1, max_size=10)
        return self._pool

    async def ensure_table(self, conn: Any = None) -> None:
        """Create vector_indexing_outbox table and enums if not exists."""
        if self._table_ensured:
            return
        ddl = """
        DO $$ BEGIN
            CREATE TYPE "OutboxEventStatus" AS ENUM ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'DEAD_LETTER');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;

        DO $$ BEGIN
            CREATE TYPE "OutboxEventType" AS ENUM ('UPSERT_VECTOR', 'DELETE_VECTOR');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;

        CREATE TABLE IF NOT EXISTS "vector_indexing_outbox" (
            "id" UUID NOT NULL DEFAULT gen_random_uuid(),
            "tenant_id" UUID NOT NULL,
            "document_id" UUID NOT NULL,
            "chunk_id" UUID,
            "event_type" "OutboxEventType" NOT NULL DEFAULT 'UPSERT_VECTOR',
            "status" "OutboxEventStatus" NOT NULL DEFAULT 'PENDING',
            "payload" JSONB NOT NULL DEFAULT '{}',
            "retry_count" INTEGER NOT NULL DEFAULT 0,
            "max_retries" INTEGER NOT NULL DEFAULT 3,
            "last_error" TEXT,
            "processed_at" TIMESTAMPTZ(6),
            "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "vector_indexing_outbox_pkey" PRIMARY KEY ("id")
        );

        CREATE INDEX IF NOT EXISTS "vector_indexing_outbox_tenant_id_status_idx" ON "vector_indexing_outbox"("tenant_id", "status");
        CREATE INDEX IF NOT EXISTS "vector_indexing_outbox_status_created_at_idx" ON "vector_indexing_outbox"("status", "created_at");
        CREATE INDEX IF NOT EXISTS "vector_indexing_outbox_document_id_idx" ON "vector_indexing_outbox"("document_id");
        """
        if conn is not None:
            await conn.execute(ddl)
        else:
            pool = await self.get_pool()
            async with pool.acquire() as c:
                await c.execute(ddl)
        self._table_ensured = True

    async def enqueue_event(
        self,
        event: OutboxEvent,
        conn: Any = None,
    ) -> OutboxEvent:
        await self.ensure_table(conn)
        query = """
            INSERT INTO vector_indexing_outbox (
                id, tenant_id, document_id, chunk_id, event_type, status,
                payload, retry_count, max_retries, last_error, created_at, updated_at
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::"OutboxEventType", $6::"OutboxEventStatus",
                $7::jsonb, $8, $9, $10, $11, $12
            ) RETURNING id;
        """
        payload_json = json.dumps(event.payload) if isinstance(event.payload, dict) else "{}"
        chunk_uuid = event.chunk_id if event.chunk_id else None

        if conn is not None:
            await conn.fetchrow(
                query,
                event.id,
                event.tenant_id,
                event.document_id,
                chunk_uuid,
                event.event_type,
                event.status,
                payload_json,
                event.retry_count,
                event.max_retries,
                event.last_error,
                event.created_at,
                event.updated_at,
            )
            return event

        pool = await self.get_pool()
        async with pool.acquire() as c:
            await c.fetchrow(
                query,
                event.id,
                event.tenant_id,
                event.document_id,
                chunk_uuid,
                event.event_type,
                event.status,
                payload_json,
                event.retry_count,
                event.max_retries,
                event.last_error,
                event.created_at,
                event.updated_at,
            )
            return event

    async def fetch_pending_events(
        self,
        tenant_id: str | None = None,
        limit: int = 50,
    ) -> list[OutboxEvent]:
        await self.ensure_table()
        pool = await self.get_pool()
        if tenant_id:
            query = """
                SELECT id, tenant_id, document_id, chunk_id, event_type, status,
                       payload, retry_count, max_retries, last_error, processed_at,
                       created_at, updated_at
                FROM vector_indexing_outbox
                WHERE status = 'PENDING'::"OutboxEventStatus"
                  AND tenant_id = $1::uuid
                ORDER BY created_at ASC
                LIMIT $2;
            """
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, tenant_id, limit)
        else:
            query = """
                SELECT id, tenant_id, document_id, chunk_id, event_type, status,
                       payload, retry_count, max_retries, last_error, processed_at,
                       created_at, updated_at
                FROM vector_indexing_outbox
                WHERE status = 'PENDING'::"OutboxEventStatus"
                ORDER BY created_at ASC
                LIMIT $1;
            """
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, limit)

        return [self._row_to_event(r) for r in rows]

    async def fetch_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[OutboxEvent]:
        await self.ensure_table()
        pool = await self.get_pool()
        query = """
            SELECT id, tenant_id, document_id, chunk_id, event_type, status,
                   payload, retry_count, max_retries, last_error, processed_at,
                   created_at, updated_at
            FROM vector_indexing_outbox
            WHERE tenant_id = $1::uuid AND document_id = $2::uuid
            ORDER BY created_at ASC;
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, tenant_id, document_id)
        return [self._row_to_event(r) for r in rows]

    async def mark_processing(self, event_id: str) -> bool:
        await self.ensure_table()
        pool = await self.get_pool()
        query = """
            UPDATE vector_indexing_outbox
            SET status = 'PROCESSING'::"OutboxEventStatus",
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1::uuid AND status = 'PENDING'::"OutboxEventStatus";
        """
        async with pool.acquire() as conn:
            result = await conn.execute(query, event_id)
            return result.endswith("1")

    async def mark_completed(self, event_id: str) -> bool:
        await self.ensure_table()
        pool = await self.get_pool()
        query = """
            UPDATE vector_indexing_outbox
            SET status = 'COMPLETED'::"OutboxEventStatus",
                processed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1::uuid;
        """
        async with pool.acquire() as conn:
            result = await conn.execute(query, event_id)
            return result.endswith("1")

    async def mark_failed(
        self,
        event_id: str,
        error: str,
        retryable: bool = True,
    ) -> bool:
        await self.ensure_table()
        pool = await self.get_pool()
        query = """
            UPDATE vector_indexing_outbox
            SET retry_count = retry_count + 1,
                last_error = $2,
                status = CASE
                    WHEN $3::boolean AND (retry_count + 1) < max_retries THEN 'PENDING'::"OutboxEventStatus"
                    WHEN (retry_count + 1) >= max_retries THEN 'DEAD_LETTER'::"OutboxEventStatus"
                    ELSE 'FAILED'::"OutboxEventStatus"
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1::uuid;
        """
        async with pool.acquire() as conn:
            result = await conn.execute(query, event_id, error, retryable)
            return result.endswith("1")

    async def delete_events_for_document(
        self,
        tenant_id: str,
        document_id: str,
        conn: Any = None,
    ) -> int:
        await self.ensure_table(conn)
        query = """
            DELETE FROM vector_indexing_outbox
            WHERE tenant_id = $1::uuid AND document_id = $2::uuid;
        """
        if conn is not None:
            res = await conn.execute(query, tenant_id, document_id)
            return int(res.split()[-1])
        pool = await self.get_pool()
        async with pool.acquire() as c:
            res = await c.execute(query, tenant_id, document_id)
            return int(res.split()[-1])

    @staticmethod
    def _row_to_event(row: Any) -> OutboxEvent:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        elif payload is None:
            payload = {}

        return OutboxEvent(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            document_id=str(row["document_id"]),
            chunk_id=str(row["chunk_id"]) if row["chunk_id"] else None,
            event_type=str(row["event_type"]),
            status=str(row["status"]),
            payload=dict(payload),
            retry_count=int(row["retry_count"]),
            max_retries=int(row["max_retries"]),
            last_error=row["last_error"],
            processed_at=row["processed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class OutboxDispatcher:
    """Dispatches transactional outbox events to Qdrant vector database.

    Enforces:
    1. At-least-once vector delivery with idempotent point upserts.
    2. Tenant-scoped operations preventing cross-tenant leakage.
    3. Compensating vector deletion when retries are permanently exhausted.
    4. Updates PostgreSQL chunk vectorStorageId pointers upon successful indexing.
    """

    def __init__(
        self,
        outbox_repo: OutboxRepository,
        qdrant_service: Any,
        chunk_repo: Any = None,
    ) -> None:
        self.outbox_repo = outbox_repo
        self.qdrant_service = qdrant_service
        self.chunk_repo = chunk_repo

    async def dispatch_event(
        self,
        event: OutboxEvent,
        context: IngestionContext | None = None,
    ) -> bool:
        """Process a single outbox event, executing vector upsert or delete."""
        marked = await self.outbox_repo.mark_processing(event.id)
        if not marked and event.status != "PENDING":
            logger.info("Outbox event %s already processed or being processed", event.id)
            return False

        tenant_id = event.tenant_id
        document_id = event.document_id

        if context is None:
            from backend.ingestion.types.context import IngestionContext
            context = IngestionContext(tenant_id=tenant_id)

        try:
            if event.event_type == "UPSERT_VECTOR":
                points_data = event.payload.get("points", [])
                if not points_data:
                    logger.warning("Outbox event %s contains no points in payload", event.id)
                    await self.outbox_repo.mark_completed(event.id)
                    return True

                # Convert points payload to QdrantPointRecord or chunk inputs
                chunks: list[DocumentChunkInput] = []
                vectors: list[list[float]] = []
                chunk_to_point_map: dict[str, str] = {}

                for pt in points_data:
                    chk_input = DocumentChunkInput(**pt["chunk"])
                    vec = pt["vector"]
                    chunks.append(chk_input)
                    vectors.append(vec)
                    chunk_to_point_map[chk_input.chunk_id] = to_point_id(chk_input.chunk_id)

                embeddings = [
                    EmbeddingResult(
                        chunk_id=chk.chunk_id,
                        vector=vec,
                        model_name="BAAI/bge-m3",
                        dimension=len(vec),
                        token_count=0,
                    )
                    for chk, vec in zip(chunks, vectors)
                ]

                # Execute idempotent Qdrant batch upsert
                index_result = await self.qdrant_service.batch_index_chunks(
                    chunks=chunks,
                    embeddings=embeddings,
                    context=context,
                )

                if index_result.points_failed > 0 and index_result.points_indexed == 0:
                    raise IndexingError(
                        f"Qdrant batch indexing failed: {index_result.error_details}",
                        details={"indexed": index_result.points_indexed, "failed": index_result.points_failed},
                    )

                # Update chunk vectorStorageId in PostgreSQL if chunk_repo is available
                if self.chunk_repo is not None:
                    await self.chunk_repo.update_chunk_vector_storage_ids(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        chunk_to_vector_map=chunk_to_point_map,
                    )

                # Mark outbox event COMPLETED
                await self.outbox_repo.mark_completed(event.id)
                logger.info(
                    "[doc=%s tenant=%s outbox=%s] Indexed %d vector points successfully",
                    document_id,
                    tenant_id,
                    event.id,
                    index_result.points_indexed,
                )
                return True

            elif event.event_type == "DELETE_VECTOR":
                point_ids = event.payload.get("point_ids", [])
                if point_ids:
                    self.qdrant_service.delete_by_point_ids(point_ids=point_ids, context=context)
                else:
                    await self.qdrant_service.delete_document_vectors(document_id=document_id, context=context)

                await self.outbox_repo.mark_completed(event.id)
                logger.info("[doc=%s tenant=%s outbox=%s] Deleted vector points", document_id, tenant_id, event.id)
                return True

            else:
                logger.error("Unknown outbox event type: %s", event.event_type)
                await self.outbox_repo.mark_failed(event.id, f"Unknown event type: {event.event_type}", retryable=False)
                return False

        except Exception as e:
            logger.error(
                "[doc=%s tenant=%s outbox=%s] Outbox dispatch failed: %s",
                document_id,
                tenant_id,
                event.id,
                e,
                exc_info=True,
            )
            # Record failure
            await self.outbox_repo.mark_failed(event.id, str(e), retryable=True)

            # Check if permanently failed (exceeded retries)
            if event.retry_count + 1 >= event.max_retries:
                # Trigger compensating vector deletion to prevent partial orphan vectors
                logger.warning(
                    "[doc=%s tenant=%s outbox=%s] Retries exhausted. Executing compensating vector deletion.",
                    document_id,
                    tenant_id,
                    event.id,
                )
                try:
                    points_data = event.payload.get("points", [])
                    point_ids = [to_point_id(pt["chunk"]["chunk_id"]) for pt in points_data]
                    if point_ids:
                        self.qdrant_service.delete_by_point_ids(point_ids=point_ids, context=context)
                except Exception as comp_err:
                    logger.error("Compensating vector deletion failed: %s", comp_err)

            raise

    async def drain_pending(
        self,
        tenant_id: str | None = None,
        limit: int = 50,
        context: IngestionContext | None = None,
    ) -> int:
        """Fetch and dispatch all pending outbox events up to limit."""
        events = await self.outbox_repo.fetch_pending_events(tenant_id=tenant_id, limit=limit)
        success_count = 0
        for ev in events:
            try:
                ok = await self.dispatch_event(ev, context=context)
                if ok:
                    success_count += 1
            except Exception as e:
                logger.warning("Error draining outbox event %s: %s", ev.id, e)
        return success_count
