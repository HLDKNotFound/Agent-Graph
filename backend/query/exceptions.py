"""Domain-specific exception hierarchy for query intelligence and routing operations."""


class QueryRouterError(Exception):
    """Base exception for all query intelligence, rewriting, and intent routing failures."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class LowConfidenceIntentError(QueryRouterError):
    """Raised when classified intent falls below acceptable confidence threshold."""

    def __init__(self, intent: str, confidence: float, threshold: float, message: str | None = None) -> None:
        msg = (
            message
            or f"Intent '{intent}' classification confidence {confidence:.2f} is below minimum threshold {threshold:.2f}."
        )
        super().__init__(msg, details={"intent": intent, "confidence": confidence, "threshold": threshold})
        self.intent = intent
        self.confidence = confidence
        self.threshold = threshold


class RoutingTimeoutError(QueryRouterError):
    """Raised when intent classification or routing exceeds the configured SLA latency budget."""

    def __init__(self, elapsed_ms: float, timeout_ms: float, message: str | None = None) -> None:
        msg = message or f"Query routing exceeded latency SLA: elapsed {elapsed_ms:.1f}ms, budget {timeout_ms:.1f}ms."
        super().__init__(msg, details={"elapsed_ms": elapsed_ms, "timeout_ms": timeout_ms})
        self.elapsed_ms = elapsed_ms
        self.timeout_ms = timeout_ms


class RewritingError(QueryRouterError):
    """Raised when coreference resolution or contextual query rewriting fails."""

    def __init__(self, query: str, reason: str, details: dict | None = None) -> None:
        super().__init__(f"Failed to rewrite query '{query}': {reason}", details=details)
        self.query = query
        self.reason = reason


class ConversationHistoryError(QueryRouterError):
    """Raised when loading or persisting multi-turn conversation history encounters an error."""

    def __init__(self, conversation_id: str, reason: str, details: dict | None = None) -> None:
        super().__init__(f"Conversation history error for '{conversation_id}': {reason}", details=details)
        self.conversation_id = conversation_id
        self.reason = reason
