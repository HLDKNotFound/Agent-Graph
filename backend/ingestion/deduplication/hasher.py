"""High-performance streaming SHA-256 hash calculation for enterprise document deduplication.

Operates independently from document extraction/parsing and processes arbitrary binary streams
chunk-by-chunk to enforce bounded memory consumption regardless of file size.
"""

import hashlib
import io
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.exceptions import EmptyFileError, ValidationError


class StreamHasher:
    """Streaming SHA-256 content hasher supporting bounded memory consumption.

    Guarantees:
    1. Independent from document parsing (extractors, OCR, format inspectors).
    2. Uses streaming block reads (default 64KB) so multi-GB files never exhaust memory.
    3. Restores stream position for seekable streams so downstream extraction is unimpeded.
    4. Calculates both SHA-256 hexadecimal digest and exact byte length in a single pass.
    """

    def __init__(self, chunk_size: int | None = None) -> None:
        settings = get_ingestion_settings()
        self.chunk_size = chunk_size or settings.deduplication_chunk_size_bytes

    def hash_stream(
        self,
        stream: BinaryIO,
        chunk_size: int | None = None,
        rewind_after: bool = True,
        reject_empty: bool = False,
    ) -> tuple[str, int]:
        """Compute SHA-256 hash and byte count from a binary stream in chunks.

        Args:
            stream: Readable binary IO stream.
            chunk_size: Optional chunk size override (default: 64KB).
            rewind_after: If True and stream is seekable, resets cursor to initial position.
            reject_empty: If True, raises EmptyFileError if 0 bytes are read.

        Returns:
            Tuple of (hex_digest: str, total_bytes: int).
        """
        buffer_size = chunk_size or self.chunk_size
        hasher = hashlib.sha256()
        total_bytes = 0

        initial_pos = 0
        is_seekable = hasattr(stream, "seekable") and stream.seekable()
        if is_seekable:
            try:
                initial_pos = stream.tell()
            except (OSError, io.UnsupportedOperation):
                is_seekable = False

        try:
            while True:
                chunk = stream.read(buffer_size)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                hasher.update(chunk)
                total_bytes += len(chunk)
        finally:
            if rewind_after and is_seekable:
                try:
                    stream.seek(initial_pos)
                except (OSError, io.UnsupportedOperation):
                    pass

        if reject_empty and total_bytes == 0:
            raise EmptyFileError("Cannot compute hash on empty file (0 bytes).")

        return hasher.hexdigest(), total_bytes

    def hash_file(
        self,
        path: Path | str,
        chunk_size: int | None = None,
        reject_empty: bool = False,
    ) -> tuple[str, int]:
        """Compute SHA-256 hash and byte count from a file on disk via streaming reads.

        Args:
            path: Local filesystem Path or str.
            chunk_size: Optional chunk size override (default: 64KB).
            reject_empty: If True, raises EmptyFileError if file size is 0.

        Returns:
            Tuple of (hex_digest: str, total_bytes: int).
        """
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            raise ValidationError(
                f"File not found for hash calculation: {file_path}",
                details={"path": str(file_path)},
            )

        with file_path.open("rb") as f:
            return self.hash_stream(
                f,
                chunk_size=chunk_size,
                rewind_after=False,
                reject_empty=reject_empty,
            )

    def hash_bytes(
        self,
        content: bytes,
        chunk_size: int | None = None,
        reject_empty: bool = False,
    ) -> tuple[str, int]:
        """Compute SHA-256 hash and byte count from contiguous in-memory bytes."""
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ValidationError(f"Expected bytes-like object, got {type(content)}")

        total_bytes = len(content)
        if reject_empty and total_bytes == 0:
            raise EmptyFileError("Cannot compute hash on empty bytes (0 bytes).")

        # For small in-memory bytes, direct sha256 is optimal
        buffer_size = chunk_size or self.chunk_size
        if total_bytes <= buffer_size:
            return hashlib.sha256(content).hexdigest(), total_bytes

        # For larger bytes, stream chunk-by-chunk using BytesIO
        stream = io.BytesIO(content)
        return self.hash_stream(
            stream,
            chunk_size=buffer_size,
            rewind_after=False,
            reject_empty=reject_empty,
        )

    def hash_content(
        self,
        content: bytes | BinaryIO | Path | str,
        chunk_size: int | None = None,
        rewind_after: bool = True,
        reject_empty: bool = False,
    ) -> tuple[str, int]:
        """Unified polymorphic entrypoint computing SHA-256 from bytes, stream, or file path.

        Args:
            content: bytes, file-like object, Path, or filepath str.
            chunk_size: Optional chunk buffer override.
            rewind_after: If stream is seekable, resets position after reading.
            reject_empty: If True, raises EmptyFileError if 0 bytes.

        Returns:
            Tuple of (hex_digest: str, total_bytes: int).
        """
        if isinstance(content, (bytes, bytearray, memoryview)):
            return self.hash_bytes(bytes(content), chunk_size=chunk_size, reject_empty=reject_empty)

        if isinstance(content, Path):
            return self.hash_file(content, chunk_size=chunk_size, reject_empty=reject_empty)

        if isinstance(content, str):
            p = Path(content)
            if p.exists() and p.is_file():
                return self.hash_file(p, chunk_size=chunk_size, reject_empty=reject_empty)
            # If string is not a file path, treat as string content
            return self.hash_bytes(content.encode("utf-8"), chunk_size=chunk_size, reject_empty=reject_empty)

        if hasattr(content, "read"):
            return self.hash_stream(
                content,
                chunk_size=chunk_size,
                rewind_after=rewind_after,
                reject_empty=reject_empty,
            )

        raise ValidationError(
            f"Unsupported content type for SHA-256 hashing: {type(content)}",
            details={"content_type": str(type(content))},
        )

    async def hash_async_stream(
        self,
        async_stream: AsyncIterator[bytes],
        reject_empty: bool = False,
    ) -> tuple[str, int]:
        """Compute SHA-256 hash and byte count from an async bytes generator."""
        hasher = hashlib.sha256()
        total_bytes = 0

        async for chunk in async_stream:
            if not chunk:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            hasher.update(chunk)
            total_bytes += len(chunk)

        if reject_empty and total_bytes == 0:
            raise EmptyFileError("Cannot compute hash on empty async stream (0 bytes).")

        return hasher.hexdigest(), total_bytes


# ---------------------------------------------------------------------------
# Convenience module-level functional APIs
# ---------------------------------------------------------------------------

_default_hasher = StreamHasher()


def compute_stream_sha256(
    stream: BinaryIO,
    chunk_size: int | None = None,
    rewind_after: bool = True,
    reject_empty: bool = False,
) -> tuple[str, int]:
    """Compute SHA-256 hash and byte count from a binary stream in chunks."""
    return _default_hasher.hash_stream(
        stream,
        chunk_size=chunk_size,
        rewind_after=rewind_after,
        reject_empty=reject_empty,
    )


def compute_file_sha256(
    path: Path | str,
    chunk_size: int | None = None,
    reject_empty: bool = False,
) -> tuple[str, int]:
    """Compute SHA-256 hash and byte count from a file on disk via streaming reads."""
    return _default_hasher.hash_file(
        path,
        chunk_size=chunk_size,
        reject_empty=reject_empty,
    )


def compute_bytes_sha256(
    content: bytes,
    chunk_size: int | None = None,
    reject_empty: bool = False,
) -> tuple[str, int]:
    """Compute SHA-256 hash and byte count from in-memory bytes."""
    return _default_hasher.hash_bytes(
        content,
        chunk_size=chunk_size,
        reject_empty=reject_empty,
    )


def compute_content_sha256(
    content: bytes | BinaryIO | Path | str,
    chunk_size: int | None = None,
    rewind_after: bool = True,
    reject_empty: bool = False,
) -> tuple[str, int]:
    """Compute SHA-256 hash and byte count from arbitrary input (bytes, stream, or file)."""
    return _default_hasher.hash_content(
        content,
        chunk_size=chunk_size,
        rewind_after=rewind_after,
        reject_empty=reject_empty,
    )
