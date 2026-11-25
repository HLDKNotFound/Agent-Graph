"""Conversation history storage backends with Redis and In-Memory implementations."""

import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict, deque
import logging
from typing import Any
from uuid import UUID

from backend.query.config import QuerySettings, get_query_settings
from backend.query.exceptions import ConversationHistoryError
from backend.query.history.types import ConversationTurn

logger = logging.getLogger(__name__)


class BaseConversationHistoryStore(ABC):
    """Abstract base class for multi-turn conversation history persistence."""

    @abstractmethod
    async def append_turn(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
        turn: ConversationTurn,
    ) -> None:
        """Append a conversational turn to the rolling history."""
        pass

    @abstractmethod
    async def get_recent_turns(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
        max_turns: int | None = None,
    ) -> list[ConversationTurn]:
        """Retrieve recent conversation turns in chronological order."""
        pass

    @abstractmethod
    async def clear_history(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
    ) -> None:
        """Purge all conversation turns for a given conversation."""
        pass


class InMemoryConversationHistoryStore(BaseConversationHistoryStore):
    """Thread-safe and async-safe in-memory conversation history store.

    Useful for local testing, offline development, or fallback when Redis is unreachable.
    """

    def __init__(self, max_history_turns: int = 10) -> None:
        self.max_history_turns = max_history_turns
        self._stores: dict[tuple[str, str], deque[ConversationTurn]] = defaultdict(
            lambda: deque(maxlen=self.max_history_turns)
        )
        self._lock = asyncio.Lock()

    async def append_turn(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
        turn: ConversationTurn,
    ) -> None:
        key = (str(tenant_id), str(conversation_id))
        async with self._lock:
            # Ensure maxlen is preserved
            dq = self._stores[key]
            if dq.maxlen != self.max_history_turns:
                self._stores[key] = deque(dq, maxlen=self.max_history_turns)
            self._stores[key].append(turn)

    async def get_recent_turns(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
        max_turns: int | None = None,
    ) -> list[ConversationTurn]:
        key = (str(tenant_id), str(conversation_id))
        async with self._lock:
            turns = list(self._stores.get(key, []))
            limit = max_turns if max_turns is not None else self.max_history_turns
            return turns[-limit:] if limit > 0 else []

    async def clear_history(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
    ) -> None:
        key = (str(tenant_id), str(conversation_id))
        async with self._lock:
            if key in self._stores:
                self._stores[key].clear()


class RedisConversationHistoryStore(BaseConversationHistoryStore):
    """Production-grade conversation history store backed by Redis.

    Enforces tenant-isolated keys: `conv:{tenant_id}:{conversation_id}:turns`
    Maintains rolling-window bounded lists via RPUSH + LTRIM and automatic TTL renewal.
    Seamlessly falls back to an internal InMemory store if Redis is unavailable.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        max_history_turns: int | None = None,
        ttl_seconds: int | None = None,
        fallback_to_memory: bool = True,
        redis_client: Any | None = None,
    ) -> None:
        settings = get_query_settings()
        self.redis_url = redis_url or settings.redis_url
        self.max_history_turns = max_history_turns or settings.max_history_turns
        self.ttl_seconds = ttl_seconds or settings.conversation_ttl_seconds
        self.fallback_to_memory = fallback_to_memory
        self._redis_client = redis_client
        self._memory_fallback = InMemoryConversationHistoryStore(
            max_history_turns=self.max_history_turns
        )

    def _get_redis_key(self, tenant_id: str | UUID, conversation_id: str) -> str:
        """Generate tenant-partitioned key to prevent cross-tenant data leakage."""
        return f"conv:{str(tenant_id)}:{str(conversation_id)}:turns"

    def _get_client(self) -> Any:
        if self._redis_client is None:
            try:
                import redis.asyncio as aioredis
                self._redis_client = aioredis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                )
            except Exception as e:
                logger.warning("Failed to initialize Redis async client: %s", e)
        return self._redis_client

    async def append_turn(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
        turn: ConversationTurn,
    ) -> None:
        key = self._get_redis_key(tenant_id, conversation_id)
        payload = turn.model_dump_json()

        try:
            client = self._get_client()
            if client is None:
                raise ConnectionError("Redis client is not available")

            pipe = client.pipeline(transaction=True)
            pipe.rpush(key, payload)
            pipe.ltrim(key, -self.max_history_turns, -1)
            pipe.expire(key, self.ttl_seconds)
            await pipe.execute()
        except Exception as exc:
            if self.fallback_to_memory:
                logger.warning(
                    "Redis unreachable for append_turn, falling back to memory store: %s", exc
                )
                await self._memory_fallback.append_turn(tenant_id, conversation_id, turn)
            else:
                raise ConversationHistoryError(
                    conversation_id=str(conversation_id),
                    reason=f"Failed to persist turn in Redis: {exc}",
                ) from exc

    async def get_recent_turns(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
        max_turns: int | None = None,
    ) -> list[ConversationTurn]:
        key = self._get_redis_key(tenant_id, conversation_id)
        limit = max_turns if max_turns is not None else self.max_history_turns

        try:
            client = self._get_client()
            if client is None:
                raise ConnectionError("Redis client is not available")

            start_idx = -limit if limit > 0 else 0
            raw_items = await client.lrange(key, start_idx, -1)
            if not raw_items and self.fallback_to_memory:
                # Also check memory fallback
                mem_items = await self._memory_fallback.get_recent_turns(
                    tenant_id, conversation_id, max_turns
                )
                if mem_items:
                    return mem_items
            return [ConversationTurn.model_validate_json(item) for item in raw_items]
        except Exception as exc:
            if self.fallback_to_memory:
                logger.warning(
                    "Redis unreachable for get_recent_turns, falling back to memory store: %s", exc
                )
                return await self._memory_fallback.get_recent_turns(
                    tenant_id, conversation_id, max_turns
                )
            else:
                raise ConversationHistoryError(
                    conversation_id=str(conversation_id),
                    reason=f"Failed to retrieve turns from Redis: {exc}",
                ) from exc

    async def clear_history(
        self,
        tenant_id: str | UUID,
        conversation_id: str,
    ) -> None:
        key = self._get_redis_key(tenant_id, conversation_id)
        try:
            client = self._get_client()
            if client is not None:
                await client.delete(key)
        except Exception as exc:
            if not self.fallback_to_memory:
                raise ConversationHistoryError(
                    conversation_id=str(conversation_id),
                    reason=f"Failed to clear Redis history: {exc}",
                ) from exc
        finally:
            await self._memory_fallback.clear_history(tenant_id, conversation_id)

    async def close(self) -> None:
        """Close active Redis connections."""
        if self._redis_client is not None:
            try:
                await self._redis_client.aclose()
            except Exception:
                pass


def get_conversation_history_store(
    settings: QuerySettings | None = None,
    prefer_redis: bool = True,
) -> BaseConversationHistoryStore:
    """Factory creating an appropriately configured conversation history store."""
    cfg = settings or get_query_settings()
    if prefer_redis:
        return RedisConversationHistoryStore(
            redis_url=cfg.redis_url,
            max_history_turns=cfg.max_history_turns,
            ttl_seconds=cfg.conversation_ttl_seconds,
            fallback_to_memory=True,
        )
    return InMemoryConversationHistoryStore(max_history_turns=cfg.max_history_turns)
