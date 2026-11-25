"""Unit tests for Phase 3 Task 04: Two-Tier 5-Class Intent Classification Engine."""

import asyncio
from typing import Any
import pytest

from backend.query.config import QuerySettings
from backend.query.intent.classifier import IntentClassificationResult, IntentClassifier
from backend.query.intent.heuristics import FastPathHeuristics
from backend.query.types.intent import IntentLabel


def test_fast_path_general_chat():
    """Verify Tier-1 fast-path heuristic correctly classifies conversational pleasantries."""
    heuristics = FastPathHeuristics()

    queries = [
        "Xin chào bạn",
        "Chào bot nhé",
        "Cảm ơn em nhiều",
        "Thanks a lot!",
        "Hello! Who are you?",
        "Bạn có thể làm gì?",
    ]
    for q in queries:
        match = heuristics.evaluate(q)
        assert match.matched is True, f"Failed on: {q}"
        assert match.intent == IntentLabel.GENERAL_CHAT
        assert match.confidence >= 0.98


def test_fast_path_out_of_domain():
    """Verify Tier-1 fast-path heuristic correctly catches external web inquiries."""
    heuristics = FastPathHeuristics()

    queries = [
        "Thời tiết Hà Nội hôm nay thế nào?",
        "Dự báo thời tiết ngày mai ra sao?",
        "Thủ đô của nước Pháp là gì?",
        "Ai vô địch World Cup 2022?",
        "What is the capital of Australia?",
        "Weather forecast in Tokyo today",
    ]
    for q in queries:
        match = heuristics.evaluate(q)
        assert match.matched is True, f"Failed on: {q}"
        assert match.intent == IntentLabel.OUT_OF_DOMAIN
        assert match.confidence >= 0.98


def test_fast_path_table_lookup():
    """Verify Tier-1 fast-path heuristic detects spreadsheet and matrix lookups."""
    heuristics = FastPathHeuristics()

    queries = [
        "Tra cứu bảng lương phòng IT",
        "Xem sheet phân bổ ngân sách Q3",
        "Bảng số liệu doanh thu Q2",
        "Salary table for level 3 engineers",
    ]
    for q in queries:
        match = heuristics.evaluate(q)
        assert match.matched is True, f"Failed on: {q}"
        assert match.intent == IntentLabel.TABLE_LOOKUP
        assert match.confidence >= 0.98


def test_fast_path_multi_doc_comparison():
    """Verify Tier-1 fast-path heuristic detects comparative inquiries across departments."""
    heuristics = FastPathHeuristics()

    queries = [
        "So sánh chính sách nghỉ phép của HR và Sales",
        "Điểm khác biệt giữa quy định khối kỹ thuật và khối kinh doanh",
        "Compare policy between engineering and human resources",
    ]
    for q in queries:
        match = heuristics.evaluate(q)
        assert match.matched is True, f"Failed on: {q}"
        assert match.intent == IntentLabel.MULTI_DOC_COMPARISON
        assert match.confidence >= 0.98
        assert len(match.target_departments) >= 2


def test_department_extraction():
    """Verify extracted departments from query text."""
    heuristics = FastPathHeuristics()
    assert "HR" in heuristics.extract_departments("Chính sách nghỉ phép của phòng nhân sự")
    assert "ENGINEERING" in heuristics.extract_departments("Quy định làm việc khối kỹ thuật IT")
    assert "SALES" in heuristics.extract_departments("Chỉ tiêu doanh số khối kinh doanh sales")
    assert "FINANCE" in heuristics.extract_departments("Báo cáo ngân sách tài chính")


def test_two_tier_classifier_fast_path_execution():
    """Verify IntentClassifier returns sub-5ms result when Tier-1 matches."""
    async def _run():
        classifier = IntentClassifier()
        res = await classifier.classify("Xin chào")
        assert res.intent == IntentLabel.GENERAL_CHAT
        assert res.requires_rag is False
        assert res.confidence >= 0.98
    asyncio.run(_run())


def test_two_tier_classifier_custom_llm_callable():
    """Verify Tier-2 classification using custom LLM callable for complex queries."""
    async def _run():
        async def mock_llm(query: str) -> dict[str, Any]:
            return {
                "intent": "FACTUAL_RETRIEVAL",
                "confidence": 0.96,
                "requires_rag": True,
                "needs_multi_hop": False,
                "target_departments": ["HR"],
                "reasoning": "Standard policy retrieval inquiry",
            }

        classifier = IntentClassifier(llm_callable=mock_llm)
        res = await classifier.classify("Quy trình xin cấp thẻ xe nhân viên")
        assert res.intent == IntentLabel.FACTUAL_RETRIEVAL
        assert res.confidence == 0.96
        assert res.requires_rag is True
        assert res.needs_multi_hop is False
        assert "HR" in res.target_departments
    asyncio.run(_run())


def test_classifier_low_confidence_fallback():
    """Verify that confidence < 0.60 triggers safe fallback to FACTUAL_RETRIEVAL."""
    async def _run():
        async def ambiguous_llm(query: str) -> dict[str, Any]:
            return {
                "intent": "GENERAL_CHAT",
                "confidence": 0.40,  # Below 0.60 threshold
                "requires_rag": False,
                "needs_multi_hop": False,
                "reasoning": "Uncertain about user intent",
            }

        classifier = IntentClassifier(llm_callable=ambiguous_llm)
        res = await classifier.classify("Có ai ở đó không?")
        # Low confidence must trigger conservative fallback
        assert res.intent == IntentLabel.FACTUAL_RETRIEVAL
        assert res.requires_rag is True
        assert "Low confidence" in str(res.reasoning)
    asyncio.run(_run())


def test_classifier_timeout_fallback():
    """Verify that classifier latency exceeding timeout triggers conservative fallback."""
    async def _run():
        async def hanging_llm(query: str) -> dict[str, Any]:
            await asyncio.sleep(0.5)
            return {"intent": "GENERAL_CHAT", "confidence": 0.99}

        settings = QuerySettings(ROUTER_TIMEOUT_SECONDS=0.05)
        classifier = IntentClassifier(settings=settings, llm_callable=hanging_llm)

        # Complex query that misses Tier-1 heuristics
        res = await classifier.classify("Hỏi về thủ tục điều chuyển vị trí công tác")
        assert res.intent == IntentLabel.FACTUAL_RETRIEVAL
        assert res.requires_rag is True
        assert "timed out" in str(res.reasoning).lower()
    asyncio.run(_run())
