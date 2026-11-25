import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from backend.query.history.types import ConversationTurn
from backend.query.history.store import (
    InMemoryConversationHistoryStore,
    RedisConversationHistoryStore,
    get_conversation_history_store,
)
from backend.query.types.intent import IntentLabel
from backend.query.exceptions import ConversationHistoryError


def test_in_memory_store_rolling_window():
    """Verify in-memory conversation store rolling window retention and limits."""
    async def _run():
        store = InMemoryConversationHistoryStore(max_history_turns=3)
        tenant_id = uuid4()
        conv_id = "test-conv-001"

        # Append 5 turns
        for i in range(5):
            turn = ConversationTurn(
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}",
            )
            await store.append_turn(tenant_id, conv_id, turn)

        turns = await store.get_recent_turns(tenant_id, conv_id)
        assert len(turns) == 3
        assert turns[0].content == "Message 2"
        assert turns[1].content == "Message 3"
        assert turns[2].content == "Message 4"
    asyncio.run(_run())


def test_in_memory_store_tenant_isolation():
    """Verify that different tenants are isolated and cannot access each other's history."""
    async def _run():
        store = InMemoryConversationHistoryStore(max_history_turns=5)
        tenant_a = uuid4()
        tenant_b = uuid4()
        conv_id = "shared-conv-id"

        turn_a = ConversationTurn(role="user", content="Tenant A confidential message")
        turn_b = ConversationTurn(role="user", content="Tenant B public message")

        await store.append_turn(tenant_a, conv_id, turn_a)
        await store.append_turn(tenant_b, conv_id, turn_b)

        turns_a = await store.get_recent_turns(tenant_a, conv_id)
        turns_b = await store.get_recent_turns(tenant_b, conv_id)

        assert len(turns_a) == 1
        assert turns_a[0].content == "Tenant A confidential message"

        assert len(turns_b) == 1
        assert turns_b[0].content == "Tenant B public message"
    asyncio.run(_run())


def test_in_memory_store_clear():
    """Verify clearing conversation history."""
    async def _run():
        store = InMemoryConversationHistoryStore(max_history_turns=5)
        tenant_id = uuid4()
        conv_id = "clear-test"

        await store.append_turn(tenant_id, conv_id, ConversationTurn(role="user", content="Hello"))
        turns = await store.get_recent_turns(tenant_id, conv_id)
        assert len(turns) == 1

        await store.clear_history(tenant_id, conv_id)
        turns_cleared = await store.get_recent_turns(tenant_id, conv_id)
        assert len(turns_cleared) == 0
    asyncio.run(_run())


def test_redis_store_pipeline_and_key_structure():
    """Verify Redis store uses tenant-scoped key and executes pipeline RPUSH/LTRIM/EXPIRE."""
    async def _run():
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(return_value=[1, "OK", True])

        mock_client = AsyncMock()
        mock_client.pipeline = MagicMock(return_value=mock_pipeline)
        mock_client.lrange = AsyncMock(return_value=[])

        store = RedisConversationHistoryStore(
            redis_url="redis://localhost:6379/0",
            max_history_turns=5,
            ttl_seconds=3600,
            redis_client=mock_client,
        )

        tenant_id = uuid4()
        conv_id = "conv-abc"
        turn = ConversationTurn(
            role="user",
            content="Chính sách bảo hiểm là gì?",
            intent_label=IntentLabel.FACTUAL_RETRIEVAL,
        )

        await store.append_turn(tenant_id, conv_id, turn)

        expected_key = f"conv:{tenant_id}:{conv_id}:turns"
        mock_pipeline.rpush.assert_called_once()
        assert mock_pipeline.rpush.call_args[0][0] == expected_key
        mock_pipeline.ltrim.assert_called_once_with(expected_key, -5, -1)
        mock_pipeline.expire.assert_called_once_with(expected_key, 3600)
        mock_pipeline.execute.assert_called_once()
    asyncio.run(_run())


def test_redis_store_retrieval_and_serialization():
    """Verify deserialization of turns from Redis lrange."""
    async def _run():
        turn_obj = ConversationTurn(
            role="assistant",
            content="Chính sách bảo hiểm được quy định tại mục 4.",
        )
        payload = turn_obj.model_dump_json()

        mock_client = AsyncMock()
        mock_client.lrange = AsyncMock(return_value=[payload])

        store = RedisConversationHistoryStore(
            max_history_turns=10,
            redis_client=mock_client,
        )

        tenant_id = uuid4()
        conv_id = "conv-xyz"
        turns = await store.get_recent_turns(tenant_id, conv_id)

        assert len(turns) == 1
        assert turns[0].content == turn_obj.content
        assert turns[0].role == "assistant"
    asyncio.run(_run())


def test_redis_fallback_to_memory_on_connection_failure():
    """Verify graceful fallback to memory store when Redis raises ConnectionError."""
    async def _run():
        broken_client = AsyncMock()
        broken_pipeline = MagicMock()
        broken_pipeline.execute = AsyncMock(side_effect=ConnectionError("Redis server offline"))
        broken_client.pipeline = MagicMock(return_value=broken_pipeline)
        broken_client.lrange = AsyncMock(side_effect=ConnectionError("Redis server offline"))

        store = RedisConversationHistoryStore(
            fallback_to_memory=True,
            redis_client=broken_client,
        )

        tenant_id = uuid4()
        conv_id = "conv-offline"
        turn = ConversationTurn(role="user", content="Xin chào khi offline")

        # Should not raise exception because fallback_to_memory is True
        await store.append_turn(tenant_id, conv_id, turn)

        # Retrieval should return from in-memory fallback
        turns = await store.get_recent_turns(tenant_id, conv_id)
        assert len(turns) == 1
        assert turns[0].content == "Xin chào khi offline"
    asyncio.run(_run())


def test_redis_store_strict_error_when_no_fallback():
    """Verify that when fallback_to_memory is False, Redis errors raise ConversationHistoryError."""
    async def _run():
        broken_client = AsyncMock()
        broken_pipeline = MagicMock()
        broken_pipeline.execute = AsyncMock(side_effect=ConnectionError("Connection refused"))
        broken_client.pipeline = MagicMock(return_value=broken_pipeline)

        store = RedisConversationHistoryStore(
            fallback_to_memory=False,
            redis_client=broken_client,
        )

        tenant_id = uuid4()
        conv_id = "conv-error"
        turn = ConversationTurn(role="user", content="Hello")

        with pytest.raises(ConversationHistoryError):
            await store.append_turn(tenant_id, conv_id, turn)
    asyncio.run(_run())


def test_store_factory():
    """Verify get_conversation_history_store factory."""
    store_redis = get_conversation_history_store(prefer_redis=True)
    assert isinstance(store_redis, RedisConversationHistoryStore)

    store_mem = get_conversation_history_store(prefer_redis=False)
    assert isinstance(store_mem, InMemoryConversationHistoryStore)
