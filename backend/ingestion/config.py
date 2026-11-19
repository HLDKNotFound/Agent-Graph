"""Dynamic environment-based configuration for document ingestion and storage subsystems."""

from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionSettings(BaseSettings):
    """Dynamic configuration settings for file ingestion, validation, and storage.

    Adheres strictly to the Zero Hardcoding Principle: resolves all paths,
    credentials, bucket names, and file size limits from environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------
    # Storage Backend Configuration
    # ------------------------------------------
    storage_backend: str = Field(
        default="local",
        alias="STORAGE_BACKEND",
        description="Physical storage backend driver ('local' or 's3')",
    )
    storage_local_root: str = Field(
        default="storage/documents",
        alias="STORAGE_LOCAL_ROOT",
        description="Root directory for local filesystem storage",
    )

    # ------------------------------------------
    # S3 Object Storage Settings
    # ------------------------------------------
    s3_endpoint_url: str | None = Field(default=None, alias="S3_ENDPOINT_URL")
    s3_access_key_id: str | None = Field(default=None, alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str | None = Field(default=None, alias="S3_SECRET_ACCESS_KEY")
    s3_bucket_name: str = Field(
        default="enterprise-knowledge-docs",
        alias="S3_BUCKET_NAME",
        description="Target S3 / MinIO / R2 bucket name",
    )
    s3_region: str = Field(
        default="us-east-1",
        alias="S3_REGION",
        description="S3 region identifier",
    )

    # ------------------------------------------
    # Upload & File Validation Restrictions
    # ------------------------------------------
    max_file_size_bytes: int = Field(
        default=52_428_800,  # 50 MB
        alias="MAX_FILE_SIZE_BYTES",
        description="Maximum allowed uploaded file size in bytes (default: 50MB)",
    )
    min_file_size_bytes: int = Field(
        default=1,
        alias="MIN_FILE_SIZE_BYTES",
        description="Minimum allowed uploaded file size in bytes (rejects 0-byte files)",
    )
    max_filename_length: int = Field(
        default=255,
        alias="MAX_FILENAME_LENGTH",
        description="Maximum length for sanitized filenames",
    )
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".docx", ".xlsx", ".csv"],
        alias="ALLOWED_EXTENSIONS",
        description="List of allowed document file extensions",
    )
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
            "text/plain",
        ],
        alias="ALLOWED_MIME_TYPES",
        description="List of allowed document MIME content types",
    )

    # ------------------------------------------
    # Spreadsheet & Tabular Extraction Settings
    # ------------------------------------------
    spreadsheet_max_rows_per_sheet: int = Field(
        default=50_000,
        alias="SPREADSHEET_MAX_ROWS_PER_SHEET",
        description="Maximum allowed rows to parse per worksheet before truncation",
    )
    spreadsheet_streaming_threshold_bytes: int = Field(
        default=10_485_760,  # 10 MB
        alias="SPREADSHEET_STREAMING_THRESHOLD_BYTES",
        description="File size threshold above which streaming read-only mode is activated for spreadsheets",
    )
    spreadsheet_formula_policy: str = Field(
        default="cached_values",
        alias="SPREADSHEET_FORMULA_POLICY",
        description="Policy for formula extraction: 'cached_values', 'formulas', or 'both'",
    )

    # ------------------------------------------
    # OCR & Scanned Document Configuration
    # ------------------------------------------
    ocr_enabled: bool = Field(
        default=True,
        alias="OCR_ENABLED",
        description="Global toggle for OCR extraction capability on scanned pages",
    )
    ocr_engine: str = Field(
        default="rapidocr",
        alias="OCR_ENGINE",
        description="Default OCR engine implementation ('rapidocr' or 'tesseract')",
    )
    ocr_dpi: int = Field(
        default=300,
        alias="OCR_DPI",
        description="Rendering DPI for PDF rasterization during OCR (e.g. 150, 200, 300)",
    )
    ocr_languages: str = Field(
        default="eng+vie",
        alias="OCR_LANGUAGES",
        description="Target OCR language codes (e.g. 'eng+vie' for English and Vietnamese)",
    )
    ocr_scanned_char_threshold: int = Field(
        default=50,
        alias="OCR_SCANNED_CHAR_THRESHOLD",
        description="Character count threshold below which a page is inspected for scanned image content",
    )
    ocr_image_coverage_threshold: float = Field(
        default=0.4,
        alias="OCR_IMAGE_COVERAGE_THRESHOLD",
        description="Image-to-page surface area ratio to classify a low-text page as scanned",
    )
    ocr_min_confidence: float = Field(
        default=0.3,
        alias="OCR_MIN_CONFIDENCE",
        description="Minimum confidence score threshold for recognized text blocks (0.0 to 1.0)",
    )

    # ------------------------------------------
    # Relational Database & Deduplication Settings
    # ------------------------------------------
    database_url: str = Field(
        default="postgresql://postgres:password@localhost:5432/agentgraph_db?schema=public",
        alias="DATABASE_URL",
        description="PostgreSQL connection string for relational storage and document deduplication",
    )
    direct_url: str | None = Field(
        default=None,
        alias="DIRECT_URL",
        description="Direct PostgreSQL connection string bypassing transaction poolers",
    )
    deduplication_chunk_size_bytes: int = Field(
        default=65_536,  # 64 KB
        alias="DEDUPLICATION_CHUNK_SIZE_BYTES",
        description="Buffer size in bytes for streaming SHA-256 hash calculation",
    )
    deduplication_default_policy: str = Field(
        default="RETURN_EXISTING",
        alias="DEDUPLICATION_DEFAULT_POLICY",
        description="Default handling policy when duplicate document is encountered ('RETURN_EXISTING', 'REJECT', 'NEW_VERSION')",
    )

    # ------------------------------------------
    # MinHash LSH Near-Duplicate Chunk Settings
    # ------------------------------------------
    minhash_enabled: bool = Field(
        default=True,
        alias="MINHASH_ENABLED",
        description="Global toggle for chunk-level MinHash LSH near-duplicate detection",
    )
    minhash_shingle_size: int = Field(
        default=3,
        alias="MINHASH_SHINGLE_SIZE",
        description="N-gram shingle size for MinHash signature calculation (default: 3)",
    )
    minhash_num_permutations: int = Field(
        default=128,
        alias="MINHASH_NUM_PERMUTATIONS",
        description="Number of permutation hash functions for MinHash signature (default: 128)",
    )
    minhash_similarity_threshold: float = Field(
        default=0.92,
        alias="MINHASH_SIMILARITY_THRESHOLD",
        description="Jaccard similarity threshold for near-duplicate chunk detection (default: 0.92)",
    )
    minhash_exact_threshold: float = Field(
        default=0.999,
        alias="MINHASH_EXACT_THRESHOLD",
        description="Threshold above which chunk is classified as an exact duplicate (default: 0.999)",
    )
    minhash_ngram_type: str = Field(
        default="word",
        alias="MINHASH_NGRAM_TYPE",
        description="Shingle segmentation tokenization type ('word' or 'character')",
    )
    minhash_lsh_bands: int | None = Field(
        default=None,
        alias="MINHASH_LSH_BANDS",
        description="Number of bands for LSH partitioning. If None, computed from threshold and permutations",
    )
    minhash_lsh_rows: int | None = Field(
        default=None,
        alias="MINHASH_LSH_ROWS",
        description="Number of rows per band for LSH partitioning",
    )
    minhash_seed: int = Field(
        default=42,
        alias="MINHASH_SEED",
        description="Deterministic PRNG seed for universal permutation generation",
    )

    # ------------------------------------------
    # Dense Vector Embedding Settings (BGE-M3)
    # ------------------------------------------
    embedding_model_name: str = Field(
        default="BAAI/bge-m3",
        alias="EMBEDDING_MODEL_NAME",
        description="Pretrained embedding model identifier (default: BAAI/bge-m3)",
    )
    embedding_dimension: int = Field(
        default=1024,
        alias="EMBEDDING_DIMENSION",
        description="Expected dense embedding vector dimension (default: 1024)",
    )
    embedding_device: str = Field(
        default="auto",
        alias="EMBEDDING_DEVICE",
        description="Compute device for embedding inference ('auto', 'cuda', 'cpu')",
    )
    embedding_batch_size: int = Field(
        default=32,
        alias="EMBEDDING_BATCH_SIZE",
        description="Batch size for vector embedding inference (default: 32)",
    )
    embedding_max_length: int = Field(
        default=8192,
        alias="EMBEDDING_MAX_LENGTH",
        description="Maximum token sequence length supported by BGE-M3 (default: 8192)",
    )
    embedding_normalize: bool = Field(
        default=True,
        alias="EMBEDDING_NORMALIZE",
        description="Whether to L2-normalize dense embeddings to unit length (default: True)",
    )

    # ------------------------------------------
    # Vector Database (Qdrant) Settings
    # ------------------------------------------
    qdrant_host: str = Field(
        default="localhost",
        alias="QDRANT_HOST",
        description="Qdrant service hostname or IP",
    )
    qdrant_port: int = Field(
        default=6333,
        alias="QDRANT_PORT",
        description="Qdrant HTTP REST API port",
    )
    qdrant_grpc_port: int = Field(
        default=6334,
        alias="QDRANT_GRPC_PORT",
        description="Qdrant gRPC port",
    )
    qdrant_api_key: str | None = Field(
        default=None,
        alias="QDRANT_API_KEY",
        description="API key for authenticated Qdrant access",
    )
    qdrant_url: str | None = Field(
        default=None,
        alias="QDRANT_URL",
        description="Full Qdrant endpoint URL override (e.g. cloud URL or ':memory:')",
    )
    qdrant_collection: str = Field(
        default="enterprise_knowledge_chunks",
        alias="QDRANT_COLLECTION",
        description="Default Qdrant collection name for knowledge chunks",
    )
    qdrant_hnsw_m: int = Field(
        default=16,
        alias="QDRANT_HNSW_M",
        description="Number of bidirectional edges per HNSW node (default: 16)",
    )
    qdrant_hnsw_ef_construct: int = Field(
        default=128,
        alias="QDRANT_HNSW_EF_CONSTRUCT",
        description="Size of dynamic candidate list during HNSW construction (default: 128)",
    )
    qdrant_hnsw_ef: int = Field(
        default=64,
        alias="QDRANT_HNSW_EF",
        description="Size of dynamic candidate list during query time (default: 64)",
    )
    qdrant_prefer_grpc: bool = Field(
        default=False,
        alias="QDRANT_PREFER_GRPC",
        description="Whether to prefer gRPC over HTTP for vector operations",
    )

    # ------------------------------------------
    # Redis & Celery Background Worker Settings
    # ------------------------------------------
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        alias="REDIS_URL",
        description="Redis connection URL for cache and distributed locking",
    )
    celery_broker_url: str = Field(
        default="redis://localhost:6379/1",
        alias="CELERY_BROKER_URL",
        description="Celery message broker URL",
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/2",
        alias="CELERY_RESULT_BACKEND",
        description="Celery task result backend URL",
    )
    celery_task_default_queue: str = Field(
        default="document_ingestion",
        alias="CELERY_TASK_DEFAULT_QUEUE",
        description="Default queue name for document ingestion tasks",
    )
    celery_worker_concurrency: int = Field(
        default=4,
        alias="CELERY_WORKER_CONCURRENCY",
        description="Number of concurrent worker processes",
    )
    celery_task_time_limit: int = Field(
        default=600,
        alias="CELERY_TASK_TIME_LIMIT",
        description="Hard time limit in seconds for tasks before termination (default: 600s / 10m)",
    )
    celery_task_soft_time_limit: int = Field(
        default=540,
        alias="CELERY_TASK_SOFT_TIME_LIMIT",
        description="Soft time limit in seconds for tasks before SoftTimeLimitExceeded (default: 540s / 9m)",
    )
    celery_task_max_retries: int = Field(
        default=3,
        alias="CELERY_TASK_MAX_RETRIES",
        description="Maximum retry attempts on transient task failures",
    )
    celery_task_retry_backoff: int = Field(
        default=2,
        alias="CELERY_TASK_RETRY_BACKOFF",
        description="Exponential backoff factor for retries",
    )
    celery_task_retry_backoff_max: int = Field(
        default=60,
        alias="CELERY_TASK_RETRY_BACKOFF_MAX",
        description="Maximum backoff delay in seconds between retries",
    )
    celery_task_acks_late: bool = Field(
        default=True,
        alias="CELERY_TASK_ACKS_LATE",
        description="Acknowledge task only after execution completes to prevent task loss on crash",
    )
    celery_worker_prefetch_multiplier: int = Field(
        default=1,
        alias="CELERY_WORKER_PREFETCH_MULTIPLIER",
        description="Prefetch limit for worker processes (1 for heavy compute/OCR tasks)",
    )
    celery_task_always_eager: bool = Field(
        default=False,
        alias="CELERY_TASK_ALWAYS_EAGER",
        description="Whether to execute tasks synchronously locally (useful for unit testing and offline CI)",
    )


@lru_cache(maxsize=1)
def get_ingestion_settings() -> IngestionSettings:
    """Provide a cached singleton instance of IngestionSettings."""
    return IngestionSettings()
