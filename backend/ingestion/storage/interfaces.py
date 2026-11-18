"""Storage backend abstraction contracts and interfaces."""

import abc
from typing import Any


class StorageBackend(abc.ABC):
    """Abstract driver interface for low-level physical file storage.

    Decouples raw persistence (Local Filesystem, S3, MinIO, Azure Blob) from business logic.
    """

    @property
    @abc.abstractmethod
    def driver_name(self) -> str:
        """Identifier name of the storage backend driver."""
        raise NotImplementedError

    @abc.abstractmethod
    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        """Store binary payload under target relative storage key.

        Args:
            key: Relative storage path key (e.g. 'tenants/123/documents/abc/file.pdf').
            data: Binary content bytes.
            content_type: MIME content type string.

        Returns:
            Canonical persistent storage URI (e.g. 'file:///...' or 's3://...').
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get(self, key: str) -> bytes:
        """Retrieve binary payload for a storage key.

        Args:
            key: Relative storage path key.

        Returns:
            Binary content bytes.

        Raises:
            StorageFileNotFoundError: If key does not exist.
            StorageError: On low-level IO or connection failure.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete stored binary object associated with key.

        Args:
            key: Relative storage path key.

        Returns:
            True if object was deleted or did not exist.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def exists(self, key: str) -> bool:
        """Check whether object exists at target storage key.

        Args:
            key: Relative storage path key.

        Returns:
            True if object exists, False otherwise.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_raw_metadata(self, key: str) -> dict[str, Any]:
        """Retrieve low-level physical metadata (file size, modified timestamp, ETag).

        Args:
            key: Relative storage path key.

        Returns:
            Dictionary containing size_bytes, modified_at, and driver-specific attributes.

        Raises:
            StorageFileNotFoundError: If key does not exist.
        """
        raise NotImplementedError
