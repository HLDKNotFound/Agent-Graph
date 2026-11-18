"""Factory functions to instantiate configured storage backends and services."""

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.storage.interfaces import StorageBackend
from backend.ingestion.storage.local import LocalStorageBackend
from backend.ingestion.storage.s3 import S3StorageBackend
from backend.ingestion.storage.service import StorageService
from backend.ingestion.validation.interfaces import DocumentValidator


def get_storage_backend(settings: IngestionSettings | None = None) -> StorageBackend:
    """Instantiate storage backend based on environment configuration."""
    cfg = settings or get_ingestion_settings()
    driver = cfg.storage_backend.lower().strip()

    if driver == "s3":
        return S3StorageBackend(
            bucket_name=cfg.s3_bucket_name,
            endpoint_url=cfg.s3_endpoint_url,
            access_key_id=cfg.s3_access_key_id,
            secret_access_key=cfg.s3_secret_access_key,
            region_name=cfg.s3_region,
        )

    # Default to Local Filesystem storage
    return LocalStorageBackend(base_dir=cfg.storage_local_root)


def get_storage_service(
    backend: StorageBackend | None = None,
    validator: DocumentValidator | None = None,
    settings: IngestionSettings | None = None,
) -> StorageService:
    """Instantiate high-level StorageService with configured driver and validator."""
    cfg = settings or get_ingestion_settings()
    active_backend = backend or get_storage_backend(cfg)
    return StorageService(backend=active_backend, validator=validator, settings=cfg)
