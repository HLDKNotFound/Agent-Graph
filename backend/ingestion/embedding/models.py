"""Data contracts, models, and configuration options for vector embedding generation."""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class BgeM3Config(BaseModel):
    """Configuration options governing BGE-M3 dense vector embedding generation.

    Adheres strictly to the Zero Hardcoding Principle: resolves defaults dynamically
    from environment settings while allowing runtime overrides.
    """

    model_config = ConfigDict(frozen=True)

    model_name: str = Field(
        default="BAAI/bge-m3",
        description="Pretrained embedding model identifier",
    )
    dimension: int = Field(
        default=1024,
        ge=1,
        description="Expected dense embedding vector dimension (1024 for BGE-M3)",
    )
    device: str = Field(
        default="auto",
        description="Execution compute device ('auto', 'cuda', 'cpu')",
    )
    batch_size: int = Field(
        default=32,
        ge=1,
        le=512,
        description="Batch size for vector embedding inference to bound memory",
    )
    max_length: int = Field(
        default=8192,
        ge=64,
        le=8192,
        description="Maximum token sequence length supported by BGE-M3",
    )
    normalize_embeddings: bool = Field(
        default=True,
        description="Whether to L2-normalize dense vectors to unit length",
    )
    show_progress_bar: bool = Field(
        default=False,
        description="Whether to display progress bar during batch inference",
    )
    fallback_to_cpu: bool = Field(
        default=True,
        description="Whether to gracefully fall back to CPU if CUDA OOM occurs",
    )
    max_retries_on_oom: int = Field(
        default=3,
        ge=0,
        le=5,
        description="Number of retries with halved batch size upon CUDA OutOfMemoryError",
    )


class EmbeddingBatchStats(BaseModel):
    """Execution telemetry and metrics for an embedding generation run."""

    model_config = ConfigDict(frozen=True)

    chunk_count: int = Field(..., ge=0, description="Total chunks processed")
    token_count: int = Field(default=0, ge=0, description="Estimated total tokens consumed")
    duration_seconds: float = Field(..., ge=0.0, description="Inference execution duration in seconds")
    effective_device: str = Field(..., description="Device utilized for inference ('cuda' or 'cpu')")
    effective_batch_size: int = Field(..., ge=1, description="Effective batch size used")
    throughput_chunks_per_sec: float = Field(default=0.0, ge=0.0, description="Chunks processed per second")
