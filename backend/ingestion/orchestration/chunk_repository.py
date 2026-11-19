"""Relational repository layer for document chunks and transactional atomic persistence."""

from __future__ import annotations

import abc
import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Sequence
import uuid

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.orchestration.outbox import OutboxEvent, OutboxRepository
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.coordinates import ChunkLocator
from backend.ingestion.types.metadata import AccessControlPayload

logger = logging.getLogger(__name__)


class ChunkRepository(abc.ABC):
    """Abstract contract for document chunk persistence, atomic transactions, and queries."""

    @abc.abstractmethod
    async def save_chunks_and_outbox(
        self,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[DocumentChunkInput],
        outbox_event: OutboxEvent,
    ) -> tuple[int, OutboxEvent]:
        """Atomically persist chunks and an outbox event in the same database transaction.

        Guarantees:
        - If chunk insertion fails, outbox event is NOT enqueued.
        - If outbox insertion fails, chunks are NOT persisted.
        - PostgreSQL remains the authoritative source of truth.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def find_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[DocumentChunkInput]:
        """Retrieve all chunks belonging to a document under tenant scope."""
        raise NotImplementedError

    @abc.abstractmethod
    async def find_chunk_ids_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[str]:
        """Retrieve ordered chunk IDs for a document."""
        raise NotImplementedError

    @abc.abstractmethod
    async def count_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> int:
        """Count active chunks for a document."""
        raise NotImplementedError

    @abc.abstractmethod
    async def update_chunk_vector_storage_ids(
        self,
        tenant_id: str,
        document_id: str,
        chunk_to_vector_map: dict[str, str],
    ) -> int:
        """Update vector_storage_id pointers on chunk records after successful Qdrant indexing."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> int:
        """Delete all chunks for a document (used in compensation or rollback)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def find_all_chunks_for_tenant(
        self,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """Retrieve chunk inventory for a tenant to support cross-storage reconciliation."""
        raise NotImplementedError


class InMemoryChunkRepository(ChunkRepository):
    """Thread-safe, asyncio-safe in-memory simulation of document_chunks table."""

    def __init__(self, outbox_repo: OutboxRepository | None = None) -> None:
        self._lock = asyncio.Lock()
        # chunk_id -> dict
        self._chunks: dict[str, dict[str, Any]] = {}
        self.outbox_repo = outbox_repo

    async def save_chunks_and_outbox(
        self,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[DocumentChunkInput],
        outbox_event: OutboxEvent,
    ) -> tuple[int, OutboxEvent]:
        async with self._lock:
            # Atomic commit simulation
            saved_count = 0
            for chunk in chunks:
                record = {
                    "id": chunk.chunk_id,
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "document_version_id": chunk.document_version_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "content_hash": chunk.content_hash,
                    "token_count": chunk.token_count,
                    "embedding_id": chunk.embedding_id,
                    "vector_storage_id": chunk.vector_storage_id,
                    "metadata": dict(chunk.metadata),
                    "page_number": chunk.locator.page_number,
                    "section_heading": chunk.locator.section_heading,
                    "sheet_name": chunk.locator.sheet_name,
                    "row_range": chunk.locator.row_range,
                    "char_start": chunk.locator.char_start,
                    "char_end": chunk.locator.char_end,
                    "acl": chunk.acl.model_dump(),
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
                self._chunks[chunk.chunk_id] = record
                saved_count += 1

            if self.outbox_repo is not None:
                await self.outbox_repo.enqueue_event(outbox_event)

            return saved_count, outbox_event

    async def find_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[DocumentChunkInput]:
        async with self._lock:
            results = []
            for r in self._chunks.values():
                if r["tenant_id"] == tenant_id and r["document_id"] == document_id:
                    chk = self._record_to_chunk_input(r)
                    results.append(chk)
            results.sort(key=lambda x: x.chunk_index)
            return results

    async def find_chunk_ids_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[str]:
        async with self._lock:
            chks = [
                r for r in self._chunks.values()
                if r["tenant_id"] == tenant_id and r["document_id"] == document_id
            ]
            chks.sort(key=lambda x: x["chunk_index"])
            return [r["id"] for r in chks]

    async def count_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> int:
        async with self._lock:
            return sum(
                1 for r in self._chunks.values()
                if r["tenant_id"] == tenant_id and r["document_id"] == document_id
            )

    async def update_chunk_vector_storage_ids(
        self,
        tenant_id: str,
        document_id: str,
        chunk_to_vector_map: dict[str, str],
    ) -> int:
        async with self._lock:
            updated = 0
            for chunk_id, vector_id in chunk_to_vector_map.items():
                r = self._chunks.get(chunk_id)
                if r and r["tenant_id"] == tenant_id and r["document_id"] == document_id:
                    r["vector_storage_id"] = vector_id
                    r["updated_at"] = datetime.now(timezone.utc)
                    updated += 1
            return updated

    async def delete_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> int:
        async with self._lock:
            to_delete = [
                cid for cid, r in self._chunks.items()
                if r["tenant_id"] == tenant_id and r["document_id"] == document_id
            ]
            for cid in to_delete:
                del self._chunks[cid]
            return len(to_delete)

    async def find_all_chunks_for_tenant(
        self,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                dict(r) for r in self._chunks.values()
                if r["tenant_id"] == tenant_id
            ]

    @staticmethod
    def _record_to_chunk_input(r: dict[str, Any]) -> DocumentChunkInput:
        locator = ChunkLocator(
            page_number=r.get("page_number"),
            section_heading=r.get("section_heading"),
            sheet_name=r.get("sheet_name"),
            row_range=r.get("row_range"),
            char_start=r.get("char_start"),
            char_end=r.get("char_end"),
        )
        acl_data = r.get("acl", {})
        acl = AccessControlPayload(
            tenant_id=r["tenant_id"],
            roles=set(acl_data.get("roles", [])),
            departments=set(acl_data.get("departments", [])),
            users=set(acl_data.get("users", [])),
        )
        return DocumentChunkInput(
            chunk_id=r["id"],
            tenant_id=r["tenant_id"],
            document_id=r["document_id"],
            document_version_id=r.get("document_version_id"),
            chunk_index=r["chunk_index"],
            content=r["content"],
            content_hash=r["content_hash"],
            token_count=r["token_count"],
            embedding_id=r.get("embedding_id"),
            vector_storage_id=r.get("vector_storage_id"),
            locator=locator,
            acl=acl,
            metadata=dict(r.get("metadata", {})),
        )


class PostgresChunkRepository(ChunkRepository):
    """PostgreSQL 16+ implementation of ChunkRepository using asyncpg."""

    def __init__(
        self,
        connection_url: str | None = None,
        pool: Any = None,
        outbox_repo: OutboxRepository | None = None,
    ) -> None:
        settings = get_ingestion_settings()
        self.connection_url = connection_url or settings.database_url
        self._pool = pool
        self._pool_lock = asyncio.Lock()
        self.outbox_repo = outbox_repo

    async def get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                import asyncpg
                clean_url = self.connection_url.split("?")[0]
                self._pool = await asyncpg.create_pool(dsn=clean_url, min_size=1, max_size=10)
        return self._pool

    async def save_chunks_and_outbox(
        self,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[DocumentChunkInput],
        outbox_event: OutboxEvent,
    ) -> tuple[int, OutboxEvent]:
        pool = await self.get_pool()
        chunk_query = """
            INSERT INTO document_chunks (
                id, tenant_id, document_id, document_version_id, chunk_index,
                content, content_hash, token_count, embedding_id, vector_storage_id,
                metadata, page_number, section_heading, sheet_name, row_range,
                char_start, char_end, created_at, updated_at
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5,
                $6, $7, $8, $9, $10,
                $11::jsonb, $12, $13, $14, $15,
                $16, $17, $18, $19
            );
        """
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Insert chunks
                now = datetime.now(timezone.utc)
                saved_count = 0
                for chk in chunks:
                    meta_dict = dict(chk.metadata)
                    meta_dict["acl"] = chk.acl.model_dump()
                    meta_json = json.dumps(meta_dict)

                    await conn.execute(
                        chunk_query,
                        chk.chunk_id,
                        tenant_id,
                        document_id,
                        chk.document_version_id,
                        chk.chunk_index,
                        chk.content,
                        chk.content_hash,
                        chk.token_count,
                        chk.embedding_id,
                        chk.vector_storage_id,
                        meta_json,
                        chk.locator.page_number,
                        chk.locator.section_heading,
                        chk.locator.sheet_name,
                        chk.locator.row_range,
                        chk.locator.char_start,
                        chk.locator.char_end,
                        now,
                        now,
                    )
                    saved_count += 1

                # 2. Insert outbox event in the same transaction
                if self.outbox_repo is not None:
                    await self.outbox_repo.enqueue_event(outbox_event, conn=conn)

                return saved_count, outbox_event

    async def find_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[DocumentChunkInput]:
        pool = await self.get_pool()
        query = """
            SELECT id, tenant_id, document_id, document_version_id, chunk_index,
                   content, content_hash, token_count, embedding_id, vector_storage_id,
                   metadata, page_number, section_heading, sheet_name, row_range,
                   char_start, char_end
            FROM document_chunks
            WHERE tenant_id = $1::uuid AND document_id = $2::uuid
            ORDER BY chunk_index ASC;
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, tenant_id, document_id)
            return [self._row_to_chunk_input(r) for r in rows]

    async def find_chunk_ids_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> list[str]:
        pool = await self.get_pool()
        query = """
            SELECT id FROM document_chunks
            WHERE tenant_id = $1::uuid AND document_id = $2::uuid
            ORDER BY chunk_index ASC;
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, tenant_id, document_id)
            return [str(r["id"]) for r in rows]

    async def count_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> int:
        pool = await self.get_pool()
        query = """
            SELECT COUNT(*) FROM document_chunks
            WHERE tenant_id = $1::uuid AND document_id = $2::uuid;
        """
        async with pool.acquire() as conn:
            return await conn.fetchval(query, tenant_id, document_id)

    async def update_chunk_vector_storage_ids(
        self,
        tenant_id: str,
        document_id: str,
        chunk_to_vector_map: dict[str, str],
    ) -> int:
        pool = await self.get_pool()
        query = """
            UPDATE document_chunks
            SET vector_storage_id = $3,
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = $1::uuid AND id = $2::uuid;
        """
        updated = 0
        async with pool.acquire() as conn:
            for chunk_id, vector_id in chunk_to_vector_map.items():
                res = await conn.execute(query, tenant_id, chunk_id, vector_id)
                if res.endswith("1"):
                    updated += 1
        return updated

    async def delete_chunks_by_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> int:
        pool = await self.get_pool()
        query = """
            DELETE FROM document_chunks
            WHERE tenant_id = $1::uuid AND document_id = $2::uuid;
        """
        async with pool.acquire() as conn:
            res = await conn.execute(query, tenant_id, document_id)
            return int(res.split()[-1])

    async def find_all_chunks_for_tenant(
        self,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        pool = await self.get_pool()
        query = """
            SELECT id, tenant_id, document_id, chunk_index, content_hash, vector_storage_id
            FROM document_chunks
            WHERE tenant_id = $1::uuid;
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, tenant_id)
            return [
                {
                    "id": str(r["id"]),
                    "tenant_id": str(r["tenant_id"]),
                    "document_id": str(r["document_id"]),
                    "chunk_index": r["chunk_index"],
                    "content_hash": r["content_hash"],
                    "vector_storage_id": r["vector_storage_id"],
                }
                for r in rows
            ]

    @staticmethod
    def _row_to_chunk_input(row: Any) -> DocumentChunkInput:
        meta = row["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        elif meta is None:
            meta = {}

        acl_data = meta.get("acl", {})
        acl = AccessControlPayload(
            tenant_id=str(row["tenant_id"]),
            roles=set(acl_data.get("roles", [])),
            departments=set(acl_data.get("departments", [])),
            users=set(acl_data.get("users", [])),
        )

        locator = ChunkLocator(
            page_number=row["page_number"],
            section_heading=row["section_heading"],
            sheet_name=row["sheet_name"],
            row_range=row["row_range"],
            char_start=row["char_start"],
            char_end=row["char_end"],
        )

        return DocumentChunkInput(
            chunk_id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            document_id=str(row["document_id"]),
            document_version_id=str(row["document_version_id"]) if row["document_version_id"] else None,
            chunk_index=row["chunk_index"],
            content=row["content"],
            content_hash=row["content_hash"],
            token_count=row["token_count"],
            embedding_id=row["embedding_id"],
            vector_storage_id=row["vector_storage_id"],
            locator=locator,
            acl=acl,
            metadata=dict(meta),
        )
