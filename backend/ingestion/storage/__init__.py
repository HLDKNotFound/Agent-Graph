"""Storage subsystem providing storage abstraction, local/S3 backends, and tenant isolation."""

from backend.ingestion.storage.factory import (
    get_storage_backend,
    get_storage_service,
)
from backend.ingestion.storage.interfaces import StorageBackend
from backend.ingestion.storage.local import LocalStorageBackend
from backend.ingestion.storage.models import StoredFileMetadata
from backend.ingestion.storage.s3 import S3StorageBackend
from backend.ingestion.storage.service import StorageService

__all__ = [
    "StorageBackend",
    "LocalStorageBackend",
    "S3StorageBackend",
    "StorageService",
    "StoredFileMetadata",
    "get_storage_backend",
    "get_storage_service",
]
