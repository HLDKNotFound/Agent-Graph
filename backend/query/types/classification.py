"""Data contract for classified and contextualized queries."""

from pydantic import BaseModel, Field, ConfigDict, model_validator
from backend.query.types.intent import IntentLabel


class ClassifiedQuery(BaseModel):
    """Immutable output model produced by the First-Stage Intent Router Gate."""

    model_config = ConfigDict(frozen=True)

    raw_query: str = Field(
        ...,
        min_length=1,
        description="Original query input as received from the user",
    )
    rewritten_query: str = Field(
        ...,
        min_length=1,
        description="Coreference-resolved standalone query for search retrieval",
    )
    intent: IntentLabel = Field(
        ...,
        description="Identified 5-class intent label",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score strictly within [0.0, 1.0]",
    )
    reasoning: str | None = Field(
        default=None,
        description="Brief justification or rule trigger explaining the routing decision",
    )
    conversation_id: str | None = Field(
        default=None,
        description="Optional active conversation identifier for multi-turn sessions",
    )
    turn_id: str | None = Field(
        default=None,
        description="Optional turn sequence or ID within the conversation",
    )
    latency_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Total elapsed routing latency in milliseconds",
    )
    requires_rag: bool = Field(
        default=False,
        description="Whether this query requires vector DB search or document retrieval",
    )
    needs_multi_hop: bool = Field(
        default=False,
        description="Whether this query requires iterative multi-hop LangGraph reasoning",
    )
    target_departments: list[str] = Field(
        default_factory=list,
        description="List of target departments detected in query (e.g. ['HR', 'Sales'])",
    )

    @model_validator(mode="before")
    @classmethod
    def populate_flags_from_intent(cls, data: dict) -> dict:
        """Derive requires_rag and needs_multi_hop defaults from intent if not explicitly set."""
        if isinstance(data, dict):
            intent_val = data.get("intent")
            if intent_val is not None:
                if isinstance(intent_val, str):
                    try:
                        intent_enum = IntentLabel(intent_val)
                    except ValueError:
                        intent_enum = None
                else:
                    intent_enum = intent_val

                if intent_enum is not None:
                    if "requires_rag" not in data or data["requires_rag"] is None:
                        data["requires_rag"] = intent_enum.requires_rag
                    if "needs_multi_hop" not in data or data["needs_multi_hop"] is None:
                        data["needs_multi_hop"] = intent_enum.needs_multi_hop
        return data
