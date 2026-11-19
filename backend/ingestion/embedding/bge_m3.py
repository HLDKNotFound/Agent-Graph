"""BGE-M3 Dense Vector Embedding Service.

Implements BAAI/bge-m3 dense vector embedding generation conforming to the
EmbeddingProvider contract with strict 1024-dimension enforcement, L2 normalization,
device management, reusable model caching, and retry-safe batch execution.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Sequence

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.embedding.base import EmbeddingProvider, EmbeddingResult
from backend.ingestion.embedding.models import BgeM3Config, EmbeddingBatchStats
from backend.ingestion.exceptions import EmbeddingError, ValidationError
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext

logger = logging.getLogger(__name__)


def resolve_device(requested_device: str = "auto") -> str:
    """Resolve compute device string ('auto', 'cuda', 'cpu') to active torch device.

    Args:
        requested_device: Desired device specification.

    Returns:
        Canonical torch device string ('cuda' or 'cpu').
    """
    dev = requested_device.lower().strip()
    if dev in ("cuda", "gpu"):
        if torch.cuda.is_available():
            return "cuda"
        logger.warning("CUDA requested but not available. Falling back to CPU.")
        return "cpu"
    elif dev == "cpu":
        return "cpu"
    elif dev == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    else:
        # Custom device string like 'cuda:0', 'cuda:1'
        if dev.startswith("cuda") and not torch.cuda.is_available():
            logger.warning("CUDA device '%s' requested but CUDA is unavailable. Falling back to CPU.", dev)
            return "cpu"
        return dev


class BgeM3ModelManager:
    """Thread-safe lifecycle manager and registry for loaded SentenceTransformer models.

    Ensures the heavy BGE-M3 model weights (~2.2 GB) are loaded only once per
    (model_name, device) configuration and reused across concurrent requests.
    """

    _instance: BgeM3ModelManager | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._models: dict[tuple[str, str], SentenceTransformer] = {}
        self._registry_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> BgeM3ModelManager:
        """Provide or create shared singleton BgeM3ModelManager."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get_model(self, model_name: str = "BAAI/bge-m3", device: str = "auto") -> SentenceTransformer:
        """Retrieve cached model instance or load weights into memory/VRAM if not cached.

        Args:
            model_name: HuggingFace model identifier or local directory path.
            device: Compute device string ('auto', 'cuda', 'cpu').

        Returns:
            Instantiated and warmed SentenceTransformer model.
        """
        canonical_device = resolve_device(device)
        cache_key = (model_name, canonical_device)

        if cache_key in self._models:
            return self._models[cache_key]

        with self._registry_lock:
            # Double-checked locking
            if cache_key in self._models:
                return self._models[cache_key]

            logger.info(
                "Loading embedding model '%s' onto device '%s'...",
                model_name,
                canonical_device,
            )
            try:
                model = SentenceTransformer(model_name, device=canonical_device)
                self._models[cache_key] = model
                logger.info(
                    "Model '%s' successfully loaded onto '%s' (max_seq_length=%d).",
                    model_name,
                    canonical_device,
                    getattr(model, "max_seq_length", 8192),
                )
                return model
            except Exception as exc:
                logger.error("Failed to load embedding model '%s': %s", model_name, exc, exc_info=True)
                raise EmbeddingError(
                    f"Failed to load embedding model '{model_name}' onto device '{canonical_device}': {exc}",
                    details={"model_name": model_name, "device": canonical_device, "error": str(exc)},
                ) from exc

    def unload_model(self, model_name: str, device: str = "auto") -> bool:
        """Unload model from cache and release device memory/VRAM."""
        canonical_device = resolve_device(device)
        cache_key = (model_name, canonical_device)
        with self._registry_lock:
            model = self._models.pop(cache_key, None)
            if model is not None:
                del model
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                logger.info("Unloaded model '%s' from device '%s'.", model_name, canonical_device)
                return True
            return False

    def clear_all(self) -> None:
        """Purge all cached models from memory."""
        with self._registry_lock:
            self._models.clear()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            logger.info("Cleared all cached embedding models.")

    def is_loaded(self, model_name: str = "BAAI/bge-m3", device: str = "auto") -> bool:
        """Check if a specific model and device combination is loaded in cache."""
        canonical_device = resolve_device(device)
        return (model_name, canonical_device) in self._models


class BgeM3EmbeddingService(EmbeddingProvider):
    """Production implementation of EmbeddingProvider using BAAI/bge-m3 dense vector representations.

    Features:
    1. Dense Representations: Enforces dense 1024-dimensional normalized vector embeddings.
    2. Zero Memory Leaks: Configurable batch sizes and micro-batch streaming.
    3. Device Agnostic: Supports CPU and CUDA acceleration with auto-detection.
    4. Lifecycle Management: Uses BgeM3ModelManager so weights are not reloaded per request.
    5. Retry-Safe Inference: Adaptive batch halving and CPU fallback on CUDA OutOfMemoryError.
    6. Exact Mapping Preservation: 1:1 order-preserving correlation between input chunks and vectors.
    7. Fully Decoupled: Zero runtime dependencies on vector databases (Qdrant) or task queues (Celery).
    """

    def __init__(
        self,
        config: BgeM3Config | None = None,
        model_manager: BgeM3ModelManager | None = None,
        settings: IngestionSettings | None = None,
        model_name: str | None = None,
        dimension: int | None = None,
        device: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.settings = settings or get_ingestion_settings()
        self.config = config or BgeM3Config(
            model_name=model_name or self.settings.embedding_model_name,
            dimension=dimension or self.settings.embedding_dimension,
            device=device or self.settings.embedding_device,
            batch_size=batch_size or self.settings.embedding_batch_size,
            max_length=self.settings.embedding_max_length,
            normalize_embeddings=self.settings.embedding_normalize,
        )

        self._model_manager = model_manager or BgeM3ModelManager.get_instance()
        self._effective_device = resolve_device(self.config.device)

    @property
    def model_name(self) -> str:
        """Underlying model architecture identifier (e.g. 'BAAI/bge-m3')."""
        return self.config.model_name

    @property
    def dimension(self) -> int:
        """Embedding vector dimension (strictly 1024 for BGE-M3 dense)."""
        return self.config.dimension

    @property
    def device(self) -> str:
        """Configured or resolved compute device."""
        return self._effective_device

    @property
    def batch_size(self) -> int:
        """Configured batch size for vector generation."""
        return self.config.batch_size

    def _get_active_model(self, force_device: str | None = None) -> SentenceTransformer:
        """Retrieve model instance from manager."""
        target_device = force_device or self._effective_device
        return self._model_manager.get_model(
            model_name=self.config.model_name,
            device=target_device,
        )

    def _encode_batch(
        self,
        texts: list[str],
        batch_size: int | None = None,
    ) -> list[list[float]]:
        """Core batch inference engine with retry-safe micro-batching and adaptive OOM recovery.

        Args:
            texts: List of textual strings to encode.
            batch_size: Optional micro-batch size override.

        Returns:
            List of 1024-dimensional normalized float vectors.
        """
        if not texts:
            return []

        # Validate input strings
        for idx, text in enumerate(texts):
            if text is None or not isinstance(text, str):
                raise ValidationError(
                    f"Invalid embedding input at index {idx}: expected string, got {type(text).__name__}"
                )

        effective_batch_size = batch_size or self.config.batch_size
        results: list[list[float]] = []

        total_texts = len(texts)
        start_idx = 0

        while start_idx < total_texts:
            end_idx = min(start_idx + effective_batch_size, total_texts)
            chunk_slice = texts[start_idx:end_idx]

            # Try encoding slice with adaptive OOM recovery
            slice_vectors = self._encode_slice_with_retry(
                chunk_slice=chunk_slice,
                initial_batch_size=effective_batch_size,
            )

            results.extend(slice_vectors)
            start_idx = end_idx

        # Enforce output dimension constraint
        for idx, vec in enumerate(results):
            if len(vec) != self.config.dimension:
                raise EmbeddingError(
                    f"Output dimension mismatch at index {idx}: expected {self.config.dimension}, got {len(vec)}",
                    details={"expected": self.config.dimension, "actual": len(vec), "index": idx},
                )

        return results

    def _encode_slice_with_retry(
        self,
        chunk_slice: list[str],
        initial_batch_size: int,
    ) -> list[list[float]]:
        """Encode a slice of strings with automatic OOM retry and CPU fallback."""
        current_batch_size = initial_batch_size
        retries = 0
        current_device = self._effective_device

        while True:
            try:
                model = self._get_active_model(force_device=current_device)
                raw_embeddings = model.encode(
                    chunk_slice,
                    batch_size=current_batch_size,
                    normalize_embeddings=self.config.normalize_embeddings,
                    show_progress_bar=self.config.show_progress_bar,
                    convert_to_numpy=True,
                )
                # Ensure 2D numpy array format
                if len(raw_embeddings.shape) == 1:
                    raw_embeddings = np.expand_dims(raw_embeddings, axis=0)

                return [row.tolist() for row in raw_embeddings]

            except (torch.cuda.OutOfMemoryError, RuntimeError) as exc:
                err_str = str(exc).lower()
                is_oom = "out of memory" in err_str or isinstance(exc, torch.cuda.OutOfMemoryError)

                if is_oom and current_device == "cuda":
                    logger.warning(
                        "CUDA OutOfMemory detected during batch embedding (slice_size=%d, batch_size=%d).",
                        len(chunk_slice),
                        current_batch_size,
                    )
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()

                    # Strategy 1: Halve batch size and retry
                    if retries < self.config.max_retries_on_oom and current_batch_size > 1:
                        retries += 1
                        current_batch_size = max(1, current_batch_size // 2)
                        logger.info("Retrying with halved batch size: %d (attempt %d)", current_batch_size, retries)
                        continue

                    # Strategy 2: Fall back to CPU if enabled
                    if self.config.fallback_to_cpu:
                        logger.warning("Exhausted CUDA retries. Falling back to CPU for current slice.")
                        current_device = "cpu"
                        continue

                logger.error("Embedding inference error: %s", exc, exc_info=True)
                raise EmbeddingError(
                    f"Model inference failed: {exc}",
                    details={"error": str(exc), "slice_size": len(chunk_slice), "device": current_device},
                ) from exc

            except Exception as exc:
                logger.error("Unexpected embedding failure: %s", exc, exc_info=True)
                raise EmbeddingError(
                    f"Failed to generate embeddings: {exc}",
                    details={"error": str(exc)},
                ) from exc

    async def embed_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: IngestionContext | None = None,
    ) -> list[EmbeddingResult]:
        """Generate dense embeddings for a sequence of DocumentChunkInput records.

        Guarantees:
        1. Exact Mapping: Output order strictly corresponds 1:1 with input chunks.
        2. Output Dimension: Strictly 1024-dimensional normalized vectors.
        3. Token Metric Preservation: Propagates chunk.token_count to EmbeddingResult.

        Args:
            chunks: List of DocumentChunkInput contracts.
            context: IngestionContext providing runtime telemetry and options.

        Returns:
            List of EmbeddingResult objects matching input chunks in exact order.
        """
        if not chunks:
            return []

        # Extract text payloads
        texts = [chunk.content for chunk in chunks]

        # Generate vectors
        vectors = self._encode_batch(texts)

        # Assemble results preserving exact 1:1 chunk mapping
        results: list[EmbeddingResult] = []
        for chunk, vec in zip(chunks, vectors):
            res = EmbeddingResult(
                chunk_id=chunk.chunk_id,
                vector=vec,
                model_name=self.config.model_name,
                dimension=self.config.dimension,
                token_count=chunk.token_count,
            )
            results.append(res)

        return results

    async def embed_texts(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """Generate dense embeddings for an arbitrary batch of text strings.

        Args:
            texts: List of input strings.

        Returns:
            List of 1024-dimensional normalized float vectors.
        """
        return self._encode_batch(texts)

    def embed_single_text(self, text: str) -> list[float]:
        """Convenience method to generate an embedding for a single text string."""
        if text is None or not isinstance(text, str):
            raise ValidationError(f"Invalid text input: expected string, got {type(text).__name__}")
        return self._encode_batch([text])[0]

    def embed_single_chunk(self, chunk: DocumentChunkInput) -> EmbeddingResult:
        """Convenience method to generate an embedding for a single DocumentChunkInput."""
        if not isinstance(chunk, DocumentChunkInput):
            raise ValidationError(f"Invalid chunk input: expected DocumentChunkInput, got {type(chunk).__name__}")
        vec = self._encode_batch([chunk.content])[0]
        return EmbeddingResult(
            chunk_id=chunk.chunk_id,
            vector=vec,
            model_name=self.config.model_name,
            dimension=self.config.dimension,
            token_count=chunk.token_count,
        )


# Module-level default singleton for easy consumption
_default_embedding_service: BgeM3EmbeddingService | None = None


def get_embedding_service() -> BgeM3EmbeddingService:
    """Provide or initialize the shared singleton BgeM3EmbeddingService."""
    global _default_embedding_service
    if _default_embedding_service is None:
        _default_embedding_service = BgeM3EmbeddingService()
    return _default_embedding_service
