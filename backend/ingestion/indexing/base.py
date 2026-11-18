"""Vector database indexing contracts and payload definitions."""

import abc
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.embedding.base import EmbeddingResult
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext


class VectorIndexResult(BaseModel):
    """Indexing outcome report after upserting vector points into Qdrant."""

    model_config = ConfigDict(frozen=True)

    collection_name: str = Field(..., description="Target Qdrant vector collection name")
    points_indexed: int = Field(..., ge=0, description="Total points upserted")
    operation_status: str = Field(default="completed", description="Qdrant upsert status")
    vector_storage_ids: list[str] = Field(
        default_factory=list,
        description="List of point IDs created/updated in vector database",
    )


class VectorIndexProvider(abc.ABC):
    """Abstract contract for upserting and removing vector points in Qdrant with zero-trust ACL payloads."""

    @property
    @abc.abstractmethod
    def collection_name(self) -> str:
        """Target Qdrant collection name."""
        raise NotImplementedError

    @abc.abstractmethod
    async def index_chunks(
        self,
        chunks: list[DocumentChunkInput],
        embeddings: list[EmbeddingResult],
        context: IngestionContext,
    ) -> VectorIndexResult:
        """Upsert embedded chunks with mandatory zero-trust ACL payloads into Qdrant."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_document_vectors(
        self,
        document_id: str,
        context: IngestionContext,
    ) -> int:
        """Remove all vector points belonging to document under authenticated tenant."""
        raise NotImplementedError
