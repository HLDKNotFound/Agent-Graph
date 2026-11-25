"""Coreference resolution and contextual query rewriting engine."""

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

from backend.query.config import QuerySettings, get_query_settings
from backend.query.history.types import ConversationTurn

logger = logging.getLogger(__name__)

# Demonstrative pronouns, referential markers, and elliptical triggers (Vietnamese & English)
PRONOUN_PATTERN = re.compile(
    r"\b("
    # Vietnamese demonstratives & reference words
    r"nó|chúng nó|họ|đó|này|kia|ấy|vậy|như trên|ở trên|trên|"
    r"vừa rồi|vừa nêu|vừa nói|chính sách đó|chính sách này|"
    r"quy định đó|quy định này|quy chế đó|quy chế này|"
    r"tài liệu đó|tài liệu này|còn|thế còn|vậy còn|"
    # English demonstratives & pronouns
    r"it|its|they|them|their|this|that|these|those|"
    r"above|aforementioned|the same|what about|how about"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


class QueryRewriter:
    """Translates ambiguous multi-turn user inquiries into standalone search queries.

    Uses fast-path heuristic evaluation to bypass LLM rewriting when queries are
    already self-contained (>= 12 words with zero demonstratives).
    Guards LLM execution with strict latency timeouts and graceful raw query fallback.
    """

    def __init__(
        self,
        settings: QuerySettings | None = None,
        llm_callable: Callable[[str, list[dict[str, str]]], Awaitable[str]] | None = None,
    ) -> None:
        self.settings = settings or get_query_settings()
        self.timeout_seconds = self.settings.rewriter_timeout_seconds
        self.bypass_word_count = self.settings.rewriter_fast_bypass_word_count
        self._custom_llm_callable = llm_callable

    def has_referential_pronouns(self, query: str) -> bool:
        """Check whether the query contains demonstrative pronouns or ellipsis markers."""
        return bool(PRONOUN_PATTERN.search(query))

    def should_bypass_rewriting(self, query: str, history: list[ConversationTurn]) -> bool:
        """Determine if query is already standalone and can bypass LLM rewriting."""
        # If there is no previous conversation history, no coreference is possible
        if not history:
            return True

        words = query.strip().split()
        # If query has sufficient length and zero referential markers, bypass
        if len(words) >= self.bypass_word_count and not self.has_referential_pronouns(query):
            return True

        return False

    async def rewrite(
        self,
        raw_query: str,
        history: list[ConversationTurn] | None = None,
    ) -> str:
        """Rewrite ambiguous query into a standalone query using conversation history."""
        cleaned_raw = raw_query.strip()
        history_turns = history or []

        # Fast heuristic bypass
        if self.should_bypass_rewriting(cleaned_raw, history_turns):
            return cleaned_raw

        try:
            rewritten = await asyncio.wait_for(
                self._invoke_llm_rewrite(cleaned_raw, history_turns),
                timeout=self.timeout_seconds,
            )
            cleaned_rewritten = self._sanitize_rewritten_query(rewritten, cleaned_raw)
            return cleaned_rewritten
        except asyncio.TimeoutError:
            logger.warning(
                "QueryRewriter timed out after %.2fs for query: '%s'. Falling back to raw query.",
                self.timeout_seconds,
                cleaned_raw,
            )
            return cleaned_raw
        except Exception as exc:
            logger.warning(
                "QueryRewriter failed for query '%s' with error: %s. Falling back to raw query.",
                cleaned_raw,
                exc,
            )
            return cleaned_raw

    async def _invoke_llm_rewrite(
        self,
        query: str,
        history: list[ConversationTurn],
    ) -> str:
        """Invoke configured LLM backend or custom callable."""
        history_formatted = [
            {"role": turn.role, "content": turn.content}
            for turn in history[-self.settings.max_history_turns:]
        ]

        if self._custom_llm_callable is not None:
            return await self._custom_llm_callable(query, history_formatted)

        # If LLM API key is not configured or provider is mock, use fallback heuristic
        if not self.settings.llm_api_key or self.settings.llm_provider == "mock":
            return self._heuristic_coreference_resolution(query, history)

        # Call OpenAI-compatible chat completion endpoint via httpx
        base_url = self.settings.llm_base_url or "https://api.openai.com/v1"
        url = f"{base_url.rstrip('/')}/chat/completions"

        system_prompt = (
            "Bạn là trợ lý Contextual Query Rewriter cho hệ thống Enterprise Knowledge Agent. "
            "Dựa trên lịch sử hội thoại gần nhất và câu hỏi hiện tại, hãy viết lại câu hỏi thành "
            "MỘT câu hỏi hoàn chỉnh, độc lập, đầy đủ ngữ cảnh để tìm kiếm thông tin chính xác. "
            "Thay thế toàn bộ đại từ chỉ định (nó, đó, này, họ, it, that...) bằng thực thể cụ thể. "
            "KHÔNG giải thích, KHÔNG thêm lời chào, chỉ trả về duy nhất câu hỏi đã viết lại."
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for turn in history_formatted:
            messages.append(turn)
        messages.append({"role": "user", "content": f"Hãy viết lại câu hỏi sau: {query}"})

        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model_name,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return str(data["choices"][0]["message"]["content"]).strip()

    def _sanitize_rewritten_query(self, rewritten: str, original: str) -> str:
        """Strip surrounding quotation marks and validate output."""
        result = rewritten.strip()
        # Remove surrounding quotes if model wrapped response in quotes
        if (result.startswith('"') and result.endswith('"')) or (
            result.startswith("'") and result.endswith("'")
        ):
            result = result[1:-1].strip()

        # If model returned empty or refused, retain original
        if not result or len(result) < 3:
            return original
        return result

    def _heuristic_coreference_resolution(
        self,
        query: str,
        history: list[ConversationTurn],
    ) -> str:
        """Deterministic rule-based coreference fallback when LLM API is unavailable."""
        if not history:
            return query

        # Find the last user turn or assistant turn that introduced a substantive entity
        last_turn_content = ""
        for turn in reversed(history):
            if turn.content:
                last_turn_content = turn.content.strip()
                break

        if not last_turn_content:
            return query

        # Detect topic keywords in last turn (e.g. "nghỉ phép", "bảo hiểm", "lương")
        common_topics = [
            "nghỉ phép", "bảo hiểm", "lương", "thưởng", "công tác phí",
            "thử việc", "hợp đồng", "thẻ xe", "trang thiết bị", "leave",
            "insurance", "salary", "bonus", "travel allowance",
        ]
        detected_topic = None
        for topic in common_topics:
            if topic in last_turn_content.lower():
                detected_topic = topic
                break

        if detected_topic and PRONOUN_PATTERN.search(query):
            # Replace demonstratives with detected topic
            resolved = PRONOUN_PATTERN.sub(detected_topic, query)
            return resolved.strip()

        return query
