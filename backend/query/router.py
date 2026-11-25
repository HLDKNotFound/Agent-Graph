"""Unified First-Stage Query Router Gate orchestrating history, rewriting, and intent classification."""

import logging
import time
from uuid import UUID, uuid4

from backend.query.config import QuerySettings, get_query_settings
from backend.query.history.store import (
    BaseConversationHistoryStore,
    get_conversation_history_store,
)
from backend.query.history.types import ConversationTurn
from backend.query.intent.classifier import IntentClassifier
from backend.query.rewriting.rewriter import QueryRewriter
from backend.query.types.classification import ClassifiedQuery
from backend.query.types.context import UserQueryContext

logger = logging.getLogger(__name__)


class QueryRouter:
    """The first-stage gateway for all incoming user messages in the Enterprise Knowledge Agent.

    Orchestrates:
    1. Multi-turn conversation history retrieval from tenant-partitioned storage.
    2. Contextual coreference query rewriting.
    3. Two-tier 5-class intent classification.
    4. Session turn history persistence.
    5. SLA latency budgeting and metric tracking.
    """

    def __init__(
        self,
        settings: QuerySettings | None = None,
        history_store: BaseConversationHistoryStore | None = None,
        rewriter: QueryRewriter | None = None,
        classifier: IntentClassifier | None = None,
    ) -> None:
        self.settings = settings or get_query_settings()
        self.history_store = history_store or get_conversation_history_store(
            settings=self.settings, prefer_redis=True
        )
        self.rewriter = rewriter or QueryRewriter(settings=self.settings)
        self.classifier = classifier or IntentClassifier(settings=self.settings)

    async def route(
        self,
        raw_query: str,
        conversation_id: str | None = None,
        context: UserQueryContext | None = None,
    ) -> ClassifiedQuery:
        """Route raw user query into an actionable, classified, and contextualized search query.

        Args:
            raw_query: The incoming text inquiry from the user.
            conversation_id: Optional UUID/string identifying the active conversation session.
            context: Authenticated user context containing tenant_id, user_id, and RBAC roles.

        Returns:
            Immutable ClassifiedQuery with intent, standalone rewritten query, and routing flags.
        """
        start_time = time.perf_counter()
        cleaned_raw = raw_query.strip()
        turn_id = str(uuid4())

        # Resolve tenant identifier for tenant-isolated history
        tenant_id: str | UUID = "default_tenant"
        if context is not None:
            tenant_id = context.tenant_id

        # -----------------------------------------------------------------
        # Step 1: Load Conversation History
        # -----------------------------------------------------------------
        history: list[ConversationTurn] = []
        if conversation_id:
            try:
                history = await self.history_store.get_recent_turns(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    max_turns=self.settings.max_history_turns,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to retrieve conversation history for conv '%s': %s",
                    conversation_id,
                    exc,
                )

        # -----------------------------------------------------------------
        # Step 2: Coreference Resolution & Query Rewriting
        # -----------------------------------------------------------------
        rewritten_query = await self.rewriter.rewrite(cleaned_raw, history=history)

        # -----------------------------------------------------------------
        # Step 3: Intent Classification (Two-Tier)
        # -----------------------------------------------------------------
        intent_res = await self.classifier.classify(rewritten_query, context=context)

        # -----------------------------------------------------------------
        # Step 4: Persist User Turn to Conversation History
        # -----------------------------------------------------------------
        if conversation_id:
            try:
                turn = ConversationTurn(
                    turn_id=turn_id,
                    role="user",
                    content=cleaned_raw,
                    intent_label=intent_res.intent,
                    rewritten_query=rewritten_query,
                )
                await self.history_store.append_turn(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    turn=turn,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to append turn to conversation '%s': %s",
                    conversation_id,
                    exc,
                )

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        if elapsed_ms > self.settings.router_timeout_seconds * 1000.0:
            logger.warning(
                "Query routing exceeded latency SLA: %.1fms (budget: %.1fms) for query: '%s'",
                elapsed_ms,
                self.settings.router_timeout_seconds * 1000.0,
                cleaned_raw,
            )

        return ClassifiedQuery(
            raw_query=cleaned_raw,
            rewritten_query=rewritten_query,
            intent=intent_res.intent,
            confidence=intent_res.confidence,
            reasoning=intent_res.reasoning,
            conversation_id=conversation_id,
            turn_id=turn_id,
            latency_ms=round(elapsed_ms, 2),
            requires_rag=intent_res.requires_rag,
            needs_multi_hop=intent_res.needs_multi_hop,
            target_departments=intent_res.target_departments,
        )
