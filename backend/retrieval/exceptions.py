"""Typed domain exception hierarchy for the hybrid retrieval subsystem."""

from __future__ import annotations


class RetrievalError(Exception):
    """Base domain exception for all retrieval and reranking failures."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class RetrievalSecurityViolationError(RetrievalError):
    """Raised when a query attempts to bypass multi-tenant boundaries or RBAC rules."""
    pass


class DenseRetrievalError(RetrievalError):
    """Raised when Qdrant dense vector search fails or times out."""
    pass


class SparseRetrievalError(RetrievalError):
    """Raised when PostgreSQL GIN lexical search fails or encounters database errors."""
    pass


class RerankerTimeoutError(RetrievalError):
    """Raised when Cross-Encoder reranking exceeds configured SLA timeout."""
    pass


class RerankerInferenceError(RetrievalError):
    """Raised when Cross-Encoder model encounters compute/VRAM errors during inference."""
    pass


class EmptyRetrievalResultWarning(UserWarning):
    """Warning emitted when candidate retrieval yields zero passages matching criteria."""
    pass
