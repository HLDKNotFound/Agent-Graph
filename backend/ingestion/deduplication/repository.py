"""Database repository layer for document deduplication and relational persistence."""

import abc
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.deduplication.models import (
    DocumentCreateInput,
    DocumentVersionCreateInput,
    ExistingDocumentSummary,
)
from backend.ingestion.exceptions import (
    ConcurrentDuplicateError,
    DeduplicationError,
)

logger = logging.getLogger(__name__)


class DocumentRepository(abc.ABC):
    """Abstract contract for document persistence and hash deduplication lookup."""

    @abc.abstractmethod
    async def find_active_by_hash(
        self,
        tenant_id: str,
        content_hash: str,
    ) -> ExistingDocumentSummary | None:
        """Find non-soft-deleted document with matching content_hash strictly within tenant."""
        raise NotImplementedError

    @abc.abstractmethod
    async def find_by_id(
        self,
        tenant_id: str,
        document_id: str,
    ) -> ExistingDocumentSummary | None:
        """Find document by UUID within tenant boundary."""
        raise NotImplementedError

    @abc.abstractmethod
    async def register_document(
        self,
        doc_input: DocumentCreateInput,
    ) -> ExistingDocumentSummary:
        """Atomically insert a new document record under tenant scope with race safety."""
        raise NotImplementedError

    @abc.abstractmethod
    async def create_version(
        self,
        version_input: DocumentVersionCreateInput,
    ) -> ExistingDocumentSummary:
        """Create an incremental version under an existing document."""
        raise NotImplementedError

    @abc.abstractmethod
    async def soft_delete_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> bool:
        """Soft-delete document (sets deleted_at = NOW())."""
        raise NotImplementedError

    @abc.abstractmethod
    async def count_active_documents(self, tenant_id: str) -> int:
        """Count active (non-soft-deleted) documents for a tenant."""
        raise NotImplementedError


class PostgresDocumentRepository(DocumentRepository):
    """PostgreSQL 16+ implementation of DocumentRepository using asyncpg."""

    def __init__(
        self,
        connection_url: str | None = None,
        pool: Any = None,
    ) -> None:
        settings = get_ingestion_settings()
        self.connection_url = connection_url or settings.database_url
        self._pool = pool
        self._pool_lock = asyncio.Lock()

    async def get_pool(self) -> Any:
        """Provide or initialize shared asyncpg connection pool."""
        if self._pool is not None:
            return self._pool

        async with self._pool_lock:
            if self._pool is None:
                import asyncpg
                # Strip Prisma query params like ?schema=public for asyncpg if present
                clean_url = self.connection_url.split("?")[0]
                self._pool = await asyncpg.create_pool(
                    dsn=clean_url,
                    min_size=1,
                    max_size=10,
                )
        return self._pool

    async def close(self) -> None:
        """Close database connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def find_active_by_hash(
        self,
        tenant_id: str,
        content_hash: str,
    ) -> ExistingDocumentSummary | None:
        pool = await self.get_pool()
        query = """
            SELECT id, tenant_id, department_id, uploaded_by_id, current_version_id,
                   original_filename, normalized_filename, storage_uri, mime_type,
                   file_size_bytes, content_hash, classification, status, metadata,
                   created_at, updated_at
            FROM documents
            WHERE tenant_id = $1::uuid
              AND content_hash = $2
              AND deleted_at IS NULL
            LIMIT 1;
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, tenant_id, content_hash)
            if not row:
                return None
            return self._row_to_summary(row)

    async def find_by_id(
        self,
        tenant_id: str,
        document_id: str,
    ) -> ExistingDocumentSummary | None:
        pool = await self.get_pool()
        query = """
            SELECT id, tenant_id, department_id, uploaded_by_id, current_version_id,
                   original_filename, normalized_filename, storage_uri, mime_type,
                   file_size_bytes, content_hash, classification, status, metadata,
                   created_at, updated_at
            FROM documents
            WHERE tenant_id = $1::uuid
              AND id = $2::uuid
              AND deleted_at IS NULL
            LIMIT 1;
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, tenant_id, document_id)
            if not row:
                return None
            return self._row_to_summary(row)

    async def register_document(
        self,
        doc_input: DocumentCreateInput,
    ) -> ExistingDocumentSummary:
        pool = await self.get_pool()
        import asyncpg

        query = """
            INSERT INTO documents (
                id, tenant_id, department_id, uploaded_by_id,
                original_filename, normalized_filename, storage_uri, mime_type,
                file_size_bytes, content_hash, classification, status, metadata
            ) VALUES (
                COALESCE($1::uuid, gen_random_uuid()), $2::uuid, $3::uuid, $4::uuid,
                $5, $6, $7, $8,
                $9, $10, $11::"DocumentClassification", $12::"DocumentIngestionStatus", $13::jsonb
            ) RETURNING id, tenant_id, department_id, uploaded_by_id, current_version_id,
                        original_filename, normalized_filename, storage_uri, mime_type,
                        file_size_bytes, content_hash, classification, status, metadata,
                        created_at, updated_at;
        """
        doc_id = doc_input.document_id
        meta_json = json.dumps(doc_input.metadata) if isinstance(doc_input.metadata, dict) else "{}"

        async with pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    query,
                    doc_id,
                    doc_input.tenant_id,
                    doc_input.department_id,
                    doc_input.uploaded_by_id,
                    doc_input.original_filename,
                    doc_input.normalized_filename,
                    doc_input.storage_uri,
                    doc_input.mime_type,
                    doc_input.file_size_bytes,
                    doc_input.content_hash,
                    doc_input.classification,
                    doc_input.status,
                    meta_json,
                )
                return self._row_to_summary(row)
            except asyncpg.UniqueViolationError as e:
                # Catch unique index violation (documents_tenant_active_content_hash_key)
                logger.warning(
                    "Concurrent upload collision on content hash %s for tenant %s: %s",
                    doc_input.content_hash,
                    doc_input.tenant_id,
                    e,
                )
                # Re-fetch the winning active document created concurrently
                existing = await self.find_active_by_hash(doc_input.tenant_id, doc_input.content_hash)
                if existing:
                    return existing
                raise ConcurrentDuplicateError(
                    f"Concurrent duplicate insertion conflict on hash {doc_input.content_hash}",
                    tenant_id=doc_input.tenant_id,
                    content_hash=doc_input.content_hash,
                ) from e

    async def create_version(
        self,
        version_input: DocumentVersionCreateInput,
    ) -> ExistingDocumentSummary:
        pool = await self.get_pool()
        meta_json = json.dumps(version_input.metadata) if isinstance(version_input.metadata, dict) else "{}"

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Get next version number
                row_ver = await conn.fetchrow(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1 AS next_ver
                    FROM document_versions
                    WHERE document_id = $1::uuid;
                    """,
                    version_input.document_id,
                )
                next_version = row_ver["next_ver"] if row_ver else 1

                # Insert version snapshot
                insert_ver_query = """
                    INSERT INTO document_versions (
                        id, document_id, tenant_id, created_by_id, version_number,
                        storage_uri, original_filename, mime_type, file_size_bytes,
                        content_hash, status, metadata, is_current
                    ) VALUES (
                        gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid, $4,
                        $5, $6, $7, $8,
                        $9, $10::"DocumentIngestionStatus", $11::jsonb, $12
                    ) RETURNING id;
                """
                ver_row = await conn.fetchrow(
                    insert_ver_query,
                    version_input.document_id,
                    version_input.tenant_id,
                    version_input.created_by_id,
                    next_version,
                    version_input.storage_uri,
                    version_input.original_filename,
                    version_input.mime_type,
                    version_input.file_size_bytes,
                    version_input.content_hash,
                    version_input.status,
                    meta_json,
                    version_input.is_current,
                )

                # Fetch updated document summary (triggers automatically updated parent if is_current)
                doc_row = await conn.fetchrow(
                    """
                    SELECT id, tenant_id, department_id, uploaded_by_id, current_version_id,
                           original_filename, normalized_filename, storage_uri, mime_type,
                           file_size_bytes, content_hash, classification, status, metadata,
                           created_at, updated_at
                    FROM documents
                    WHERE id = $1::uuid AND tenant_id = $2::uuid;
                    """,
                    version_input.document_id,
                    version_input.tenant_id,
                )

                summary = self._row_to_summary(doc_row)
                summary.version_number = next_version
                return summary

    async def soft_delete_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> bool:
        pool = await self.get_pool()
        query = """
            UPDATE documents
            SET deleted_at = CURRENT_TIMESTAMP
            WHERE tenant_id = $1::uuid AND id = $2::uuid AND deleted_at IS NULL;
        """
        async with pool.acquire() as conn:
            result = await conn.execute(query, tenant_id, document_id)
            return result.endswith("1")

    async def count_active_documents(self, tenant_id: str) -> int:
        pool = await self.get_pool()
        query = """
            SELECT COUNT(*) FROM documents
            WHERE tenant_id = $1::uuid AND deleted_at IS NULL;
        """
        async with pool.acquire() as conn:
            return await conn.fetchval(query, tenant_id)

    @staticmethod
    def _row_to_summary(row: Any) -> ExistingDocumentSummary:
        meta = row["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        elif meta is None:
            meta = {}

        return ExistingDocumentSummary(
            document_id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            department_id=str(row["department_id"]) if row["department_id"] else None,
            uploaded_by_id=str(row["uploaded_by_id"]) if row["uploaded_by_id"] else None,
            current_version_id=str(row["current_version_id"]) if row["current_version_id"] else None,
            original_filename=row["original_filename"],
            normalized_filename=row.get("normalized_filename"),
            storage_uri=row.get("storage_uri"),
            mime_type=row.get("mime_type"),
            file_size_bytes=int(row["file_size_bytes"]),
            content_hash=row["content_hash"],
            classification=str(row["classification"]),
            status=str(row["status"]),
            metadata=dict(meta),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class InMemoryDocumentRepository(DocumentRepository):
    """Thread-safe, asyncio-safe in-memory simulation of PostgreSQL tables and unique indexes.

    Faithfully enforces:
    1. Tenant isolation (tenant_id partition).
    2. Partial unique index on (tenant_id, content_hash) WHERE deleted_at IS NULL.
    3. Soft-deletion allows re-uploading identical hash.
    4. Document versioning lineage and sequential version incrementation.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # id -> dict
        self._documents: dict[str, dict[str, Any]] = {}
        # (document_id, version_number) -> dict
        self._versions: dict[tuple[str, int], dict[str, Any]] = {}

    async def find_active_by_hash(
        self,
        tenant_id: str,
        content_hash: str,
    ) -> ExistingDocumentSummary | None:
        async with self._lock:
            for doc in self._documents.values():
                if (
                    doc["tenant_id"] == tenant_id
                    and doc["content_hash"] == content_hash
                    and doc["deleted_at"] is None
                ):
                    return self._to_summary(doc)
            return None

    async def find_by_id(
        self,
        tenant_id: str,
        document_id: str,
    ) -> ExistingDocumentSummary | None:
        async with self._lock:
            doc = self._documents.get(document_id)
            if doc and doc["tenant_id"] == tenant_id and doc["deleted_at"] is None:
                return self._to_summary(doc)
            return None

    async def register_document(
        self,
        doc_input: DocumentCreateInput,
    ) -> ExistingDocumentSummary:
        async with self._lock:
            # Enforce unique index: (tenant_id, content_hash) WHERE deleted_at IS NULL
            for doc in self._documents.values():
                if (
                    doc["tenant_id"] == doc_input.tenant_id
                    and doc["content_hash"] == doc_input.content_hash
                    and doc["deleted_at"] is None
                ):
                    # Collision detected! Return existing active document (race-safe behavior)
                    return self._to_summary(doc)

            doc_id = doc_input.document_id or str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            record = {
                "id": doc_id,
                "tenant_id": doc_input.tenant_id,
                "department_id": doc_input.department_id,
                "uploaded_by_id": doc_input.uploaded_by_id,
                "current_version_id": None,
                "original_filename": doc_input.original_filename,
                "normalized_filename": doc_input.normalized_filename,
                "storage_uri": doc_input.storage_uri,
                "mime_type": doc_input.mime_type,
                "file_size_bytes": doc_input.file_size_bytes,
                "content_hash": doc_input.content_hash,
                "classification": doc_input.classification,
                "status": doc_input.status,
                "metadata": dict(doc_input.metadata),
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            }
            self._documents[doc_id] = record
            return self._to_summary(record)

    async def create_version(
        self,
        version_input: DocumentVersionCreateInput,
    ) -> ExistingDocumentSummary:
        async with self._lock:
            doc = self._documents.get(version_input.document_id)
            if not doc or doc["tenant_id"] != version_input.tenant_id:
                raise DeduplicationError(f"Parent document not found: {version_input.document_id}")

            # Determine next version
            existing_vers = [
                v_num for (d_id, v_num) in self._versions.keys() if d_id == version_input.document_id
            ]
            next_ver = max(existing_vers, default=0) + 1

            version_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            ver_record = {
                "id": version_id,
                "document_id": version_input.document_id,
                "tenant_id": version_input.tenant_id,
                "created_by_id": version_input.created_by_id,
                "version_number": next_ver,
                "storage_uri": version_input.storage_uri,
                "original_filename": version_input.original_filename,
                "mime_type": version_input.mime_type,
                "file_size_bytes": version_input.file_size_bytes,
                "content_hash": version_input.content_hash,
                "status": version_input.status,
                "metadata": dict(version_input.metadata),
                "is_current": version_input.is_current,
                "created_at": now,
                "deleted_at": None,
            }
            self._versions[(version_input.document_id, next_ver)] = ver_record

            if version_input.is_current:
                doc["current_version_id"] = version_id
                doc["original_filename"] = version_input.original_filename
                doc["storage_uri"] = version_input.storage_uri
                doc["mime_type"] = version_input.mime_type
                doc["file_size_bytes"] = version_input.file_size_bytes
                doc["content_hash"] = version_input.content_hash
                doc["status"] = version_input.status
                doc["updated_at"] = now

            summary = self._to_summary(doc)
            summary.version_number = next_ver
            return summary

    async def soft_delete_document(
        self,
        tenant_id: str,
        document_id: str,
    ) -> bool:
        async with self._lock:
            doc = self._documents.get(document_id)
            if doc and doc["tenant_id"] == tenant_id and doc["deleted_at"] is None:
                doc["deleted_at"] = datetime.now(timezone.utc)
                return True
            return False

    async def count_active_documents(self, tenant_id: str) -> int:
        async with self._lock:
            return sum(
                1
                for doc in self._documents.values()
                if doc["tenant_id"] == tenant_id and doc["deleted_at"] is None
            )

    @staticmethod
    def _to_summary(doc: dict[str, Any]) -> ExistingDocumentSummary:
        return ExistingDocumentSummary(
            document_id=doc["id"],
            tenant_id=doc["tenant_id"],
            department_id=doc["department_id"],
            uploaded_by_id=doc["uploaded_by_id"],
            current_version_id=doc["current_version_id"],
            original_filename=doc["original_filename"],
            normalized_filename=doc.get("normalized_filename"),
            storage_uri=doc.get("storage_uri"),
            mime_type=doc.get("mime_type"),
            file_size_bytes=doc["file_size_bytes"],
            content_hash=doc["content_hash"],
            classification=doc["classification"],
            status=doc["status"],
            metadata=dict(doc.get("metadata", {})),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
        )
