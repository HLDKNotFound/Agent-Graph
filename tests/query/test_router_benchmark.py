"""Benchmark and Integration tests for Phase 3 Task 05: First-Stage Router Orchestration."""

import asyncio
import json
import logging
from pathlib import Path
import time
from uuid import uuid4
import pytest
import numpy as np

from backend.query.history.store import InMemoryConversationHistoryStore
from backend.query.history.types import ConversationTurn
from backend.query.intent.classifier import IntentClassifier
from backend.query.rewriting.rewriter import QueryRewriter
from backend.query.router import QueryRouter
from backend.query.types.context import UserQueryContext
from backend.query.types.intent import IntentLabel

logger = logging.getLogger(__name__)


def test_end_to_end_router_multi_turn_flow():
    """Verify that QueryRouter resolves pronouns across multi-turn conversation and routes intents."""
    async def _run():
        store = InMemoryConversationHistoryStore()
        # Custom mock LLM for rewriting
        async def mock_llm_rewriter(q: str, hist: list[dict[str, str]]) -> str:
            if "nó" in q.lower():
                return "Thời gian nghỉ của chính sách thai sản là bao lâu?"
            return q

        rewriter = QueryRewriter(llm_callable=mock_llm_rewriter)
        classifier = IntentClassifier()
        router = QueryRouter(history_store=store, rewriter=rewriter, classifier=classifier)

        tenant_id = uuid4()
        user_id = uuid4()
        ctx = UserQueryContext(tenant_id=tenant_id, user_id=user_id)
        conv_id = "conv-e2e-1"

        # Turn 1: Initial substantive question
        res1 = await router.route(
            raw_query="Chính sách nghỉ thai sản của công ty là gì?",
            conversation_id=conv_id,
            context=ctx,
        )
        assert res1.intent == IntentLabel.FACTUAL_RETRIEVAL
        assert res1.requires_rag is True
        assert res1.needs_multi_hop is False

        # Add assistant reply to simulate conversation turn
        await store.append_turn(
            tenant_id=tenant_id,
            conversation_id=conv_id,
            turn=ConversationTurn(role="assistant", content="Chính sách thai sản quy định nghỉ 6 tháng."),
        )

        # Turn 2: Referential pronoun question
        res2 = await router.route(
            raw_query="Thời gian nghỉ của nó là bao lâu?",
            conversation_id=conv_id,
            context=ctx,
        )
        assert res2.intent == IntentLabel.FACTUAL_RETRIEVAL
        assert "thai sản" in res2.rewritten_query.lower()
        assert res2.requires_rag is True

        # Verify history contains both user turns
        turns = await store.get_recent_turns(tenant_id, conv_id)
        assert len(turns) == 3  # Turn 1 User, Turn 1 Assistant, Turn 2 User
    asyncio.run(_run())


def test_router_single_hop_vs_multihop_vs_table_chat_web():
    """Verify routing decisions for all 5 intent classes through the router."""
    async def _run():
        router = QueryRouter()

        # 1. FACTUAL_RETRIEVAL
        r1 = await router.route("Quy trình xin cấp mới thẻ nhân viên")
        assert r1.intent == IntentLabel.FACTUAL_RETRIEVAL
        assert r1.requires_rag is True
        assert r1.needs_multi_hop is False

        # 2. MULTI_DOC_COMPARISON
        r2 = await router.route("So sánh chính sách nghỉ phép của HR và Sales")
        assert r2.intent == IntentLabel.MULTI_DOC_COMPARISON
        assert r2.requires_rag is True
        assert r2.needs_multi_hop is True

        # 3. TABLE_LOOKUP
        r3 = await router.route("Tra cứu bảng lương phòng IT năm 2024")
        assert r3.intent == IntentLabel.TABLE_LOOKUP
        assert r3.requires_rag is True
        assert r3.needs_multi_hop is False

        # 4. GENERAL_CHAT
        r4 = await router.route("Xin chào bạn, chúc một ngày tốt lành!")
        assert r4.intent == IntentLabel.GENERAL_CHAT
        assert r4.requires_rag is False
        assert r4.needs_multi_hop is False

        # 5. OUT_OF_DOMAIN
        r5 = await router.route("Thời tiết Hà Nội hôm nay thế nào?")
        assert r5.intent == IntentLabel.OUT_OF_DOMAIN
        assert r5.requires_rag is False
        assert r5.intent.triggers_web_search is True
    asyncio.run(_run())


def test_intent_benchmark_accuracy_and_latency():
    """Benchmark QueryRouter on 100 curated enterprise queries across 5 intents.

    Quality Gate:
    1. Intent Classification Accuracy >= 95%
    2. Routing Latency P95 <= 120ms
    """
    async def _run():
        fixture_path = Path(__file__).resolve().parent.parent / "fixtures" / "intent_benchmark.json"
        assert fixture_path.exists(), f"Fixture not found at {fixture_path}"

        with open(fixture_path, "r", encoding="utf-8") as f:
            samples = json.load(f)

        assert len(samples) == 100, f"Expected 100 samples, got {len(samples)}"

        router = QueryRouter()
        tenant_id = uuid4()
        user_id = uuid4()
        ctx = UserQueryContext(tenant_id=tenant_id, user_id=user_id)

        correct_count = 0
        latencies_ms: list[float] = []
        errors = []

        for idx, sample in enumerate(samples):
            query = sample["query"]
            expected = sample["expected_intent"]

            t0 = time.perf_counter()
            result = await router.route(query, context=ctx)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies_ms.append(elapsed_ms)

            predicted = result.intent.value
            if predicted == expected:
                correct_count += 1
            else:
                errors.append({
                    "index": idx,
                    "query": query,
                    "expected": expected,
                    "predicted": predicted,
                    "reasoning": result.reasoning,
                })

        accuracy = correct_count / len(samples)
        p50 = float(np.percentile(latencies_ms, 50))
        p90 = float(np.percentile(latencies_ms, 90))
        p95 = float(np.percentile(latencies_ms, 95))
        p99 = float(np.percentile(latencies_ms, 99))
        mean_lat = float(np.mean(latencies_ms))

        print("\n" + "=" * 60)
        print("PHASE 3 INTENT ROUTER BENCHMARK RESULTS")
        print("=" * 60)
        print(f"Total Samples Evaluated: {len(samples)}")
        print(f"Correctly Classified:    {correct_count} / {len(samples)}")
        print(f"Accuracy:                {accuracy * 100:.2f}% (Target: >= 95.0%)")
        print(f"Mean Latency:            {mean_lat:.2f} ms")
        print(f"P50 Latency:             {p50:.2f} ms")
        print(f"P90 Latency:             {p90:.2f} ms")
        print(f"P95 Latency:             {p95:.2f} ms (SLA Target: <= 120.0 ms)")
        print(f"P99 Latency:             {p99:.2f} ms")
        print("=" * 60)

        if errors:
            print("Misclassified queries:")
            for err in errors:
                print(f"  - [{err['expected']} -> {err['predicted']}] '{err['query']}'")
            print("=" * 60)

        # Quality Gates
        assert accuracy >= 0.95, f"Accuracy {accuracy*100:.1f}% below target 95.0%"
        assert p95 <= 120.0, f"P95 Latency {p95:.1f}ms exceeds target 120.0ms"
    asyncio.run(_run())
