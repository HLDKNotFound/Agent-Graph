"""Unit tests for Phase 3 Task 01: Query Intelligence Architecture & Shared Data Contracts."""

import pytest
from uuid import uuid4
from pydantic import ValidationError

from backend.query.config import QuerySettings, get_query_settings
from backend.query.exceptions import (
    ConversationHistoryError,
    LowConfidenceIntentError,
    QueryRouterError,
    RewritingError,
    RoutingTimeoutError,
)
from backend.query.types import (
    ClassifiedQuery,
    IntentLabel,
    UserQueryContext,
)


def test_intent_label_attributes():
    """Verify that IntentLabel correctly defines RAG, multi-hop, and web flags."""
    assert IntentLabel.FACTUAL_RETRIEVAL.requires_rag is True
    assert IntentLabel.FACTUAL_RETRIEVAL.needs_multi_hop is False
    assert IntentLabel.FACTUAL_RETRIEVAL.triggers_web_search is False

    assert IntentLabel.MULTI_DOC_COMPARISON.requires_rag is True
    assert IntentLabel.MULTI_DOC_COMPARISON.needs_multi_hop is True
    assert IntentLabel.MULTI_DOC_COMPARISON.triggers_web_search is False

    assert IntentLabel.TABLE_LOOKUP.requires_rag is True
    assert IntentLabel.TABLE_LOOKUP.needs_multi_hop is False
    assert IntentLabel.TABLE_LOOKUP.triggers_web_search is False

    assert IntentLabel.GENERAL_CHAT.requires_rag is False
    assert IntentLabel.GENERAL_CHAT.needs_multi_hop is False
    assert IntentLabel.GENERAL_CHAT.triggers_web_search is False

    assert IntentLabel.OUT_OF_DOMAIN.requires_rag is False
    assert IntentLabel.OUT_OF_DOMAIN.needs_multi_hop is False
    assert IntentLabel.OUT_OF_DOMAIN.triggers_web_search is True


def test_user_query_context():
    """Verify UserQueryContext validation and immutability."""
    tenant_id = uuid4()
    user_id = uuid4()
    ctx = UserQueryContext(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=["ENGINEER"],
        departments=["TECH"],
        locale="vi",
    )
    assert ctx.tenant_id == tenant_id
    assert ctx.user_id == user_id
    assert ctx.roles == ["ENGINEER"]
    assert ctx.locale == "vi"

    # Verify immutability
    with pytest.raises(ValidationError):
        ctx.locale = "en"  # type: ignore


def test_classified_query_validation_and_immutability():
    """Verify ClassifiedQuery constraints, confidence range, and immutability."""
    cq = ClassifiedQuery(
        raw_query="Chính sách nghỉ phép là gì?",
        rewritten_query="Chính sách nghỉ phép năm 2024 là gì?",
        intent=IntentLabel.FACTUAL_RETRIEVAL,
        confidence=0.95,
        reasoning="Inquiry about leave policy",
        conversation_id="conv-123",
        turn_id="turn-1",
        latency_ms=15.4,
    )
    assert cq.intent == IntentLabel.FACTUAL_RETRIEVAL
    assert cq.requires_rag is True
    assert cq.needs_multi_hop is False
    assert cq.confidence == 0.95

    # Verify confidence bounds (must be in [0.0, 1.0])
    with pytest.raises(ValidationError):
        ClassifiedQuery(
            raw_query="Test",
            rewritten_query="Test",
            intent=IntentLabel.GENERAL_CHAT,
            confidence=1.5,  # Exceeds 1.0
        )

    with pytest.raises(ValidationError):
        ClassifiedQuery(
            raw_query="Test",
            rewritten_query="Test",
            intent=IntentLabel.GENERAL_CHAT,
            confidence=-0.1,  # Below 0.0
        )

    # Verify immutability
    with pytest.raises(ValidationError):
        cq.raw_query = "Changed"  # type: ignore


def test_typed_exceptions():
    """Verify typed exceptions hierarchy and payload extraction."""
    err = LowConfidenceIntentError(intent="FACTUAL_RETRIEVAL", confidence=0.45, threshold=0.60)
    assert isinstance(err, QueryRouterError)
    assert err.confidence == 0.45
    assert err.threshold == 0.60
    assert "0.45" in str(err)

    t_err = RoutingTimeoutError(elapsed_ms=150.0, timeout_ms=120.0)
    assert isinstance(t_err, QueryRouterError)
    assert t_err.elapsed_ms == 150.0

    rw_err = RewritingError(query="nó là gì", reason="LLM timeout")
    assert isinstance(rw_err, QueryRouterError)

    ch_err = ConversationHistoryError(conversation_id="c1", reason="Redis unreachable")
    assert isinstance(ch_err, QueryRouterError)


def test_query_settings_dynamic_defaults():
    """Verify that QuerySettings resolves defaults cleanly adhering to Zero Hardcoding."""
    settings = get_query_settings()
    assert isinstance(settings, QuerySettings)
    assert settings.conversation_ttl_seconds > 0
    assert settings.max_history_turns > 0
    assert settings.rewriter_timeout_seconds > 0
    assert 0.0 < settings.intent_fast_path_confidence_threshold <= 1.0
    assert 0.0 < settings.intent_low_confidence_threshold < 1.0
