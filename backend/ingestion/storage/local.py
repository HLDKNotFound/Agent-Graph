"""Local filesystem storage backend driver with path confinement and atomic writes."""

import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.ingestion.exceptions import (
    PathTraversalError,
    StorageError,
    StorageFileNotFoundError,
)
from backend.ingestion.storage.interfaces import StorageBackend
from backend.ingestion.validation.sanitizer import validate_safe_path


class LocalStorageBackend(StorageBackend):
    """Local filesystem storage driver enforcing directory confinement and atomic persistence."""

    def __init__(self, base_dir: Path | str = "storage/documents") -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def driver_name(self) -> str:
        """Driver identifier."""
        return "local_filesystem"

    def _resolve_safe_key(self, key: str) -> Path:
        """Resolve storage key against base directory and verify path containment."""
        if not key or not isinstance(key, str):
            raise StorageError("Storage key cannot be empty or non-string")

        # Reject explicit traversal sequences
        normalized_key = key.strip().replace("\\", "/")
        if ".." in normalized_key or normalized_key.startswith("/"):
            # Check if it attempts to escape
            target = (self.base_dir / normalized_key.lstrip("/")).resolve()
        else:
            target = (self.base_dir / normalized_key).resolve()

        return validate_safe_path(target, self.base_dir)

    def _sync_put(self, target_path: Path, data: bytes) -> str:
        """Synchronously write bytes atomically using a temporary file in the same directory."""
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file then replace
        temp_file = tempfile.NamedTemporaryFile(
            dir=target_path.parent,
            prefix=".tmp_upload_",
            delete=False,
        )
        try:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_file.close()
            os.replace(temp_file.name, str(target_path))
        except Exception as e:
            if os.path.exists(temp_file.name):
                try:
                    os.unlink(temp_file.name)
                except OSError:
                    pass
            raise StorageError(f"Failed writing file to local storage '{target_path}': {e}") from e

        return f"file://{target_path.as_posix()}"

    def _sync_get(self, target_path: Path) -> bytes:
        """Synchronously read file bytes."""
        if not target_path.exists() or not target_path.is_file():
            raise StorageFileNotFoundError(
                f"File not found in local storage: '{target_path}'",
                details={"path": str(target_path)},
            )
        try:
            return target_path.read_bytes()
        except Exception as e:
            raise StorageError(f"Failed reading file from local storage '{target_path}': {e}") from e

    def _sync_delete(self, target_path: Path) -> bool:
        """Synchronously delete file."""
        if not target_path.exists():
            return True
        try:
            target_path.unlink()
            return True
        except Exception as e:
            raise StorageError(f"Failed deleting file from local storage '{target_path}': {e}") from e

    def _sync_exists(self, target_path: Path) -> bool:
        """Synchronously check existence."""
        return target_path.exists() and target_path.is_file()

    def _sync_metadata(self, target_path: Path) -> dict[str, Any]:
        """Synchronously fetch stat metadata."""
        if not target_path.exists() or not target_path.is_file():
            raise StorageFileNotFoundError(
                f"File not found in local storage: '{target_path}'",
                details={"path": str(target_path)},
            )
        try:
            stat_result = target_path.stat()
            return {
                "size_bytes": stat_result.st_size,
                "created_at": datetime.fromtimestamp(stat_result.st_ctime, tz=timezone.utc),
                "modified_at": datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc),
                "path": str(target_path),
                "driver": self.driver_name,
            }
        except Exception as e:
            raise StorageError(f"Failed retrieving metadata from local storage '{target_path}': {e}") from e

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        """Store bytes under target key with path confinement."""
        target_path = self._resolve_safe_key(key)
        return await asyncio.to_thread(self._sync_put, target_path, data)

    async def get(self, key: str) -> bytes:
        """Retrieve stored bytes under key."""
        target_path = self._resolve_safe_key(key)
        return await asyncio.to_thread(self._sync_get, target_path)

    async def delete(self, key: str) -> bool:
        """Delete file at key."""
        target_path = self._resolve_safe_key(key)
        return await asyncio.to_thread(self._sync_delete, target_path)

    async def exists(self, key: str) -> bool:
        """Check whether file at key exists."""
        target_path = self._resolve_safe_key(key)
        return await asyncio.to_thread(self._sync_exists, target_path)

    async def get_raw_metadata(self, key: str) -> dict[str, Any]:
        """Fetch file metadata at key."""
        target_path = self._resolve_safe_key(key)
        return await asyncio.to_thread(self._sync_metadata, target_path)
