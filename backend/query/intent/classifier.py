"""Two-tier intent classification engine with fast heuristics and structured LLM routing."""

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable

import httpx
from pydantic import BaseModel, Field, ConfigDict

from backend.query.config import QuerySettings, get_query_settings
from backend.query.intent.heuristics import FastPathHeuristics
from backend.query.types.context import UserQueryContext
from backend.query.types.intent import IntentLabel

logger = logging.getLogger(__name__)


class IntentClassificationResult(BaseModel):
    """Structured result produced by the Intent Classification Engine."""

    model_config = ConfigDict(frozen=True)

    intent: IntentLabel = Field(
        ...,
        description="The primary identified user intent among the 5 supported classes.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score of the classification decision.",
    )
    requires_rag: bool = Field(
        ...,
        description="Flag indicating if vector DB search and document retrieval are needed.",
    )
    needs_multi_hop: bool = Field(
        ...,
        description="True if query requires sub-goal decomposition and multiple search hops.",
    )
    target_departments: list[str] = Field(
        default_factory=list,
        description="Extracted department filters (e.g. ['HR', 'SALES', 'ENGINEERING']).",
    )
    reasoning: str | None = Field(
        default=None,
        description="Brief justification for the classification decision.",
    )


class IntentClassifier:
    """Evaluates incoming inquiries across 5 canonical intents using a 2-tier architecture.

    Tier 1: Sub-5ms fast-path heuristic regex patterns for obvious intents.
    Tier 2: Structured JSON LLM classification with Pydantic validation.
    Fallback: Conservative fallback to FACTUAL_RETRIEVAL upon ambiguity or timeouts.
    """

    def __init__(
        self,
        settings: QuerySettings | None = None,
        llm_callable: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.settings = settings or get_query_settings()
        self.heuristics = FastPathHeuristics(
            confidence_threshold=self.settings.intent_fast_path_confidence_threshold
        )
        self.timeout_seconds = self.settings.router_timeout_seconds
        self.low_confidence_threshold = self.settings.intent_low_confidence_threshold
        self._custom_llm_callable = llm_callable

    async def classify(
        self,
        query: str,
        context: UserQueryContext | None = None,
    ) -> IntentClassificationResult:
        """Execute two-tier classification pipeline."""
        cleaned_query = query.strip()
        extracted_depts = self.heuristics.extract_departments(cleaned_query)

        # ----------------------------------------------------
        # Tier 1: Fast-Path Heuristics (< 5ms)
        # ----------------------------------------------------
        fast_result = self.heuristics.evaluate(cleaned_query)
        if fast_result.matched and fast_result.intent is not None:
            if fast_result.confidence >= self.settings.intent_fast_path_confidence_threshold:
                logger.debug(
                    "Query '%s' classified by FastPath rule '%s' as %s (confidence: %.2f)",
                    cleaned_query,
                    fast_result.rule_name,
                    fast_result.intent.value,
                    fast_result.confidence,
                )
                return IntentClassificationResult(
                    intent=fast_result.intent,
                    confidence=fast_result.confidence,
                    requires_rag=fast_result.intent.requires_rag,
                    needs_multi_hop=fast_result.intent.needs_multi_hop,
                    target_departments=fast_result.target_departments,
                    reasoning=fast_result.reasoning,
                )

        # ----------------------------------------------------
        # Tier 2: Structured LLM Classification
        # ----------------------------------------------------
        try:
            llm_result = await asyncio.wait_for(
                self._invoke_llm_classification(cleaned_query, extracted_depts, context),
                timeout=self.timeout_seconds,
            )

            # Check confidence guardrail
            if llm_result.confidence < self.low_confidence_threshold:
                logger.warning(
                    "Intent '%s' confidence (%.2f) below threshold (%.2f) for query '%s'. Falling back to FACTUAL_RETRIEVAL.",
                    llm_result.intent.value,
                    llm_result.confidence,
                    self.low_confidence_threshold,
                    cleaned_query,
                )
                return self._create_safe_fallback_result(
                    cleaned_query,
                    extracted_depts,
                    f"Low confidence ({llm_result.confidence:.2f}) from classifier: {llm_result.reasoning}",
                )

            return llm_result

        except asyncio.TimeoutError:
            logger.warning(
                "Intent classification timed out after %.2fs for query: '%s'. Applying conservative fallback.",
                self.timeout_seconds,
                cleaned_query,
            )
            return self._create_safe_fallback_result(
                cleaned_query, extracted_depts, "Classification timed out"
            )
        except Exception as exc:
            logger.warning(
                "Intent classification encountered error for query '%s': %s. Applying conservative fallback.",
                cleaned_query,
                exc,
            )
            return self._create_safe_fallback_result(
                cleaned_query, extracted_depts, f"Classification exception: {exc}"
            )

    async def _invoke_llm_classification(
        self,
        query: str,
        extracted_depts: list[str],
        context: UserQueryContext | None = None,
    ) -> IntentClassificationResult:
        """Invoke custom callable or OpenAI-compatible endpoint with JSON validation."""
        if self._custom_llm_callable is not None:
            raw_dict = await self._custom_llm_callable(query)
            return self._parse_and_validate_dict(raw_dict, extracted_depts)

        # If LLM API key is not configured or provider is mock, use intelligent semantic heuristic fallback
        if not self.settings.llm_api_key or self.settings.llm_provider == "mock":
            return self._semantic_fallback_classifier(query, extracted_depts)

        system_prompt = (
            "Bạn là Router Layer chuyên phân loại ý định (Intent Classification) cho hệ thống Enterprise Knowledge Agent.\n"
            "Nhiệm vụ của bạn là phân loại câu hỏi của người dùng vào chính xác 1 trong 5 loại sau:\n\n"
            "1. FACTUAL_RETRIEVAL: Tìm kiếm thông tin hoặc quy định cụ thể trong tài liệu nội bộ (chính sách, quy chế, hướng dẫn, quyền lợi).\n"
            "   requires_rag: true, needs_multi_hop: false\n"
            "2. MULTI_DOC_COMPARISON: So sánh, tổng hợp hoặc đối chiếu giữa nhiều tài liệu, phòng ban, chính sách hoặc đối tượng khác nhau.\n"
            "   requires_rag: true, needs_multi_hop: true\n"
            "3. TABLE_LOOKUP: Tra cứu thông tin dạng bảng, ma trận số liệu, bậc lương, ngân sách, bảng biểu kỹ thuật.\n"
            "   requires_rag: true, needs_multi_hop: false\n"
            "4. GENERAL_CHAT: Chào hỏi xã giao, cảm ơn, hỏi thăm thông thường hoặc câu hỏi về khả năng của trợ lý.\n"
            "   requires_rag: false, needs_multi_hop: false\n"
            "5. OUT_OF_DOMAIN: Câu hỏi hoàn toàn ngoài phạm vi doanh nghiệp/công việc nội bộ (thời tiết, thể thao, địa lý, chính trị, kiến thức phổ thông).\n"
            "   requires_rag: false, needs_multi_hop: false\n\n"
            "Trả về DUY NHẤT một chuỗi JSON chuẩn theo cấu trúc:\n"
            "{\n"
            '  "intent": "FACTUAL_RETRIEVAL" | "MULTI_DOC_COMPARISON" | "TABLE_LOOKUP" | "GENERAL_CHAT" | "OUT_OF_DOMAIN",\n'
            '  "confidence": float,\n'
            '  "requires_rag": boolean,\n'
            '  "needs_multi_hop": boolean,\n'
            '  "target_departments": string[],\n'
            '  "reasoning": string\n'
            "}"
        )

        base_url = self.settings.llm_base_url or "https://api.openai.com/v1"
        url = f"{base_url.rstrip('/')}/chat/completions"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Hãy phân loại câu hỏi sau: {query}"},
        ]
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model_name,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw_content = str(data["choices"][0]["message"]["content"]).strip()
            parsed_json = json.loads(raw_content)
            return self._parse_and_validate_dict(parsed_json, extracted_depts)

    def _parse_and_validate_dict(
        self,
        data: dict[str, Any],
        extracted_depts: list[str],
    ) -> IntentClassificationResult:
        """Parse raw dictionary into validated IntentClassificationResult."""
        raw_intent = data.get("intent", "FACTUAL_RETRIEVAL")
        try:
            intent_enum = IntentLabel(raw_intent)
        except ValueError:
            intent_enum = IntentLabel.FACTUAL_RETRIEVAL

        confidence = float(data.get("confidence", 0.85))
        confidence = max(0.0, min(1.0, confidence))

        requires_rag = data.get("requires_rag", intent_enum.requires_rag)
        needs_multi_hop = data.get("needs_multi_hop", intent_enum.needs_multi_hop)

        target_depts = data.get("target_departments")
        if not target_depts or not isinstance(target_depts, list):
            target_depts = extracted_depts
        else:
            # Combine extracted departments
            target_depts = list(set(target_depts + extracted_depts))

        reasoning = data.get("reasoning", "Classified by Tier-2 engine")

        return IntentClassificationResult(
            intent=intent_enum,
            confidence=confidence,
            requires_rag=requires_rag,
            needs_multi_hop=needs_multi_hop,
            target_departments=target_depts,
            reasoning=reasoning,
        )

    def _create_safe_fallback_result(
        self,
        query: str,
        extracted_depts: list[str],
        reason: str,
    ) -> IntentClassificationResult:
        """Construct deterministic safe fallback to FACTUAL_RETRIEVAL to avoid dropping valid queries."""
        return IntentClassificationResult(
            intent=IntentLabel.FACTUAL_RETRIEVAL,
            confidence=0.70,
            requires_rag=True,
            needs_multi_hop=False,
            target_departments=extracted_depts,
            reasoning=f"Safe fallback to FACTUAL_RETRIEVAL: {reason}",
        )

    def _semantic_fallback_classifier(
        self,
        query: str,
        extracted_depts: list[str],
    ) -> IntentClassificationResult:
        """Deterministic pattern-based fallback classifier when live LLM API is offline."""
        q_lower = query.lower()

        # Check OUT_OF_DOMAIN
        from backend.query.intent.heuristics import OUT_OF_DOMAIN_PATTERNS, GENERAL_CHAT_EXACT
        if OUT_OF_DOMAIN_PATTERNS.search(query):
            return IntentClassificationResult(
                intent=IntentLabel.OUT_OF_DOMAIN,
                confidence=0.98,
                requires_rag=False,
                needs_multi_hop=False,
                target_departments=extracted_depts,
                reasoning="Identified out of domain external query in semantic fallback",
            )

        # Check GENERAL_CHAT
        if GENERAL_CHAT_EXACT.search(query):
            return IntentClassificationResult(
                intent=IntentLabel.GENERAL_CHAT,
                confidence=0.98,
                requires_rag=False,
                needs_multi_hop=False,
                target_departments=extracted_depts,
                reasoning="Identified general conversational chat in semantic fallback",
            )

        # Multi-document comparison check
        if any(term in q_lower for term in ["so sánh", "khác gì", "khác nhau", "compare", "difference", "vs", "versus"]):
            return IntentClassificationResult(
                intent=IntentLabel.MULTI_DOC_COMPARISON,
                confidence=0.92,
                requires_rag=True,
                needs_multi_hop=True,
                target_departments=extracted_depts,
                reasoning="Identified comparative keywords between policies or entities",
            )

        # Tabular / matrix lookup check
        tabular_terms = [
            "bảng lương", "bậc lương", "thang lương", "dải lương", "bảng", "ma trận",
            "sheet", "spreadsheet", "worksheet", "pay scale", "salary table", "salary matrix",
            "salary bracket", "summary table", "doanh thu q", "ngân sách q"
        ]
        if any(term in q_lower for term in tabular_terms):
            return IntentClassificationResult(
                intent=IntentLabel.TABLE_LOOKUP,
                confidence=0.90,
                requires_rag=True,
                needs_multi_hop=False,
                target_departments=extracted_depts,
                reasoning="Identified structured numerical or tabular query pattern",
            )

        # Default enterprise question -> FACTUAL_RETRIEVAL
        return IntentClassificationResult(
            intent=IntentLabel.FACTUAL_RETRIEVAL,
            confidence=0.95,
            requires_rag=True,
            needs_multi_hop=False,
            target_departments=extracted_depts,
            reasoning="Identified internal enterprise factual policy inquiry",
        )
