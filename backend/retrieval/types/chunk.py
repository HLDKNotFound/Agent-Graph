"""Retrieved candidate passage models and line-level locator coordinate contracts."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class RetrievedChunkSource(str, Enum):
    """Source engine originating or fusing the retrieved passage."""

    DENSE = "DENSE"
    SPARSE = "SPARSE"
    FUSED = "FUSED"


class ChunkLocatorCoordinates(BaseModel):
    """Physical coordinates preserving line-level provenance across heterogeneous formats."""

    model_config = ConfigDict(frozen=True)

    page_number: int | None = Field(
        default=None,
        description="1-based page number for PDF and scanned OCR documents",
    )
    section_heading: str | None = Field(
        default=None,
        description="Hierarchical section or heading title for structural lineage",
    )
    sheet_name: str | None = Field(
        default=None,
        description="Spreadsheet worksheet name for XLSX/CSV data",
    )
    row_range: str | None = Field(
        default=None,
        description="1-based row index range (e.g. 'Rows 12-25') for tabular data",
    )
    char_start: int | None = Field(
        default=None,
        description="0-indexed start character offset in original document",
    )
    char_end: int | None = Field(
        default=None,
        description="0-indexed end character offset in original document",
    )
    bounding_box: dict[str, Any] | None = Field(
        default=None,
        description="Bounding box coordinates for visual evidence highlighting",
    )


class RetrievedChunk(BaseModel):
    """Fully attributed passage candidate retrieved via dense, sparse, or fused methods.

    Tracks similarity scores, reciprocal rank fusion scores, and cross-encoder relevance scores.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: uuid.UUID = Field(
        ...,
        description="Unique chunk identifier",
    )
    document_id: uuid.UUID = Field(
        ...,
        description="Parent document UUID",
    )
    document_version_id: uuid.UUID | None = Field(
        default=None,
        description="Active version UUID of parent document",
    )
    content: str = Field(
        ...,
        description="Extracted textual content payload",
    )
    chunk_index: int = Field(
        default=0,
        description="0-based sequence index within document",
    )
    token_count: int = Field(
        default=0,
        description="Token count for context budgeting",
    )
    locator: ChunkLocatorCoordinates = Field(
        default_factory=ChunkLocatorCoordinates,
        description="Line-level physical coordinate provenance container",
    )

    # Retrieval and Reranking Scores
    dense_score: float | None = Field(
        default=None,
        description="Cosine similarity score from Qdrant dense vector search [-1.0, 1.0]",
    )
    sparse_score: float | None = Field(
        default=None,
        description="Lexical rank score from PostgreSQL ts_rank_cd cover density",
    )
    rrf_score: float | None = Field(
        default=None,
        description="Reciprocal Rank Fusion score merging dense and sparse ranks",
    )
    rerank_score: float | None = Field(
        default=None,
        description="Cross-Encoder semantic relevance score normalized to [0.0, 1.0]",
    )

    # Ranks
    dense_rank: int | None = Field(
        default=None,
        description="1-based ordinal rank in dense candidate list",
    )
    sparse_rank: int | None = Field(
        default=None,
        description="1-based ordinal rank in sparse candidate list",
    )

    source: RetrievedChunkSource = Field(
        default=RetrievedChunkSource.DENSE,
        description="Originating retrieval engine or fused representation",
    )

    # Access Control Attribution
    classification: str = Field(
        default="INTERNAL",
        description="Security classification of document (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED)",
    )
    department_id: uuid.UUID | None = Field(
        default=None,
        description="Department owning the document",
    )
    document_title: str | None = Field(
        default=None,
        description="Original human-readable filename or document title",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional custom attributes, sheet headers, or OCR metrics",
    )
