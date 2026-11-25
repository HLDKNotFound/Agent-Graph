"""Shared data contracts and types for query intelligence."""

from backend.query.types.classification import ClassifiedQuery
from backend.query.types.context import UserQueryContext
from backend.query.types.intent import IntentLabel

__all__ = [
    "IntentLabel",
    "UserQueryContext",
    "ClassifiedQuery",
]
