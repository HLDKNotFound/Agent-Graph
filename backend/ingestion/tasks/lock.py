"""Distributed and local idempotency lock managers for preventing duplicate Celery task processing."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from backend.ingestion.config import IngestionSettings, get_ingestion_settings

logger = logging.getLogger(__name__)


class InMemoryLockStore:
    """Thread-safe in-memory lock and job completion registry for local/offline testing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_locks: dict[str, float] = {}  # job_id -> expiry_timestamp
        self._completed_jobs: dict[str, float] = {}  # job_id -> expiry_timestamp

    def acquire(self, key: str, timeout_seconds: int = 600) -> bool:
        with self._lock:
            now = time.time()
            # Clean up expired lock if present
            if key in self._active_locks and self._active_locks[key] < now:
                del self._active_locks[key]

            if key in self._active_locks:
                return False

            self._active_locks[key] = now + timeout_seconds
            return True

    def release(self, key: str) -> bool:
        with self._lock:
            if key in self._active_locks:
                del self._active_locks[key]
                return True
            return False

    def is_locked(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            if key in self._active_locks:
                if self._active_locks[key] >= now:
                    return True
                del self._active_locks[key]
            return False

    def mark_completed(self, key: str, ttl_seconds: int = 86400) -> None:
        with self._lock:
            self._completed_jobs[key] = time.time() + ttl_seconds
            if key in self._active_locks:
                del self._active_locks[key]

    def is_completed(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            if key in self._completed_jobs:
                if self._completed_jobs[key] >= now:
                    return True
                del self._completed_jobs[key]
            return False

    def clear(self) -> None:
        with self._lock:
            self._active_locks.clear()
            self._completed_jobs.clear()


class JobIdempotencyGuard:
    """Idempotency and concurrency guard for Celery background tasks.

    Prevents duplicate processing when the same job is delivered multiple times
    by message brokers or retry mechanisms (Requirements 7 & 8).
    Uses Redis when connected, with automatic fallback to thread-safe in-memory registry.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        settings: IngestionSettings | None = None,
        force_in_memory: bool = False,
    ) -> None:
        self.settings = settings or get_ingestion_settings()
        self._redis_url = redis_url or self.settings.redis_url
        self._force_in_memory = force_in_memory
        self._in_memory = InMemoryLockStore()
        self._redis_client: Any = None
        self._redis_available: bool | None = None

    def _get_redis(self) -> Any:
        if self._force_in_memory:
            return None
        if self._redis_available is False:
            return None

        if self._redis_client is None:
            try:
                import redis
                client = redis.Redis.from_url(
                    self._redis_url,
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                )
                client.ping()
                self._redis_client = client
                self._redis_available = True
                logger.info("Connected to Redis for job idempotency locking at %s", self._redis_url)
            except Exception as exc:
                logger.warning(
                    "Redis not reachable at %s (%s). Falling back to in-memory lock store.",
                    self._redis_url,
                    exc,
                )
                self._redis_available = False
                self._redis_client = None

        return self._redis_client

    def acquire_job_lock(self, job_id: str, timeout_seconds: int = 600) -> bool:
        """Acquire a processing lock for a specific job ID.

        Returns:
            True if the lock was acquired (job should be processed).
            False if another worker holds the lock or is already processing.
        """
        key = f"lock:ingestion:job:{job_id}"
        r = self._get_redis()
        if r is not None:
            try:
                acquired = bool(r.set(key, "locked", nx=True, ex=timeout_seconds))
                return acquired
            except Exception as exc:
                logger.warning("Redis acquire error: %s. Using in-memory fallback.", exc)

        return self._in_memory.acquire(key, timeout_seconds)

    def release_job_lock(self, job_id: str) -> bool:
        """Release the processing lock for a specific job ID."""
        key = f"lock:ingestion:job:{job_id}"
        r = self._get_redis()
        if r is not None:
            try:
                deleted = bool(r.delete(key))
                return deleted
            except Exception as exc:
                logger.warning("Redis release error: %s. Using in-memory fallback.", exc)

        return self._in_memory.release(key)

    def mark_job_completed(self, job_id: str, ttl_seconds: int = 86400) -> None:
        """Record that a job has finished successfully to prevent duplicate processing on redelivery."""
        comp_key = f"completed:ingestion:job:{job_id}"
        lock_key = f"lock:ingestion:job:{job_id}"
        r = self._get_redis()
        if r is not None:
            try:
                r.set(comp_key, "completed", ex=ttl_seconds)
                r.delete(lock_key)
                return
            except Exception as exc:
                logger.warning("Redis mark_completed error: %s.", exc)

        self._in_memory.mark_completed(comp_key, ttl_seconds)
        self._in_memory.release(lock_key)

    def is_job_completed(self, job_id: str) -> bool:
        """Check whether a job has already completed successfully."""
        comp_key = f"completed:ingestion:job:{job_id}"
        r = self._get_redis()
        if r is not None:
            try:
                return bool(r.exists(comp_key))
            except Exception:
                pass

        return self._in_memory.is_completed(comp_key)

    def is_job_in_progress(self, job_id: str) -> bool:
        """Check whether a job is currently locked and undergoing execution."""
        key = f"lock:ingestion:job:{job_id}"
        r = self._get_redis()
        if r is not None:
            try:
                return bool(r.exists(key))
            except Exception:
                pass

        return self._in_memory.is_locked(key)


# Shared default singleton
_default_lock_guard: JobIdempotencyGuard | None = None


def get_idempotency_guard(force_in_memory: bool = False) -> JobIdempotencyGuard:
    """Retrieve or initialize the shared singleton JobIdempotencyGuard."""
    global _default_lock_guard
    if _default_lock_guard is None or force_in_memory:
        _default_lock_guard = JobIdempotencyGuard(force_in_memory=force_in_memory)
    return _default_lock_guard
