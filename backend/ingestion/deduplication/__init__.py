"""Deduplication subsystem contracts."""

from backend.ingestion.deduplication.base import (
    DeduplicationResult,
    DeduplicationStrategy,
)

__all__ = [
    "DeduplicationResult",
    "DeduplicationStrategy",
]
