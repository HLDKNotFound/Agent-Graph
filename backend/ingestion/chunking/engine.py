"""Configurable document chunking engine orchestrating structural, sliding window, and table-aware strategies."""

import asyncio
from typing import Any
import logging

from backend.ingestion.chunking.base import ChunkingStrategy
from backend.ingestion.chunking.recursive import RecursiveStructuralChunker
from backend.ingestion.chunking.sliding_window import FixedSlidingWindowChunker
from backend.ingestion.chunking.table_aware import SemanticTableAwareChunker, TableAwareChunker
from backend.ingestion.exceptions import ChunkingError
from backend.ingestion.types.chunking import ChunkingConfig, DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.enums import ChunkingStrategyType, DocumentType
from backend.ingestion.types.extraction import ExtractionResult

logger = logging.getLogger(__name__)


class UnsupportedChunkingStrategyError(ChunkingError):
    """Raised when an unregistered or invalid chunking strategy is requested."""
    pass


class ChunkingEngine:
    """Configurable document chunking engine coordinating multi-strategy text segmentation.

    Adheres to the Technical Requirements:
    - Provides a unified ChunkingStrategy interface.
    - Strategy selection is configurable per document.
    - Supports configurable maximum token limits.
    - Preserves line-level source provenance coordinates across chunk boundaries.
    - Generates deterministic, stable chunk identifiers.
    - Keeps chunking independent from embedding generation and vector indexing.
    - Guarantees zero silent content loss.
    """

    def __init__(
        self,
        strategies: dict[ChunkingStrategyType | str, ChunkingStrategy] | None = None,
    ) -> None:
        self._strategies: dict[str, ChunkingStrategy] = {}

        # Register default initial strategies
        self.register_strategy(RecursiveStructuralChunker())
        self.register_strategy(FixedSlidingWindowChunker())
        self.register_strategy(SemanticTableAwareChunker())

        if strategies:
            for strat in strategies.values():
                self.register_strategy(strat)

    def register_strategy(self, strategy: ChunkingStrategy) -> None:
        """Register or override a chunking strategy implementation."""
        type_str = (
            strategy.strategy_type.value
            if isinstance(strategy.strategy_type, ChunkingStrategyType)
            else str(strategy.strategy_type)
        )
        self._strategies[type_str.upper()] = strategy

        # If registering SEMANTIC_TABLE_AWARE or TABLE_AWARE, alias both
        if type_str.upper() in ("SEMANTIC_TABLE_AWARE", "TABLE_AWARE"):
            self._strategies["SEMANTIC_TABLE_AWARE"] = strategy
            self._strategies["TABLE_AWARE"] = strategy

    def get_strategy(self, strategy_type: ChunkingStrategyType | str) -> ChunkingStrategy:
        """Resolve a strategy by enum member or case-insensitive name."""
        key = strategy_type.value if isinstance(strategy_type, ChunkingStrategyType) else str(strategy_type)
        normalized = key.strip().upper()

        if normalized in self._strategies:
            return self._strategies[normalized]

        raise UnsupportedChunkingStrategyError(
            f"Unsupported chunking strategy: '{strategy_type}'. Available strategies: {list(self._strategies.keys())}"
        )

    def resolve_strategy_for_document(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> ChunkingStrategy:
        """Determine appropriate chunking strategy based on explicit config or document type defaults."""
        # 1. If caller explicitly passed a config, honor its strategy
        if config is not None:
            return self.get_strategy(config.strategy)

        # 2. Automated format-aware default heuristics
        doc_type = extraction_result.document_type
        if doc_type in (DocumentType.XLSX, DocumentType.CSV):
            return self.get_strategy(ChunkingStrategyType.SEMANTIC_TABLE_AWARE)

        # 3. Context options chunking config
        if context.options and context.options.chunking_config:
            return self.get_strategy(context.options.chunking_config.strategy)

        # Default for PDF, DOCX, TXT, Markdown
        return self.get_strategy(ChunkingStrategyType.RECURSIVE_STRUCTURAL)


    async def chunk_document(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> list[DocumentChunkInput]:
        """Execute chunking using the resolved strategy for the input document."""
        strategy = self.resolve_strategy_for_document(
            extraction_result=extraction_result,
            context=context,
            config=config,
        )

        effective_config = config or context.options.chunking_config

        logger.info(
            "Executing document chunking",
            document_id=context.document_id,
            tenant_id=context.tenant_id,
            document_type=extraction_result.document_type.value,
            strategy=strategy.strategy_type.value,
            chunk_size_tokens=effective_config.chunk_size_tokens,
        )

        chunks = await strategy.chunk(
            extraction_result=extraction_result,
            context=context,
            config=effective_config,
        )

        # Post-chunking validation and invariant checks
        self._validate_chunks(chunks, context, extraction_result=extraction_result)
        return chunks

    def chunk_document_sync(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> list[DocumentChunkInput]:
        """Synchronous wrapper for chunk_document."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            def _run():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    return new_loop.run_until_complete(
                        self.chunk_document(extraction_result, context, config)
                    )
                finally:
                    new_loop.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run)
                return future.result()
        else:
            return asyncio.run(self.chunk_document(extraction_result, context, config))


    def _validate_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: IngestionContext,
        extraction_result: ExtractionResult | None = None,
    ) -> None:
        """Enforce strict invariant checks on generated chunk records."""
        for idx, chunk in enumerate(chunks):
            if chunk.chunk_index != idx:
                logger.warning(
                    "Non-sequential chunk index detected; correcting to sequential order",
                    document_id=context.document_id,
                    expected=idx,
                    actual=chunk.chunk_index,
                )
                chunk.chunk_index = idx

            if chunk.tenant_id != context.tenant_id:
                raise ChunkingError(
                    f"Tenant isolation breach in chunk {chunk.chunk_id}: "
                    f"expected '{context.tenant_id}', got '{chunk.tenant_id}'"
                )

            if chunk.document_id != context.document_id:
                raise ChunkingError(
                    f"Document ID mismatch in chunk {chunk.chunk_id}: "
                    f"expected '{context.document_id}', got '{chunk.document_id}'"
                )

            if not chunk.content_hash or len(chunk.content_hash) != 64:
                raise ChunkingError(
                    f"Malformed content hash in chunk {chunk.chunk_id}: '{chunk.content_hash}'"
                )

        # Enforce provenance and locator coordinate validation against document boundaries
        from backend.ingestion.validation.provenance import default_provenance_validator

        default_provenance_validator.validate_chunks(
            chunks=chunks,
            context=context,
            extraction_result=extraction_result,
            strict=True,
        )


# Global default instance and convenience helpers
default_chunking_engine = ChunkingEngine()


def get_chunking_strategy(strategy_type: ChunkingStrategyType | str) -> ChunkingStrategy:
    """Convenience accessor to retrieve a registered ChunkingStrategy instance."""
    return default_chunking_engine.get_strategy(strategy_type)
