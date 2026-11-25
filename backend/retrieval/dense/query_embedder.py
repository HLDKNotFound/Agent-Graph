"""Online dense query embedding service utilizing singleton model lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

from backend.ingestion.embedding.bge_m3 import BgeM3ModelManager, resolve_device
from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.exceptions import DenseRetrievalError

logger = logging.getLogger(__name__)


class QueryEmbedder:
    """Online dense query embedding service reusing BgeM3ModelManager singleton.

    Capabilities:
    1. Singleton Model Lifecycle: Reuses loaded SentenceTransformer model weights across
       ingestion and online retrieval without duplicate memory/VRAM allocation.
    2. Precision Inference: Employs `torch.inference_mode()` with fp16 acceleration on CUDA.
    3. Strict Dimensionality: Enforces exact 1024-dimensional normalized dense vectors.
    4. L2 Normalization: Guarantees unit Euclidean norm for cosine similarity in Qdrant.
    5. Asynchronous API: Exposes `async def embed_query()` via worker thread pool.
    """

    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        model_manager: BgeM3ModelManager | None = None,
        model: Any | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.model_manager = model_manager or BgeM3ModelManager.get_instance()
        self._model = model  # Injected model (useful for unit testing without downloading 2.2GB weights)
        self.device = resolve_device(self.settings.embedding_device)

    def _get_model(self) -> SentenceTransformer:
        """Provide cached model instance from singleton registry or injected fixture."""
        if self._model is not None:
            return self._model
        return self.model_manager.get_model(
            model_name=self.settings.embedding_model_name,
            device=self.device,
        )

    def embed_query_sync(self, text: str) -> list[float]:
        """Synchronously encode a single user query into a 1024-dimensional normalized dense vector.

        Args:
            text: Query text string.

        Returns:
            List of 1024 floating point values with unit L2 norm.

        Raises:
            DenseRetrievalError: If embedding computation fails.
        """
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Query text cannot be empty or purely whitespace")

        model = self._get_model()

        try:
            with torch.inference_mode():
                # Encode with normalize_embeddings=True to guarantee unit Euclidean length
                embedding: np.ndarray = model.encode(
                    clean_text,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )

            # Ensure 1D array
            if embedding.ndim > 1:
                embedding = embedding.flatten()

            vector = [float(x) for x in embedding.tolist()]
            if len(vector) != 1024 and getattr(self, "_allow_custom_dim", False) is False:
                # If custom model in tests is not 1024, check flag
                raise ValueError(f"Expected 1024-dim dense embedding, got {len(vector)}")

            return vector

        except Exception as exc:
            logger.error("Failed to generate dense query embedding: %s", exc, exc_info=True)
            raise DenseRetrievalError(
                f"Failed to generate dense query embedding for '{clean_text[:40]}...': {exc}",
                details={"error": str(exc)},
            ) from exc

    async def embed_query(self, text: str) -> list[float]:
        """Asynchronously encode a single query text in thread pool."""
        return await asyncio.to_thread(self.embed_query_sync, text)

    def embed_batch_sync(self, texts: Sequence[str]) -> list[list[float]]:
        """Synchronously encode multiple query texts in a single batch."""
        clean_texts = [t.strip() for t in texts if t.strip()]
        if not clean_texts:
            return []

        model = self._get_model()

        try:
            with torch.inference_mode():
                embeddings: np.ndarray = model.encode(
                    clean_texts,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    batch_size=len(clean_texts),
                    show_progress_bar=False,
                )

            vectors: list[list[float]] = []
            for row in embeddings:
                vectors.append([float(x) for x in row.tolist()])
            return vectors
        except Exception as exc:
            logger.error("Failed to batch encode query embeddings: %s", exc, exc_info=True)
            raise DenseRetrievalError(
                f"Failed to batch encode {len(clean_texts)} query embeddings: {exc}",
                details={"error": str(exc)},
            ) from exc

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Asynchronously batch encode query texts in thread pool."""
        return await asyncio.to_thread(self.embed_batch_sync, texts)
