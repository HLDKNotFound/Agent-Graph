"""Intent definitions and taxonomy for the Enterprise Knowledge Agent."""

from enum import Enum


class IntentLabel(str, Enum):
    """The 5 canonical query intents supported by the First-Stage Router Gate."""

    FACTUAL_RETRIEVAL = "FACTUAL_RETRIEVAL"
    MULTI_DOC_COMPARISON = "MULTI_DOC_COMPARISON"
    TABLE_LOOKUP = "TABLE_LOOKUP"
    GENERAL_CHAT = "GENERAL_CHAT"
    OUT_OF_DOMAIN = "OUT_OF_DOMAIN"

    @property
    def requires_rag(self) -> bool:
        """Indicate whether the intent requires internal vector and relational retrieval."""
        return self in (
            IntentLabel.FACTUAL_RETRIEVAL,
            IntentLabel.MULTI_DOC_COMPARISON,
            IntentLabel.TABLE_LOOKUP,
        )

    @property
    def needs_multi_hop(self) -> bool:
        """Indicate whether the intent requires multi-hop LangGraph DAG reasoning."""
        return self == IntentLabel.MULTI_DOC_COMPARISON

    @property
    def triggers_web_search(self) -> bool:
        """Indicate whether the intent routes to external internet web search."""
        return self == IntentLabel.OUT_OF_DOMAIN
