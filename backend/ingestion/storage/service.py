"""Tenant-isolated enterprise storage service managing document persistence and lineage."""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.exceptions import (
    PathTraversalError,
    StorageAccessDeniedError,
    StorageError,
    StorageFileNotFoundError,
    TenantStorageViolationError,
    ValidationError,
)
from backend.ingestion.storage.interfaces import StorageBackend
from backend.ingestion.storage.models import StoredFileMetadata
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.validation.interfaces import DocumentValidator
from backend.ingestion.validation.sanitizer import sanitize_filename
from backend.ingestion.validation.validator import DefaultDocumentValidator

# Regex to parse tenant and document components from canonical storage keys
STORAGE_KEY_PATTERN = re.compile(
    r"^tenants/(?P<tenant_id>[a-zA-Z0-9_\-]+)/documents/(?P<document_id>[a-zA-Z0-9_\-]+)/(?P<version_part>[a-zA-Z0-9_\-]+)/(?P<filename>[^/]+)$"
)


class StorageService:
    """High-level document storage coordinator enforcing strict multi-tenant boundary isolation."""

    def __init__(
        self,
        backend: StorageBackend,
        validator: DocumentValidator | None = None,
        settings: IngestionSettings | None = None,
    ) -> None:
        self.backend = backend
        self.validator = validator or DefaultDocumentValidator(settings)
        self.settings = settings or get_ingestion_settings()

    def build_storage_key(
        self,
        tenant_id: str,
        document_id: str,
        filename: str,
        version_id: str | None = None,
    ) -> str:
        """Construct deterministic, tenant-partitioned relative storage key.

        Format: tenants/{tenant_id}/documents/{document_id}/{version_part}/{sanitized_filename}
        """
        clean_name = sanitize_filename(filename, max_length=self.settings.max_filename_length)
        version_part = version_id or "v1"
        return f"tenants/{tenant_id}/documents/{document_id}/{version_part}/{clean_name}"

    def validate_tenant_access(self, storage_key: str, context: IngestionContext) -> None:
        """Enforce zero-trust tenant boundary and path containment before accessing storage.

        Raises:
            PathTraversalError: If key contains traversal patterns.
            TenantStorageViolationError: If storage key belongs to a different tenant.
            StorageAccessDeniedError: If context document_id contradicts key.
        """
        if not storage_key or not isinstance(storage_key, str):
            raise StorageError("Storage key cannot be empty or non-string")

        normalized = storage_key.strip().replace("\\", "/")
        if ".." in normalized or normalized.startswith("/"):
            raise PathTraversalError(
                f"Path traversal sequence detected in storage key: '{storage_key}'",
                details={"storage_key": storage_key},
            )

        match = STORAGE_KEY_PATTERN.match(normalized)
        if match:
            key_tenant = match.group("tenant_id")
            if key_tenant != context.tenant_id:
                raise TenantStorageViolationError(
                    f"Cross-Tenant Access Violation: Requesting tenant '{context.tenant_id}' "
                    f"cannot access document in storage key '{storage_key}' owned by tenant '{key_tenant}'",
                    details={
                        "requesting_tenant_id": context.tenant_id,
                        "key_tenant_id": key_tenant,
                        "storage_key": storage_key,
                    },
                )
            key_doc_id = match.group("document_id")
            if context.document_id and key_doc_id != context.document_id:
                raise StorageAccessDeniedError(
                    f"Document ID mismatch: Storage key belongs to document '{key_doc_id}', "
                    f"but context requests document '{context.document_id}'",
                    details={"context_document_id": context.document_id, "key_doc_id": key_doc_id},
                )
        else:
            # Fallback: ensure key begins with tenants/{tenant_id}/
            expected_prefix = f"tenants/{context.tenant_id}/"
            if not normalized.startswith(expected_prefix):
                raise TenantStorageViolationError(
                    f"Tenant isolation violation: Storage key '{storage_key}' does not reside "
                    f"under tenant partition '{expected_prefix}'",
                    details={
                        "requesting_tenant_id": context.tenant_id,
                        "storage_key": storage_key,
                    },
                )

    def _resolve_bytes(self, content: bytes | BinaryIO | Path) -> bytes:
        """Read content input into contiguous byte stream."""
        if isinstance(content, bytes):
            return content
        if isinstance(content, Path):
            if not content.exists() or not content.is_file():
                raise ValidationError(f"File path does not exist: {content}")
            return content.read_bytes()
        if hasattr(content, "read"):
            data = content.read()
            if isinstance(data, str):
                return data.encode("utf-8")
            return data
        raise ValidationError(f"Unsupported content input type: {type(content)}")

    async def save_file(
        self,
        content: bytes | BinaryIO | Path,
        filename: str,
        context: IngestionContext,
        validate: bool = True,
        mime_type: str | None = None,
    ) -> StoredFileMetadata:
        """Validate and securely store an uploaded document binary under tenant boundary.

        Args:
            content: Raw document bytes, file-like object, or Path.
            filename: Original uploaded filename.
            context: Ingestion execution context specifying authenticated tenant and document.
            validate: Whether to execute pre-storage format, magic bytes, and size validation.
            mime_type: Optional declared MIME type override.

        Returns:
            StoredFileMetadata with canonical URI, SHA-256 hash, and storage coordinates.
        """
        raw_bytes = self._resolve_bytes(content)
        sanitized_name = sanitize_filename(filename, max_length=self.settings.max_filename_length)

        confirmed_mime = mime_type
        if validate:
            if isinstance(self.validator, DefaultDocumentValidator):
                val_result = await self.validator.validate_or_raise(raw_bytes, filename, context)
            else:
                val_result = await self.validator.validate(raw_bytes, filename, context)
                if not val_result.is_valid:
                    error_msg = "; ".join(val_result.errors)
                    raise ValidationError(f"Document validation failed: {error_msg}")
            confirmed_mime = val_result.detected_mime_type or mime_type or "application/octet-stream"

        if not confirmed_mime:
            confirmed_mime = "application/octet-stream"

        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        file_size = len(raw_bytes)

        # Generate tenant storage key
        storage_key = self.build_storage_key(
            tenant_id=context.tenant_id,
            document_id=context.document_id,
            filename=sanitized_name,
            version_id=context.version_id,
        )

        # Enforce tenant isolation check on generated key
        self.validate_tenant_access(storage_key, context)

        # Persist through storage backend
        canonical_uri = await self.backend.put(
            key=storage_key,
            data=raw_bytes,
            content_type=confirmed_mime,
        )

        now = datetime.now(timezone.utc)
        return StoredFileMetadata(
            storage_key=storage_key,
            storage_uri=canonical_uri,
            tenant_id=context.tenant_id,
            document_id=context.document_id,
            version_id=context.version_id,
            filename=sanitized_name,
            file_size_bytes=file_size,
            content_hash=content_hash,
            mime_type=confirmed_mime,
            created_at=now,
            updated_at=now,
            metadata={"driver": self.backend.driver_name},
        )

    async def get_file(self, storage_key: str, context: IngestionContext) -> bytes:
        """Retrieve stored document bytes with mandatory tenant boundary validation."""
        self.validate_tenant_access(storage_key, context)
        return await self.backend.get(storage_key)

    async def delete_file(self, storage_key: str, context: IngestionContext) -> bool:
        """Delete stored document artifact with mandatory tenant boundary validation."""
        self.validate_tenant_access(storage_key, context)
        return await self.backend.delete(storage_key)

    async def file_exists(self, storage_key: str, context: IngestionContext) -> bool:
        """Check whether document artifact exists with mandatory tenant boundary validation."""
        self.validate_tenant_access(storage_key, context)
        return await self.backend.exists(storage_key)

    async def get_file_metadata(
        self,
        storage_key: str,
        context: IngestionContext,
    ) -> StoredFileMetadata:
        """Retrieve file metadata with mandatory tenant boundary validation."""
        self.validate_tenant_access(storage_key, context)
        raw_meta = await self.backend.get_raw_metadata(storage_key)

        match = STORAGE_KEY_PATTERN.match(storage_key)
        filename = match.group("filename") if match else Path(storage_key).name
        version_part = match.group("version_part") if match else None

        # Compute content hash from existing file if not in raw metadata
        file_bytes = await self.backend.get(storage_key)
        content_hash = hashlib.sha256(file_bytes).hexdigest()

        return StoredFileMetadata(
            storage_key=storage_key,
            storage_uri=f"file://{raw_meta.get('path')}" if "path" in raw_meta else f"s3://{raw_meta.get('bucket', '')}/{storage_key}",
            tenant_id=context.tenant_id,
            document_id=context.document_id,
            version_id=context.version_id or version_part,
            filename=filename,
            file_size_bytes=raw_meta.get("size_bytes", len(file_bytes)),
            content_hash=content_hash,
            mime_type=raw_meta.get("content_type", "application/octet-stream"),
            created_at=raw_meta.get("created_at", datetime.now(timezone.utc)),
            updated_at=raw_meta.get("modified_at", datetime.now(timezone.utc)),
            metadata=raw_meta,
        )
