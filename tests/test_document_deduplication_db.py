"""Database integration tests for SHA-256 Exact Document Deduplication with live PostgreSQL 16+.

Verifies:
1. PostgresDocumentRepository connecting to live PostgreSQL database.
2. Partial unique index `documents_tenant_active_content_hash_key` enforcement.
3. Race condition collision recovery via `register_document_safely`.
4. Multi-tenant hash coexistence (identical hash across distinct tenants).
5. Soft-deletion behavior in live PostgreSQL.
"""

import asyncio
import hashlib
import uuid
import pytest

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.deduplication import (
    DocumentCreateInput,
    DocumentDeduplicator,
    DuplicateAction,
    DuplicateHandlingPolicy,
    PostgresDocumentRepository,
)
from backend.ingestion.types.context import IngestionContext, IngestionOptions


async def _check_db_accessible() -> bool:
    settings = get_ingestion_settings()
    repo = PostgresDocumentRepository(connection_url=settings.database_url)
    try:
        pool = await repo.get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1;")
        await repo.close()
        return True
    except Exception:
        return False


class TestPostgresDeduplicationIntegration:
    """Live PostgreSQL database integration tests for exact deduplication."""

    @classmethod
    def setup_class(cls):
        """Skip suite if live PostgreSQL database is not reachable."""
        accessible = asyncio.run(_check_db_accessible())
        if not accessible:
            pytest.skip("Live PostgreSQL database is not running or accessible")

    def test_postgres_exact_duplicate_detection(self) -> None:
        async def _run() -> None:
            settings = get_ingestion_settings()
            repo = PostgresDocumentRepository(connection_url=settings.database_url)
            pool = await repo.get_pool()
            deduplicator = DocumentDeduplicator(repository=repo)

            run_id = uuid.uuid4().hex[:8]
            tenant_a = str(uuid.uuid4())
            slug_a = f"test-dedup-a-{run_id}"

            # Create test tenant
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO tenants (id, name, slug, status) VALUES ($1::uuid, $2, $3, 'ACTIVE');",
                    tenant_a, f"Dedup Corp Alpha {run_id}", slug_a,
                )

            try:
                payload = b"Live PostgreSQL deduplication test document body content."
                content_hash = hashlib.sha256(payload).hexdigest()

                context = IngestionContext(
                    tenant_id=tenant_a,
                    document_id=str(uuid.uuid4()),
                    options=IngestionOptions(deduplication_enabled=True),
                )

                # 1. First check: no duplicate
                res1 = await deduplicator.deduplicate_content(
                    content=payload,
                    filename="live_test.pdf",
                    context=context,
                )
                assert res1.is_duplicate is False
                assert res1.action == DuplicateAction.PROCEED

                # 2. Register first document in PostgreSQL
                doc1 = await repo.register_document(
                    DocumentCreateInput(
                        tenant_id=tenant_a,
                        original_filename="live_test.pdf",
                        normalized_filename="live_test.pdf",
                        storage_uri="s3://enterprise-docs/live_test.pdf",
                        file_size_bytes=len(payload),
                        content_hash=content_hash,
                        classification="INTERNAL",
                    )
                )
                assert doc1.document_id is not None
                assert doc1.content_hash == content_hash

                # 3. Second check in Tenant A: duplicate detected
                res2 = await deduplicator.deduplicate_content(
                    content=payload,
                    filename="live_test_copy.pdf",
                    context=context,
                    policy=DuplicateHandlingPolicy.RETURN_EXISTING,
                )
                assert res2.is_duplicate is True
                assert res2.action == DuplicateAction.RETURN_EXISTING
                assert res2.existing_document_id == doc1.document_id

            finally:
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM tenants WHERE id = $1::uuid;", tenant_a)
                await repo.close()

        asyncio.run(_run())

    def test_postgres_multi_tenant_isolation(self) -> None:
        async def _run() -> None:
            settings = get_ingestion_settings()
            repo = PostgresDocumentRepository(connection_url=settings.database_url)
            pool = await repo.get_pool()
            deduplicator = DocumentDeduplicator(repository=repo)

            run_id = uuid.uuid4().hex[:8]
            tenant_a = str(uuid.uuid4())
            tenant_b = str(uuid.uuid4())
            slug_a = f"test-isolation-a-{run_id}"
            slug_b = f"test-isolation-b-{run_id}"

            # Create test tenants
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO tenants (id, name, slug, status)
                    VALUES ($1::uuid, $2, $3, 'ACTIVE'),
                           ($4::uuid, $5, $6, 'ACTIVE');
                    """,
                    tenant_a, f"Isolation Corp Alpha {run_id}", slug_a,
                    tenant_b, f"Isolation Corp Beta {run_id}", slug_b,
                )

            try:
                payload = b"Shared standard policy content across independent tenants."
                content_hash = hashlib.sha256(payload).hexdigest()

                # Ingest in Tenant A
                doc_a = await repo.register_document(
                    DocumentCreateInput(
                        tenant_id=tenant_a,
                        original_filename="shared_policy.pdf",
                        normalized_filename="shared_policy.pdf",
                        storage_uri="s3://tenant-a/shared.pdf",
                        file_size_bytes=len(payload),
                        content_hash=content_hash,
                    )
                )

                # Check in Tenant B: MUST NOT see Tenant A's document
                context_b = IngestionContext(
                    tenant_id=tenant_b,
                    document_id=str(uuid.uuid4()),
                )
                res_b = await deduplicator.deduplicate_content(
                    content=payload,
                    filename="shared_policy.pdf",
                    context=context_b,
                )
                assert res_b.is_duplicate is False
                assert res_b.action == DuplicateAction.PROCEED

                # Register in Tenant B: PostgreSQL allows identical hash in distinct tenant
                doc_b = await repo.register_document(
                    DocumentCreateInput(
                        tenant_id=tenant_b,
                        original_filename="shared_policy.pdf",
                        normalized_filename="shared_policy.pdf",
                        storage_uri="s3://tenant-b/shared.pdf",
                        file_size_bytes=len(payload),
                        content_hash=content_hash,
                    )
                )
                assert doc_b.document_id != doc_a.document_id
                assert doc_b.tenant_id == tenant_b

            finally:
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM tenants WHERE id IN ($1::uuid, $2::uuid);", tenant_a, tenant_b)
                await repo.close()

        asyncio.run(_run())

    def test_postgres_soft_delete_and_reupload(self) -> None:
        async def _run() -> None:
            settings = get_ingestion_settings()
            repo = PostgresDocumentRepository(connection_url=settings.database_url)
            pool = await repo.get_pool()
            deduplicator = DocumentDeduplicator(repository=repo)

            run_id = uuid.uuid4().hex[:8]
            tenant_a = str(uuid.uuid4())
            slug_a = f"test-softdel-{run_id}"

            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO tenants (id, name, slug, status) VALUES ($1::uuid, $2, $3, 'ACTIVE');",
                    tenant_a, f"SoftDel Corp {run_id}", slug_a,
                )

            try:
                payload = b"Temporary document that will be soft-deleted and re-uploaded."
                content_hash = hashlib.sha256(payload).hexdigest()

                # Register document
                doc = await repo.register_document(
                    DocumentCreateInput(
                        tenant_id=tenant_a,
                        original_filename="temp_doc.pdf",
                        normalized_filename="temp_doc.pdf",
                        storage_uri="s3://tenant-a/temp.pdf",
                        file_size_bytes=len(payload),
                        content_hash=content_hash,
                    )
                )

                # Soft-delete in PostgreSQL
                deleted = await repo.soft_delete_document(tenant_a, doc.document_id)
                assert deleted is True

                # Check deduplication: soft-deleted document does not block re-upload
                context = IngestionContext(
                    tenant_id=tenant_a,
                    document_id=str(uuid.uuid4()),
                )
                res = await deduplicator.deduplicate_content(
                    content=payload,
                    filename="temp_doc_reupload.pdf",
                    context=context,
                )
                assert res.is_duplicate is False
                assert res.action == DuplicateAction.PROCEED

                # Re-upload document with identical hash in PostgreSQL succeeds
                reuploaded_doc = await repo.register_document(
                    DocumentCreateInput(
                        tenant_id=tenant_a,
                        original_filename="temp_doc_reupload.pdf",
                        normalized_filename="temp_doc_reupload.pdf",
                        storage_uri="s3://tenant-a/temp_reupload.pdf",
                        file_size_bytes=len(payload),
                        content_hash=content_hash,
                    )
                )
                assert reuploaded_doc.document_id != doc.document_id

            finally:
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM tenants WHERE id = $1::uuid;", tenant_a)
                await repo.close()

        asyncio.run(_run())

    def test_postgres_race_safe_registration(self) -> None:
        async def _run() -> None:
            settings = get_ingestion_settings()
            repo = PostgresDocumentRepository(connection_url=settings.database_url)
            pool = await repo.get_pool()
            deduplicator = DocumentDeduplicator(repository=repo)

            run_id = uuid.uuid4().hex[:8]
            tenant_a = str(uuid.uuid4())
            slug_a = f"test-racesafe-{run_id}"

            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO tenants (id, name, slug, status) VALUES ($1::uuid, $2, $3, 'ACTIVE');",
                    tenant_a, f"RaceSafe Corp {run_id}", slug_a,
                )

            try:
                payload = b"Concurrent race safety payload for live PostgreSQL unique index."
                content_hash = hashlib.sha256(payload).hexdigest()

                # Simulate concurrent registration calls for the same tenant and hash
                doc_input_1 = DocumentCreateInput(
                    tenant_id=tenant_a,
                    original_filename="race_doc_1.pdf",
                    normalized_filename="race_doc_1.pdf",
                    storage_uri="s3://tenant-a/race1.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )
                doc_input_2 = DocumentCreateInput(
                    tenant_id=tenant_a,
                    original_filename="race_doc_2.pdf",
                    normalized_filename="race_doc_2.pdf",
                    storage_uri="s3://tenant-a/race2.pdf",
                    file_size_bytes=len(payload),
                    content_hash=content_hash,
                )

                # First registration
                summary1, is_new1 = await deduplicator.register_document_safely(doc_input_1)
                assert is_new1 is True

                # Second registration with identical hash: triggers unique constraint collision and recovers
                summary2, is_new2 = await deduplicator.register_document_safely(doc_input_2)
                assert is_new2 is False
                assert summary2.document_id == summary1.document_id

            finally:
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM tenants WHERE id = $1::uuid;", tenant_a)
                await repo.close()

        asyncio.run(_run())
