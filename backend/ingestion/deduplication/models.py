"""Data contracts, models, and enums for document deduplication and duplicate handling."""

from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class DuplicateHandlingPolicy(str, Enum):
    """Configures system behavior when an exact document content hash match is detected."""

    RETURN_EXISTING = "RETURN_EXISTING"
    """Reuse and return existing document record; bypass redundant extraction/storage."""

    REJECT = "REJECT"
    """Reject upload and raise DuplicateDocumentError."""

    NEW_VERSION = "NEW_VERSION"
    """Create a new version under the existing document model if identical hash is treated as revision."""


class DuplicateAction(str, Enum):
    """Action outcome recorded during document deduplication check."""

    PROCEED = "PROCEED"
    """No active duplicate detected; proceed to extraction and chunking pipeline."""

    RETURN_EXISTING = "RETURN_EXISTING"
    """Active duplicate document found; returned existing document reference."""

    REJECTED = "REJECTED"
    """Duplicate detected and rejected according to REJECT policy."""

    NEW_VERSION_CREATED = "NEW_VERSION_CREATED"
    """New version snapshot created under parent document lineage."""


class ExistingDocumentSummary(BaseModel):
    """Structured summary of an existing document record matching a content hash."""

    model_config = ConfigDict(extra="ignore")

    document_id: str = Field(..., description="UUID of the existing document")
    tenant_id: str = Field(..., description="UUID of owning tenant")
    department_id: str | None = Field(default=None, description="Department UUID if assigned")
    uploaded_by_id: str | None = Field(default=None, description="Uploader user UUID")
    current_version_id: str | None = Field(default=None, description="UUID of current active version")
    original_filename: str = Field(..., description="Original filename upon upload")
    normalized_filename: str | None = Field(default=None, description="Sanitized/normalized filename")
    storage_uri: str | None = Field(default=None, description="Canonical storage URI (s3:// or file://)")
    mime_type: str | None = Field(default=None, description="MIME content type")
    file_size_bytes: int = Field(default=0, description="Size of file in bytes")
    content_hash: str = Field(..., description="SHA-256 hexadecimal digest")
    classification: str = Field(default="INTERNAL", description="Document security classification")
    status: str = Field(default="PENDING", description="Document ingestion status")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Custom JSON metadata attributes")
    version_number: int | None = Field(default=1, description="Current version number")
    created_at: datetime | None = Field(default=None, description="Document creation timestamp")
    updated_at: datetime | None = Field(default=None, description="Document update timestamp")


class DocumentCreateInput(BaseModel):
    """Typed payload for inserting a new document record with race safety."""

    model_config = ConfigDict(extra="ignore")

    document_id: str | None = Field(default=None, description="Explicit UUID or auto-generated")
    tenant_id: str = Field(..., description="Owning tenant UUID")
    department_id: str | None = Field(default=None, description="Associated department UUID")
    uploaded_by_id: str | None = Field(default=None, description="Uploader user UUID")
    original_filename: str = Field(..., description="Original uploaded filename")
    normalized_filename: str = Field(..., description="Sanitized filename")
    storage_uri: str = Field(..., description="Canonical storage URI")
    mime_type: str = Field(default="application/octet-stream", description="MIME type")
    file_size_bytes: int = Field(..., ge=0, description="Exact file size in bytes")
    content_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hex digest")
    classification: str = Field(default="INTERNAL", description="Classification enum")
    status: str = Field(default="PENDING", description="Ingestion status enum")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Metadata JSON payload")


class DocumentVersionCreateInput(BaseModel):
    """Typed payload for creating an incremental version snapshot."""

    model_config = ConfigDict(extra="ignore")

    document_id: str = Field(..., description="Parent document UUID")
    tenant_id: str = Field(..., description="Parent tenant UUID")
    created_by_id: str | None = Field(default=None, description="Creator user UUID")
    original_filename: str = Field(..., description="Version original filename")
    storage_uri: str = Field(..., description="Version storage URI")
    mime_type: str = Field(default="application/octet-stream", description="MIME type")
    file_size_bytes: int = Field(..., ge=0, description="Version size in bytes")
    content_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hex digest")
    status: str = Field(default="PENDING", description="Version ingestion status")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Version metadata JSON")
    is_current: bool = Field(default=True, description="Whether to promote as current version")


# ==========================================
# MINHASH & LSH CHUNK DEDUPLICATION MODELS
# ==========================================


class DuplicateType(str, Enum):
    """Classification of chunk duplicate relation."""

    EXACT = "EXACT"
    """Identical SHA-256 hash or identical normalized text (Jaccard similarity >= exact_threshold)."""

    NEAR_DUPLICATE = "NEAR_DUPLICATE"
    """Substantially similar chunk content meeting or exceeding similarity threshold."""

    UNIQUE = "UNIQUE"
    """Distinct chunk content falling below similarity threshold."""


class ComparisonScope(str, Enum):
    """Scope boundaries for chunk duplicate detection."""

    CROSS_VERSION = "CROSS_VERSION"
    """Compare strictly against other versions of the same document (revision tracking)."""

    CROSS_DOCUMENT = "CROSS_DOCUMENT"
    """Compare strictly against other documents within the same tenant boundary."""

    SAME_DOCUMENT = "SAME_DOCUMENT"
    """Compare within the same document (across all versions and current chunks)."""

    ALL = "ALL"
    """Compare across all indexed chunks within the tenant (global tenant deduplication)."""


class MinHashConfig(BaseModel):
    """Configuration options governing shingling, permutations, and LSH partitioning."""

    model_config = ConfigDict(frozen=True)

    shingle_size: int = Field(
        default=3,
        ge=1,
        le=20,
        description="N-gram shingle length (e.g. 3 for 3-shingles)",
    )
    num_permutations: int = Field(
        default=128,
        ge=16,
        le=1024,
        description="Number of permutation hash functions for signature vector",
    )
    similarity_threshold: float = Field(
        default=0.92,
        ge=0.0,
        le=1.0,
        description="Jaccard similarity threshold for flagging near-duplicates",
    )
    exact_threshold: float = Field(
        default=0.999,
        ge=0.5,
        le=1.0,
        description="Threshold above which match is classified as EXACT duplicate",
    )
    ngram_type: str = Field(
        default="word",
        description="Shingle segmentation tokenization type ('word' or 'character')",
    )
    lsh_bands: int | None = Field(
        default=None,
        ge=1,
        description="Explicit number of bands for LSH partitioning (auto-computed if None)",
    )
    lsh_rows: int | None = Field(
        default=None,
        ge=1,
        description="Explicit number of rows per band for LSH partitioning",
    )
    seed: int = Field(
        default=42,
        description="Deterministic PRNG seed for universal permutation hashing",
    )


class ChunkDuplicateMatch(BaseModel):
    """Detailed record of a matching duplicate chunk identified in the LSH index."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(..., description="UUID of the matching chunk in index")
    document_id: str = Field(..., description="UUID of the matching chunk's document")
    document_version_id: str | None = Field(
        default=None,
        description="UUID of the document version if versioned",
    )
    document_version_number: int | None = Field(
        default=None,
        description="Version sequence number (e.g. 1, 2, 3)",
    )
    chunk_index: int | None = Field(
        default=None,
        description="Chunk sequence index in source document",
    )
    duplicate_type: DuplicateType = Field(
        ...,
        description="Classification: EXACT or NEAR_DUPLICATE",
    )
    similarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Estimated Jaccard similarity score [0.0, 1.0]",
    )
    exact_content_match: bool = Field(
        default=False,
        description="True if content_hash matches or identical string",
    )
    content_hash: str | None = Field(
        default=None,
        description="SHA-256 hash of the matching chunk",
    )
    content_preview: str | None = Field(
        default=None,
        description="Preview snippet of the matching chunk content",
    )
    locator: Any | None = Field(
        default=None,
        description="Provenance coordinates of the matching chunk (ChunkLocator)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Metadata dictionary of the matching chunk",
    )


class ChunkDuplicateReport(BaseModel):
    """Comprehensive deduplication analysis report for a single chunk."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(..., description="UUID of the queried chunk")
    document_id: str = Field(..., description="UUID of the parent document")
    tenant_id: str = Field(..., description="Tenant UUID enforcing zero-trust isolation")
    document_version_id: str | None = Field(
        default=None,
        description="Document version UUID if present",
    )
    has_duplicates: bool = Field(
        default=False,
        description="Whether any duplicates (exact or near) were detected",
    )
    duplicate_type: DuplicateType = Field(
        default=DuplicateType.UNIQUE,
        description="Highest severity duplicate classification detected",
    )
    primary_match: ChunkDuplicateMatch | None = Field(
        default=None,
        description="Top-ranked matching chunk by severity, similarity, and version recency",
    )
    all_matches: list[ChunkDuplicateMatch] = Field(
        default_factory=list,
        description="All candidate matches satisfying similarity threshold, sorted by rank",
    )
    total_matches_count: int = Field(
        default=0,
        description="Total count of candidate matches meeting threshold",
    )
    action: DuplicateAction = Field(
        default=DuplicateAction.PROCEED,
        description="Recommended downstream action",
    )
    minhash_signature: list[int] | None = Field(
        default=None,
        description="Calculated MinHash signature array",
    )
    message: str | None = Field(
        default=None,
        description="Diagnostic summary of deduplication analysis",
    )
