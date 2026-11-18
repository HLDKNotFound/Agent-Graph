"""Abstract base class and contract for document chunking strategies."""

import abc
import asyncio
from typing import Any

from backend.ingestion.types.chunking import ChunkingConfig, DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.enums import ChunkingStrategyType
from backend.ingestion.types.extraction import ExtractionResult


class ChunkingStrategy(abc.ABC):
    """Abstract contract for document chunking algorithms (Recursive Structural, Sliding Window, Table-Aware)."""

    @property
    @abc.abstractmethod
    def strategy_type(self) -> ChunkingStrategyType:
        """Discriminator indicating which chunking strategy is implemented."""
        raise NotImplementedError

    @abc.abstractmethod
    async def chunk(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> list[DocumentChunkInput]:
        """Split extracted document elements into attributable, token-bounded DocumentChunkInput records.

        Must preserve exact source provenance locator coordinates:
        - page_number
        - section_heading
        - sheet_name / row_range
        - [char_start, char_end) half-open character offsets
        - Zero-trust RBAC ACL payload

        Args:
            extraction_result: Structured result from DocumentExtractor.
            context: IngestionContext containing tenant isolation, RBAC roles, and parameters.
            config: Specific chunking hyperparameters (size, overlap, separators).

        Returns:
            List of typed DocumentChunkInput records ready for embedding and persistence.
        """
        raise NotImplementedError

    def chunk_sync(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> list[DocumentChunkInput]:
        """Synchronous wrapper around async chunk()."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run,
                    self.chunk(extraction_result, context, config),
                )
                return future.result()
        else:
            return asyncio.run(self.chunk(extraction_result, context, config))
