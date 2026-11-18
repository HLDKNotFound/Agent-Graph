"""S3-compatible object storage backend driver for enterprise document storage."""

import asyncio
from datetime import datetime, timezone
from typing import Any

from backend.ingestion.exceptions import (
    StorageAccessDeniedError,
    StorageConfigurationError,
    StorageError,
    StorageFileNotFoundError,
)
from backend.ingestion.storage.interfaces import StorageBackend


class S3StorageBackend(StorageBackend):
    """S3-compatible object storage driver supporting AWS S3, Cloudflare R2, and MinIO."""

    def __init__(
        self,
        bucket_name: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        self.bucket_name = bucket_name
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region_name = region_name
        self._client: Any = None

    @property
    def driver_name(self) -> str:
        """Driver identifier."""
        return "s3_object_storage"

    def _get_client(self) -> Any:
        """Initialize or return boto3 S3 client."""
        if self._client is not None:
            return self._client

        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise StorageConfigurationError(
                "The 'boto3' package is required to use S3StorageBackend. "
                "Install it in your environment using: pip install boto3"
            ) from e

        config = Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        )

        client_kwargs: dict[str, Any] = {
            "service_name": "s3",
            "region_name": self.region_name,
            "config": config,
        }

        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url
        if self.access_key_id and self.secret_access_key:
            client_kwargs["aws_access_key_id"] = self.access_key_id
            client_kwargs["aws_secret_access_key"] = self.secret_access_key

        self._client = boto3.client(**client_kwargs)
        return self._client

    def _sync_put(self, key: str, data: bytes, content_type: str | None) -> str:
        """Synchronously upload object to S3 bucket."""
        client = self._get_client()
        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type

        try:
            client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                **extra_args,
            )
            return f"s3://{self.bucket_name}/{key}"
        except Exception as e:
            raise StorageError(f"Failed uploading object to S3 ({self.bucket_name}/{key}): {e}") from e

    def _sync_get(self, key: str) -> bytes:
        """Synchronously download object bytes from S3 bucket."""
        client = self._get_client()
        try:
            response = client.get_object(Bucket=self.bucket_name, Key=key)
            return response["Body"].read()
        except Exception as e:
            error_code = getattr(getattr(e, "response", {}), "get", lambda _: {})("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404", "NotFound"):
                raise StorageFileNotFoundError(
                    f"Object not found in S3 bucket '{self.bucket_name}': '{key}'",
                    details={"bucket": self.bucket_name, "key": key},
                ) from e
            if error_code in ("AccessDenied", "403"):
                raise StorageAccessDeniedError(
                    f"Access denied to S3 object ({self.bucket_name}/{key}): {e}",
                    details={"bucket": self.bucket_name, "key": key},
                ) from e
            raise StorageError(f"Failed retrieving object from S3 ({self.bucket_name}/{key}): {e}") from e

    def _sync_delete(self, key: str) -> bool:
        """Synchronously delete object from S3 bucket."""
        client = self._get_client()
        try:
            client.delete_object(Bucket=self.bucket_name, Key=key)
            return True
        except Exception as e:
            raise StorageError(f"Failed deleting object from S3 ({self.bucket_name}/{key}): {e}") from e

    def _sync_exists(self, key: str) -> bool:
        """Synchronously check if object exists in S3."""
        client = self._get_client()
        try:
            client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except Exception as e:
            error_code = getattr(getattr(e, "response", {}), "get", lambda _: {})("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404", "NotFound"):
                return False
            raise StorageError(f"Failed checking existence in S3 ({self.bucket_name}/{key}): {e}") from e

    def _sync_metadata(self, key: str) -> dict[str, Any]:
        """Synchronously fetch head object metadata from S3."""
        client = self._get_client()
        try:
            response = client.head_object(Bucket=self.bucket_name, Key=key)
            return {
                "size_bytes": response.get("ContentLength", 0),
                "modified_at": response.get("LastModified", datetime.now(timezone.utc)),
                "content_type": response.get("ContentType"),
                "etag": response.get("ETag", "").strip('"'),
                "bucket": self.bucket_name,
                "driver": self.driver_name,
            }
        except Exception as e:
            error_code = getattr(getattr(e, "response", {}), "get", lambda _: {})("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404", "NotFound"):
                raise StorageFileNotFoundError(
                    f"Object not found in S3 bucket '{self.bucket_name}': '{key}'",
                    details={"bucket": self.bucket_name, "key": key},
                ) from e
            raise StorageError(f"Failed retrieving metadata from S3 ({self.bucket_name}/{key}): {e}") from e

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        """Store bytes under S3 bucket key."""
        return await asyncio.to_thread(self._sync_put, key, data, content_type)

    async def get(self, key: str) -> bytes:
        """Retrieve stored bytes from S3 key."""
        return await asyncio.to_thread(self._sync_get, key)

    async def delete(self, key: str) -> bool:
        """Delete object from S3."""
        return await asyncio.to_thread(self._sync_delete, key)

    async def exists(self, key: str) -> bool:
        """Check whether object exists in S3."""
        return await asyncio.to_thread(self._sync_exists, key)

    async def get_raw_metadata(self, key: str) -> dict[str, Any]:
        """Fetch object metadata from S3."""
        return await asyncio.to_thread(self._sync_metadata, key)
