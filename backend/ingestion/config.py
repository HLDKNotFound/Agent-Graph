"""Dynamic environment-based configuration for document ingestion and storage subsystems."""

from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionSettings(BaseSettings):
    """Dynamic configuration settings for file ingestion, validation, and storage.

    Adheres strictly to the Zero Hardcoding Principle: resolves all paths,
    credentials, bucket names, and file size limits from environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------
    # Storage Backend Configuration
    # ------------------------------------------
    storage_backend: str = Field(
        default="local",
        alias="STORAGE_BACKEND",
        description="Physical storage backend driver ('local' or 's3')",
    )
    storage_local_root: str = Field(
        default="storage/documents",
        alias="STORAGE_LOCAL_ROOT",
        description="Root directory for local filesystem storage",
    )

    # ------------------------------------------
    # S3 Object Storage Settings
    # ------------------------------------------
    s3_endpoint_url: str | None = Field(default=None, alias="S3_ENDPOINT_URL")
    s3_access_key_id: str | None = Field(default=None, alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str | None = Field(default=None, alias="S3_SECRET_ACCESS_KEY")
    s3_bucket_name: str = Field(
        default="enterprise-knowledge-docs",
        alias="S3_BUCKET_NAME",
        description="Target S3 / MinIO / R2 bucket name",
    )
    s3_region: str = Field(
        default="us-east-1",
        alias="S3_REGION",
        description="S3 region identifier",
    )

    # ------------------------------------------
    # Upload & File Validation Restrictions
    # ------------------------------------------
    max_file_size_bytes: int = Field(
        default=52_428_800,  # 50 MB
        alias="MAX_FILE_SIZE_BYTES",
        description="Maximum allowed uploaded file size in bytes (default: 50MB)",
    )
    min_file_size_bytes: int = Field(
        default=1,
        alias="MIN_FILE_SIZE_BYTES",
        description="Minimum allowed uploaded file size in bytes (rejects 0-byte files)",
    )
    max_filename_length: int = Field(
        default=255,
        alias="MAX_FILENAME_LENGTH",
        description="Maximum length for sanitized filenames",
    )
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".docx", ".xlsx", ".csv"],
        alias="ALLOWED_EXTENSIONS",
        description="List of allowed document file extensions",
    )
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
            "text/plain",
        ],
        alias="ALLOWED_MIME_TYPES",
        description="List of allowed document MIME content types",
    )


@lru_cache(maxsize=1)
def get_ingestion_settings() -> IngestionSettings:
    """Provide a cached singleton instance of IngestionSettings."""
    return IngestionSettings()
