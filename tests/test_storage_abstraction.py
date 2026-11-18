"""Unit tests for Storage Abstraction, Local/S3 backends, and Zero-Trust Tenant Isolation."""

import asyncio
import io
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.ingestion.config import IngestionSettings
from backend.ingestion.exceptions import (
    EmptyFileError,
    FileSignatureMismatchError,
    PathTraversalError,
    StorageAccessDeniedError,
    StorageConfigurationError,
    StorageFileNotFoundError,
    TenantStorageViolationError,
    ValidationError,
)
from backend.ingestion.storage import (
    LocalStorageBackend,
    S3StorageBackend,
    StorageService,
    get_storage_backend,
    get_storage_service,
)
from backend.ingestion.types.context import IngestionContext
from tests.test_file_validation import (
    create_valid_csv_bytes,
    create_valid_docx_bytes,
    create_valid_pdf_bytes,
    create_valid_xlsx_bytes,
)


@pytest.fixture
def temp_storage_dir():
    """Create and tear down a temporary local directory for storage tests."""
    temp_dir = tempfile.mkdtemp(prefix="agentgraph_storage_test_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


def create_tenant_context(
    tenant_id: str = "tenant_alpha",
    document_id: str = "doc_11111111_1111_1111_1111_111111111111",
    version_id: str = "v1",
) -> IngestionContext:
    """Helper to generate tenant context."""
    return IngestionContext(
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        roles=["HR_ADMIN"],
    )


# ==========================================
# TEST SUITE: LOCAL STORAGE BACKEND
# ==========================================

class TestLocalStorageBackend:
    """Verify local filesystem persistence, path confinement, and atomic writes."""

    def test_put_get_delete_lifecycle(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)
            key = "tenants/tenant_alpha/documents/doc1/v1/handbook.pdf"
            data = create_valid_pdf_bytes()

            # 1. Put
            uri = await backend.put(key, data, content_type="application/pdf")
            assert uri.startswith("file://")
            assert Path(uri.replace("file://", "")).exists()

            # 2. Exists
            assert await backend.exists(key) is True

            # 3. Get
            retrieved = await backend.get(key)
            assert retrieved == data

            # 4. Metadata
            meta = await backend.get_raw_metadata(key)
            assert meta["size_bytes"] == len(data)
            assert meta["driver"] == "local_filesystem"

            # 5. Delete
            assert await backend.delete(key) is True
            assert await backend.exists(key) is False

        asyncio.run(_run())

    def test_get_nonexistent_file_raises(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)
            with pytest.raises(StorageFileNotFoundError, match="File not found"):
                await backend.get("nonexistent/key.pdf")

        asyncio.run(_run())

    def test_path_confinement_blocks_traversal(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)

            # Attempting directory traversal to escape base_dir
            with pytest.raises(PathTraversalError, match="Path traversal detected"):
                await backend.put("../../escaped_file.pdf", b"malicious content")

            with pytest.raises(PathTraversalError, match="Path traversal detected"):
                await backend.get("../../etc/passwd")

            with pytest.raises(PathTraversalError, match="Path traversal detected"):
                await backend.exists("../secret.txt")

        asyncio.run(_run())


# ==========================================
# TEST SUITE: STORAGE SERVICE & TENANT ISOLATION
# ==========================================

class TestStorageServiceTenantIsolation:
    """Verify multi-tenant boundary enforcement, access controls, and validation integration."""

    def test_save_and_retrieve_valid_document(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)
            service = StorageService(backend=backend)
            ctx = create_tenant_context(tenant_id="tenant_finance")

            docx_bytes = create_valid_docx_bytes()
            stored_meta = await service.save_file(
                content=docx_bytes,
                filename="Financial_Policy.docx",
                context=ctx,
                validate=True,
            )

            assert stored_meta.tenant_id == "tenant_finance"
            assert stored_meta.document_id == ctx.document_id
            assert stored_meta.version_id == ctx.version_id
            assert stored_meta.filename == "Financial_Policy.docx"
            assert stored_meta.file_size_bytes == len(docx_bytes)
            assert len(stored_meta.content_hash) == 64
            assert stored_meta.mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            assert f"tenants/{ctx.tenant_id}/documents/{ctx.document_id}" in stored_meta.storage_key

            # Retrieve file
            content = await service.get_file(stored_meta.storage_key, ctx)
            assert content == docx_bytes

            # Check existence
            assert await service.file_exists(stored_meta.storage_key, ctx) is True

            # Check metadata
            retrieved_meta = await service.get_file_metadata(stored_meta.storage_key, ctx)
            assert retrieved_meta.content_hash == stored_meta.content_hash
            assert retrieved_meta.file_size_bytes == stored_meta.file_size_bytes

        asyncio.run(_run())

    def test_cross_tenant_access_denied(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)
            service = StorageService(backend=backend)

            # Tenant A saves a document
            ctx_a = create_tenant_context(tenant_id="tenant_alpha", document_id="doc_alpha_1")
            pdf_bytes = create_valid_pdf_bytes()
            stored_doc_a = await service.save_file(
                content=pdf_bytes,
                filename="confidential_memo.pdf",
                context=ctx_a,
            )

            # Tenant B attempts to read Tenant A's document
            ctx_b = create_tenant_context(tenant_id="tenant_beta", document_id="doc_beta_2")

            with pytest.raises(TenantStorageViolationError, match="Cross-Tenant Access Violation"):
                await service.get_file(stored_doc_a.storage_key, ctx_b)

            with pytest.raises(TenantStorageViolationError, match="Cross-Tenant Access Violation"):
                await service.get_file_metadata(stored_doc_a.storage_key, ctx_b)

            with pytest.raises(TenantStorageViolationError, match="Cross-Tenant Access Violation"):
                await service.delete_file(stored_doc_a.storage_key, ctx_b)

            with pytest.raises(TenantStorageViolationError, match="Cross-Tenant Access Violation"):
                await service.file_exists(stored_doc_a.storage_key, ctx_b)

        asyncio.run(_run())

    def test_document_id_mismatch_denied(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)
            service = StorageService(backend=backend)

            ctx_1 = create_tenant_context(tenant_id="tenant_corp", document_id="doc_01")
            stored_doc = await service.save_file(
                content=create_valid_csv_bytes(),
                filename="data.csv",
                context=ctx_1,
            )

            # Same tenant, but context requests a different document ID
            ctx_2 = create_tenant_context(tenant_id="tenant_corp", document_id="doc_OTHER")

            with pytest.raises(StorageAccessDeniedError, match="Document ID mismatch"):
                await service.get_file(stored_doc.storage_key, ctx_2)

        asyncio.run(_run())

    def test_path_traversal_in_storage_key_blocked(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)
            service = StorageService(backend=backend)
            ctx = create_tenant_context(tenant_id="tenant_alpha")

            traversal_key = "tenants/tenant_alpha/../../tenant_beta/secret.pdf"
            with pytest.raises(PathTraversalError, match="Path traversal sequence"):
                await service.get_file(traversal_key, ctx)

        asyncio.run(_run())

    def test_validation_prevents_saving_corrupt_file(self, temp_storage_dir: Path) -> None:
        async def _run() -> None:
            backend = LocalStorageBackend(base_dir=temp_storage_dir)
            service = StorageService(backend=backend)
            ctx = create_tenant_context()

            # 1. Empty file rejected
            with pytest.raises(EmptyFileError, match="empty"):
                await service.save_file(b"", "empty.pdf", ctx, validate=True)

            # 2. Executable disguised as PDF rejected
            exe_content = b"MZ\x90\x00executable"
            with pytest.raises(FileSignatureMismatchError):
                await service.save_file(exe_content, "fake.pdf", ctx, validate=True)

        asyncio.run(_run())


# ==========================================
# TEST SUITE: S3 STORAGE BACKEND
# ==========================================

class TestS3StorageBackend:
    """Verify S3 backend configuration, driver naming, and mocked interactions."""

    def test_s3_backend_properties(self) -> None:
        backend = S3StorageBackend(
            bucket_name="my-enterprise-bucket",
            endpoint_url="http://localhost:9000",
            access_key_id="minioadmin",
            secret_access_key="minioadmin",
            region_name="us-east-1",
        )
        assert backend.driver_name == "s3_object_storage"
        assert backend.bucket_name == "my-enterprise-bucket"

    def test_s3_missing_boto3_raises_configuration_error(self) -> None:
        backend = S3StorageBackend(bucket_name="bucket")
        with patch.dict("sys.modules", {"boto3": None}):
            with pytest.raises(StorageConfigurationError, match="boto3"):
                backend._get_client()

    def test_s3_put_get_with_mock(self) -> None:
        async def _run() -> None:
            backend = S3StorageBackend(bucket_name="test-bucket")

            mock_client = MagicMock()
            mock_client.put_object.return_value = {"ETag": '"abcdef"'}
            mock_body = MagicMock()
            mock_body.read.return_value = b"mocked document content"
            mock_client.get_object.return_value = {"Body": mock_body}
            mock_client.head_object.return_value = {"ContentLength": 23, "ETag": '"abcdef"'}
            mock_client.delete_object.return_value = {}

            backend._client = mock_client

            # Put
            uri = await backend.put("tenants/t1/doc.pdf", b"mocked document content", "application/pdf")
            assert uri == "s3://test-bucket/tenants/t1/doc.pdf"
            mock_client.put_object.assert_called_once()

            # Get
            retrieved = await backend.get("tenants/t1/doc.pdf")
            assert retrieved == b"mocked document content"

            # Exists
            assert await backend.exists("tenants/t1/doc.pdf") is True

            # Delete
            assert await backend.delete("tenants/t1/doc.pdf") is True

        asyncio.run(_run())
