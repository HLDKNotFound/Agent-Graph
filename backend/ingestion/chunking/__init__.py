"""Chunking subsystem contracts, strategies, and orchestrating engine."""

from backend.ingestion.chunking.base import ChunkingStrategy
from backend.ingestion.chunking.engine import (
    ChunkingEngine,
    UnsupportedChunkingStrategyError,
    default_chunking_engine,
    get_chunking_strategy,
)
from backend.ingestion.chunking.recursive import RecursiveStructuralChunker
from backend.ingestion.chunking.sliding_window import FixedSlidingWindowChunker
from backend.ingestion.chunking.table_aware import (
    SemanticTableAwareChunker,
    TableAwareChunker,
)
from backend.ingestion.chunking.tokens import (
    compute_chunk_hash,
    count_tokens,
    decode_tokens,
    encode_tokens,
    generate_chunk_id,
    get_tokenizer,
)

__all__ = [
    # Base Contract
    "ChunkingStrategy",
    # Concrete Strategies
    "FixedSlidingWindowChunker",
    "RecursiveStructuralChunker",
    "SemanticTableAwareChunker",
    "TableAwareChunker",
    # Engine & Registry
    "ChunkingEngine",
    "default_chunking_engine",
    "get_chunking_strategy",
    "UnsupportedChunkingStrategyError",
    # Token & Provenance Utilities
    "count_tokens",
    "encode_tokens",
    "decode_tokens",
    "compute_chunk_hash",
    "generate_chunk_id",
    "get_tokenizer",
]
