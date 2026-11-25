"""Unit tests for Phase 3 Task 03: Coreference Resolution & Contextual Query Rewriter."""

import asyncio
import pytest

from backend.query.config import QuerySettings
from backend.query.history.types import ConversationTurn
from backend.query.rewriting.rewriter import QueryRewriter


def test_fast_bypass_when_no_history():
    """Verify that queries bypass rewriting immediately when history is empty."""
    async def _run():
        rewriter = QueryRewriter()
        raw = "Nó có áp dụng cho nhân viên thử việc không?"
        res = await rewriter.rewrite(raw, history=[])
        assert res == raw
    asyncio.run(_run())


def test_fast_bypass_on_long_standalone_query():
    """Verify that queries >= 12 words without referential pronouns bypass rewriting."""
    async def _run():
        rewriter = QueryRewriter()
        history = [
            ConversationTurn(role="user", content="Chính sách nghỉ phép năm 2024"),
            ConversationTurn(role="assistant", content="Quy định mỗi năm có 12 ngày phép."),
        ]
        # Query with 13 words, no demonstrative pronouns
        standalone = "Quy trình xin cấp mới trang thiết bị làm việc của nhân viên khối công nghệ"
        res = await rewriter.rewrite(standalone, history=history)
        assert res == standalone
    asyncio.run(_run())


def test_pronoun_detection():
    """Verify regex detection of referential pronouns and markers."""
    rewriter = QueryRewriter()
    assert rewriter.has_referential_pronouns("Nó áp dụng cho ai?") is True
    assert rewriter.has_referential_pronouns("Chi tiết chính sách đó là gì?") is True
    assert rewriter.has_referential_pronouns("Thế còn bảo hiểm y tế?") is True
    assert rewriter.has_referential_pronouns("Who is eligible for it?") is True
    assert rewriter.has_referential_pronouns("Tell me about that policy") is True
    assert rewriter.has_referential_pronouns("Quy chế công tác năm 2024") is False


def test_llm_callable_successful_rewriting():
    """Verify contextual rewriting using custom LLM callable."""
    async def _run():
        async def mock_llm(query: str, history: list[dict[str, str]]) -> str:
            assert "nó" in query.lower()
            return "Chính sách bảo hiểm y tế áp dụng cho ai?"

        rewriter = QueryRewriter(llm_callable=mock_llm)
        history = [
            ConversationTurn(role="user", content="Chính sách bảo hiểm y tế có những gì?"),
            ConversationTurn(role="assistant", content="Bảo hiểm bao gồm viện phí nội trú và nha khoa."),
        ]
        query = "Nó áp dụng cho ai?"
        rewritten = await rewriter.rewrite(query, history=history)
        assert rewritten == "Chính sách bảo hiểm y tế áp dụng cho ai?"
    asyncio.run(_run())


def test_rewriter_timeout_graceful_fallback():
    """Verify that timeout triggers clean fallback to raw query without raising error."""
    async def _run():
        async def slow_llm(query: str, history: list[dict[str, str]]) -> str:
            await asyncio.sleep(0.5)
            return "Should have timed out"

        settings = QuerySettings(REWRITER_TIMEOUT_SECONDS=0.05)
        rewriter = QueryRewriter(settings=settings, llm_callable=slow_llm)

        history = [ConversationTurn(role="user", content="Chính sách lương")]
        raw = "Nó là bao nhiêu?"
        result = await rewriter.rewrite(raw, history=history)
        # Fallback to original raw query
        assert result == raw
    asyncio.run(_run())


def test_rewriter_error_graceful_fallback():
    """Verify that LLM runtime exceptions trigger graceful fallback to raw query."""
    async def _run():
        async def failing_llm(query: str, history: list[dict[str, str]]) -> str:
            raise RuntimeError("API quota exceeded")

        rewriter = QueryRewriter(llm_callable=failing_llm)
        history = [ConversationTurn(role="user", content="Quy định nghỉ thai sản")]
        raw = "Thời gian nghỉ của nó là bao lâu?"
        result = await rewriter.rewrite(raw, history=history)
        assert result == raw
    asyncio.run(_run())


def test_rewriter_quotes_sanitization():
    """Verify that surrounding quotes in LLM output are cleanly stripped."""
    async def _run():
        async def quoted_llm(query: str, history: list[dict[str, str]]) -> str:
            return '"Chính sách bảo hiểm xã hội năm 2024 là gì?"'

        rewriter = QueryRewriter(llm_callable=quoted_llm)
        history = [ConversationTurn(role="user", content="Bảo hiểm xã hội")]
        result = await rewriter.rewrite("Nó là gì?", history=history)
        assert result == "Chính sách bảo hiểm xã hội năm 2024 là gì?"
    asyncio.run(_run())
