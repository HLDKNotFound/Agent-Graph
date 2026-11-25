"""Data models for multi-turn conversation history and session turns."""

from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, Field, ConfigDict
from backend.query.types.intent import IntentLabel


class ConversationTurn(BaseModel):
    """Represents a single conversational turn (user question or assistant answer)."""

    model_config = ConfigDict(frozen=True)

    turn_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique identifier for this conversational turn",
    )
    role: str = Field(
        ...,
        description="Role of the turn participant ('user' or 'assistant')",
    )
    content: str = Field(
        ...,
        min_length=1,
        description="Text content of the message",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the conversational turn",
    )
    intent_label: IntentLabel | None = Field(
        default=None,
        description="Classified intent label if role is user",
    )
    rewritten_query: str | None = Field(
        default=None,
        description="Coreference-resolved rewritten query if applicable",
    )
