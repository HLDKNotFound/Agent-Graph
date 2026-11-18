"""Abstract contracts for model-agnostic embedding generation."""

import abc
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext


class EmbeddingResult(BaseModel):
    """Dense vector representation produced for a document chunk."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(..., description="Target DocumentChunk UUID")
    vector: list[float] = Field(..., description="Dense embedding vector array (e.g. 1024-dim)")
    model_name: str = Field(..., description="Embedding model name used (e.g. 'BAAI/bge-m3')")
    dimension: int = Field(..., ge=1, description="Vector dimension size (e.g. 1024)")
    token_count: int = Field(default=0, ge=0, description="Token length consumed")


class EmbeddingProvider(abc.ABC):
    """Abstract contract decoupling vector generation from underlying embedding frameworks (BGE-M3, OpenAI, etc.)."""

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """Underlying model architecture identifier (default 'BAAI/bge-m3')."""
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """Embedding vector dimension (1024 for BGE-M3)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def embed_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: IngestionContext,
    ) -> list[EmbeddingResult]:
        """Generate dense embeddings for a batch of DocumentChunkInput records."""
        raise NotImplementedError

    @abc.abstractmethod
    async def embed_texts(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """Generate dense embeddings for an arbitrary batch of text strings."""
        raise NotImplementedError
