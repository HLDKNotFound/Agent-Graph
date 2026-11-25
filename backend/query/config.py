"""Dynamic environment-based configuration for query intelligence and routing subsystem."""

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class QuerySettings(BaseSettings):
    """Configuration settings for Query Intelligence, Coreference Resolution, and Intent Classification.

    Adheres strictly to the Zero Hardcoding Principle: resolves all timeouts, model names,
    thresholds, and Redis connection strings dynamically from environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------
    # Redis Session & Conversation History Settings
    # ------------------------------------------
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        alias="REDIS_URL",
        description="Redis connection URL for multi-turn conversation memory and session state",
    )
    conversation_ttl_seconds: int = Field(
        default=86400,
        alias="CONVERSATION_TTL_SECONDS",
        description="TTL in seconds for multi-turn conversation sessions (default: 86400s / 24h)",
    )
    max_history_turns: int = Field(
        default=10,
        alias="MAX_HISTORY_TURNS",
        description="Maximum rolling window of conversation turns retained for coreference resolution",
    )

    # ------------------------------------------
    # LLM Settings for Query Rewriter & Classifier
    # ------------------------------------------
    llm_provider: str = Field(
        default="openai",
        alias="LLM_PROVIDER",
        description="LLM provider backend ('openai', 'azure', 'anthropic', 'vllm', 'mock')",
    )
    llm_api_key: str | None = Field(
        default=None,
        alias="LLM_API_KEY",
        description="API key for LLM provider access",
    )
    llm_model_name: str = Field(
        default="gpt-4o",
        alias="LLM_MODEL_NAME",
        description="Default LLM model name for contextual query rewriting and intent classification",
    )
    llm_temperature: float = Field(
        default=0.1,
        alias="LLM_TEMPERATURE",
        description="Sampling temperature for deterministic classification and rewriting",
    )
    llm_max_tokens: int = Field(
        default=512,
        alias="LLM_MAX_TOKENS",
        description="Maximum tokens generated during rewriting and classification",
    )
    llm_base_url: str | None = Field(
        default=None,
        alias="LLM_BASE_URL",
        description="Custom base URL for OpenAI-compatible endpoints or local vLLM instances",
    )

    # ------------------------------------------
    # Query Rewriter & Coreference Resolution Settings
    # ------------------------------------------
    rewriter_timeout_seconds: float = Field(
        default=2.0,
        alias="REWRITER_TIMEOUT_SECONDS",
        description="Maximum latency budget for LLM-based coreference query rewriting",
    )
    rewriter_fast_bypass_word_count: int = Field(
        default=12,
        alias="REWRITER_FAST_BYPASS_WORD_COUNT",
        description="Word count threshold above which query without pronouns bypasses LLM rewriting",
    )

    # ------------------------------------------
    # Intent Classification Thresholds & Timeouts
    # ------------------------------------------
    intent_fast_path_confidence_threshold: float = Field(
        default=0.98,
        alias="INTENT_FAST_PATH_CONFIDENCE_THRESHOLD",
        description="Confidence threshold for Tier-1 heuristic rules to bypass LLM classification",
    )
    intent_low_confidence_threshold: float = Field(
        default=0.60,
        alias="INTENT_LOW_CONFIDENCE_THRESHOLD",
        description="Confidence threshold below which fallback to FACTUAL_RETRIEVAL is triggered",
    )
    router_timeout_seconds: float = Field(
        default=2.0,
        alias="ROUTER_TIMEOUT_SECONDS",
        description="Maximum end-to-end latency budget for intent routing pipeline",
    )

    # ------------------------------------------
    # External Web Search Engine (For OUT_OF_DOMAIN)
    # ------------------------------------------
    web_search_provider: str = Field(
        default="tavily",
        alias="WEB_SEARCH_PROVIDER",
        description="External web search engine provider ('tavily', 'bing', 'serper', 'mock')",
    )
    web_search_api_key: str | None = Field(
        default=None,
        alias="WEB_SEARCH_API_KEY",
        description="API key for external web search engine",
    )
    web_search_max_results: int = Field(
        default=5,
        alias="WEB_SEARCH_MAX_RESULTS",
        description="Maximum results returned by external search engine",
    )


@lru_cache(maxsize=1)
def get_query_settings() -> QuerySettings:
    """Provide a cached singleton instance of QuerySettings."""
    return QuerySettings()
