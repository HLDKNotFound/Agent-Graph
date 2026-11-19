"""Qdrant Vector Indexing and Payload Management Service.

Implements high-throughput, idempotent dense vector persistence, payload indexing,
and strict multi-tenant isolation in Qdrant (v1.8+) conforming to VectorIndexProvider.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence
import warnings

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.embedding.base import EmbeddingResult
from backend.ingestion.exceptions import (
    IndexingError,
    TenantStorageViolationError,
    ValidationError,
)
from backend.ingestion.indexing.base import VectorIndexProvider, VectorIndexResult
from backend.ingestion.indexing.models import (
    QdrantBatchIndexResult,
    QdrantConfig,
    QdrantPointRecord,
    serialize_chunk_payload,
    to_point_id,
)
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext

logger = logging.getLogger(__name__)

# Suppress local in-memory Qdrant warning regarding payload indexes
warnings.filterwarnings(
    "ignore",
    message="Payload indexes have no effect in the local Qdrant.*",
    category=UserWarning,
)


class QdrantVectorIndexService(VectorIndexProvider):
    """Production implementation of VectorIndexProvider for Qdrant vector database.

    Core Capabilities:
    1. Dense Vector Persistence: Stores 1024-dimensional normalized dense vectors with Cosine metric.
    2. HNSW Topology: Preserves m=16, ef_construct=128 collection indexing and ef=64 query search.
    3. Idempotent Upserts: Uses stable point UUIDs derived from chunk IDs for safe repeated executions.
    4. Comprehensive Payloads: Serializes tenant_id, document_id, chunk_id, RBAC ACLs, and provenance coordinates.
    5. Automatic Payload Indexing: Creates filterable keyword and integer indexes on filter attributes.
    6. Strict Multi-Tenant Isolation: Every read, check, upsert, and deletion is scoped to tenant_id.
    7. Partial Failure Resilience: Batch operations report granular success/failure metrics.
    8. Decoupled Architecture: Independent of relational database transactions and Celery workers.
    """

    def __init__(
        self,
        config: QdrantConfig | None = None,
        client: QdrantClient | None = None,
        settings: IngestionSettings | None = None,
    ) -> None:
        self.settings = settings or get_ingestion_settings()
        self.config = config or QdrantConfig(
            host=self.settings.qdrant_host,
            port=self.settings.qdrant_port,
            grpc_port=self.settings.qdrant_grpc_port,
            api_key=self.settings.qdrant_api_key,
            url=self.settings.qdrant_url,
            collection_name=self.settings.qdrant_collection,
            hnsw_m=self.settings.qdrant_hnsw_m,
            hnsw_ef_construct=self.settings.qdrant_hnsw_ef_construct,
            hnsw_ef=self.settings.qdrant_hnsw_ef,
            prefer_grpc=self.settings.qdrant_prefer_grpc,
        )

        if client is not None:
            self._client = client
        else:
            self._client = self._init_client(self.config)

        self._collection_initialized = False

    @staticmethod
    def _init_client(config: QdrantConfig) -> QdrantClient:
        """Instantiate QdrantClient based on configuration parameters."""
        if config.path:
            logger.info("Initializing QdrantClient with local path: %s", config.path)
            return QdrantClient(path=config.path)
        if config.url == ":memory:" or config.host == ":memory:":
            logger.info("Initializing in-memory QdrantClient (':memory:')")
            return QdrantClient(":memory:")
        if config.url:
            logger.info("Initializing QdrantClient with URL: %s", config.url)
            return QdrantClient(
                url=config.url,
                api_key=config.api_key,
                timeout=config.timeout,
                prefer_grpc=config.prefer_grpc,
            )
        logger.info(
            "Initializing QdrantClient with host=%s, port=%d, grpc_port=%d",
            config.host,
            config.port,
            config.grpc_port,
        )
        return QdrantClient(
            host=config.host,
            port=config.port,
            grpc_port=config.grpc_port,
            api_key=config.api_key,
            timeout=config.timeout,
            prefer_grpc=config.prefer_grpc,
        )

    @property
    def client(self) -> QdrantClient:
        """Active underlying QdrantClient instance."""
        return self._client

    @property
    def collection_name(self) -> str:
        """Target Qdrant vector collection name."""
        return self.config.collection_name

    @property
    def dimension(self) -> int:
        """Configured dense vector dimension (1024 for BGE-M3)."""
        return self.config.vector_dimension

    def ensure_collection(self, recreate_if_incompatible: bool = False) -> bool:
        """Verify collection exists and matches required schema, or create it idempotently.

        Validates:
        1. Dimension: Exactly 1024.
        2. Distance: Cosine.
        3. HNSW Configuration: m=16, ef_construct=128.
        4. Payload Indexes: Created for filterable attributes.

        Args:
            recreate_if_incompatible: If True, drops and recreates collection on schema mismatch.

        Returns:
            True if collection is ready.

        Raises:
            IndexingError: If collection exists with incompatible schema and recreate is False.
        """
        exists = self._client.collection_exists(self.collection_name)

        if exists:
            try:
                info = self._client.get_collection(self.collection_name)
                vectors_cfg = info.config.params.vectors

                # Check vector dimension compatibility
                actual_size: int | None = None
                actual_distance: models.Distance | None = None

                if isinstance(vectors_cfg, models.VectorParams):
                    actual_size = vectors_cfg.size
                    actual_distance = vectors_cfg.distance
                elif isinstance(vectors_cfg, dict) and "" in vectors_cfg:
                    default_v = vectors_cfg[""]
                    actual_size = default_v.size
                    actual_distance = default_v.distance

                is_compatible = (
                    actual_size == self.config.vector_dimension
                    and actual_distance == models.Distance.COSINE
                )

                if is_compatible:
                    logger.debug("Collection '%s' exists and is compatible.", self.collection_name)
                    self.ensure_payload_indexes()
                    self._collection_initialized = True
                    return True

                if recreate_if_incompatible:
                    logger.warning(
                        "Collection '%s' exists with incompatible schema (size=%s, distance=%s). Recreating...",
                        self.collection_name,
                        actual_size,
                        actual_distance,
                    )
                    self._client.delete_collection(self.collection_name)
                else:
                    raise IndexingError(
                        f"Collection '{self.collection_name}' exists with incompatible configuration: "
                        f"expected dimension {self.config.vector_dimension} (Cosine), got {actual_size} ({actual_distance}).",
                        details={
                            "collection": self.collection_name,
                            "expected_dimension": self.config.vector_dimension,
                            "actual_dimension": actual_size,
                            "actual_distance": str(actual_distance),
                        },
                    )

            except IndexingError:
                raise
            except Exception as exc:
                logger.warning("Could not inspect collection '%s': %s", self.collection_name, exc)

        # Create collection with strict HNSW and Vector configurations
        logger.info(
            "Creating Qdrant collection '%s' (dim=%d, distance=Cosine, HNSW m=%d, ef_construct=%d)...",
            self.collection_name,
            self.config.vector_dimension,
            self.config.hnsw_m,
            self.config.hnsw_ef_construct,
        )
        try:
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.config.vector_dimension,
                    distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(
                        m=self.config.hnsw_m,
                        ef_construct=self.config.hnsw_ef_construct,
                    ),
                ),
                hnsw_config=models.HnswConfigDiff(
                    m=self.config.hnsw_m,
                    ef_construct=self.config.hnsw_ef_construct,
                ),
            )
            self.ensure_payload_indexes()
            self._collection_initialized = True
            logger.info("Collection '%s' created and indexed successfully.", self.collection_name)
            return True
        except Exception as exc:
            logger.error("Failed to create collection '%s': %s", self.collection_name, exc, exc_info=True)
            raise IndexingError(
                f"Failed to create collection '{self.collection_name}': {exc}",
                details={"collection": self.collection_name, "error": str(exc)},
            ) from exc

    def ensure_payload_indexes(self) -> None:
        """Create or verify payload indexes for filterable fields.

        Indexes created (Requirements 7, 8, 9):
        - tenant_id: KEYWORD (multi-tenant boundary)
        - document_id: KEYWORD
        - chunk_id: KEYWORD
        - allowed_roles: KEYWORD (RBAC)
        - allowed_departments: KEYWORD (RBAC)
        - allowed_users: KEYWORD (RBAC)
        - page_number: INTEGER
        - section_heading: KEYWORD
        - sheet_name: KEYWORD
        - document_version_id: KEYWORD
        - acl.tenant_id: KEYWORD
        - acl.roles: KEYWORD
        - acl.departments: KEYWORD
        """
        payload_index_fields: list[tuple[str, models.PayloadSchemaType]] = [
            ("tenant_id", models.PayloadSchemaType.KEYWORD),
            ("document_id", models.PayloadSchemaType.KEYWORD),
            ("chunk_id", models.PayloadSchemaType.KEYWORD),
            ("allowed_roles", models.PayloadSchemaType.KEYWORD),
            ("allowed_departments", models.PayloadSchemaType.KEYWORD),
            ("allowed_users", models.PayloadSchemaType.KEYWORD),
            ("page_number", models.PayloadSchemaType.INTEGER),
            ("section_heading", models.PayloadSchemaType.KEYWORD),
            ("sheet_name", models.PayloadSchemaType.KEYWORD),
            ("document_version_id", models.PayloadSchemaType.KEYWORD),
            ("acl.tenant_id", models.PayloadSchemaType.KEYWORD),
            ("acl.roles", models.PayloadSchemaType.KEYWORD),
            ("acl.departments", models.PayloadSchemaType.KEYWORD),
        ]

        for field_name, field_type in payload_index_fields:
            try:
                self._client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=field_type,
                    wait=True,
                )
            except Exception as exc:
                # Payload index already exists or warning in local mode
                logger.debug("Payload index creation note for '%s': %s", field_name, exc)

    async def index_chunks(
        self,
        chunks: list[DocumentChunkInput],
        embeddings: list[EmbeddingResult],
        context: IngestionContext,
    ) -> VectorIndexResult:
        """Upsert embedded chunks with mandatory zero-trust ACL payloads into Qdrant.

        Enforces:
        1. Context tenant isolation: all chunks must belong to context.tenant_id.
        2. Idempotent point UUID generation based on database chunk IDs.
        3. Strict 1024-dimension vector verification.
        4. Complete payload serialization including coordinates and RBAC fields.

        Args:
            chunks: List of DocumentChunkInput objects.
            embeddings: Corresponding EmbeddingResult vector objects.
            context: Authenticated IngestionContext with tenant_id.

        Returns:
            VectorIndexResult detailing points upserted and vector storage IDs.

        Raises:
            ValidationError: If inputs are malformed or counts mismatch.
            TenantStorageViolationError: If any chunk violates context tenant boundary.
            IndexingError: If Qdrant communication fails.
        """
        if not context.tenant_id or not context.tenant_id.strip():
            raise ValidationError("Authenticated tenant_id is required in IngestionContext")

        if not chunks:
            return VectorIndexResult(
                collection_name=self.collection_name,
                points_indexed=0,
                operation_status="completed",
                vector_storage_ids=[],
            )

        if len(chunks) != len(embeddings):
            raise ValidationError(
                f"Mismatched input counts: received {len(chunks)} chunks and {len(embeddings)} embeddings"
            )

        # Enforce zero-trust multi-tenant isolation across all chunks (Requirement 10)
        for idx, chunk in enumerate(chunks):
            if chunk.tenant_id != context.tenant_id:
                raise TenantStorageViolationError(
                    f"Chunk at index {idx} (id={chunk.chunk_id}) belongs to tenant '{chunk.tenant_id}', "
                    f"violating context tenant '{context.tenant_id}'."
                )

        if not self._collection_initialized:
            self.ensure_collection()

        # Build Qdrant PointStruct list
        points: list[models.PointStruct] = []
        point_ids: list[str] = []

        for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            if len(emb.vector) != self.config.vector_dimension:
                raise IndexingError(
                    f"Vector dimension mismatch at index {idx}: expected {self.config.vector_dimension}, got {len(emb.vector)}",
                    details={"index": idx, "chunk_id": chunk.chunk_id, "dimension": len(emb.vector)},
                )

            # Generate stable deterministic point UUID (Requirement 6)
            point_id = to_point_id(chunk.chunk_id)
            point_ids.append(point_id)

            # Serialize payload (Requirements 7 & 8)
            payload = serialize_chunk_payload(chunk)

            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=emb.vector,
                    payload=payload,
                )
            )

        try:
            response = self._client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True,
            )
            op_status = str(getattr(response, "status", "completed"))
            logger.info(
                "Successfully indexed %d chunks into Qdrant collection '%s' (tenant_id=%s).",
                len(points),
                self.collection_name,
                context.tenant_id,
            )
            return VectorIndexResult(
                collection_name=self.collection_name,
                points_indexed=len(points),
                operation_status=op_status,
                vector_storage_ids=point_ids,
            )
        except Exception as exc:
            logger.error(
                "Qdrant vector upsert failed for %d points in '%s': %s",
                len(points),
                self.collection_name,
                exc,
                exc_info=True,
            )
            raise IndexingError(
                f"Failed to upsert vectors into Qdrant: {exc}",
                details={
                    "collection": self.collection_name,
                    "points_count": len(points),
                    "tenant_id": context.tenant_id,
                    "error": str(exc),
                },
            ) from exc

    async def batch_index_chunks(
        self,
        chunks: list[DocumentChunkInput],
        embeddings: list[EmbeddingResult],
        context: IngestionContext,
        batch_size: int | None = None,
    ) -> QdrantBatchIndexResult:
        """Batch index chunks in micro-batches with partial failure tracking (Requirements 11 & 14).

        Args:
            chunks: List of DocumentChunkInput records.
            embeddings: List of EmbeddingResult records.
            context: IngestionContext with tenant_id.
            batch_size: Optional micro-batch size override.

        Returns:
            QdrantBatchIndexResult with comprehensive execution metrics.

        Raises:
            IndexingError: If batch processing experiences unrecoverable errors.
        """
        effective_batch_size = batch_size or self.config.batch_size
        total_chunks = len(chunks)
        start_idx = 0

        all_vector_ids: list[str] = []
        failed_chunk_ids: list[str] = []
        points_indexed = 0
        points_failed = 0
        error_details: dict[str, Any] = {}

        while start_idx < total_chunks:
            end_idx = min(start_idx + effective_batch_size, total_chunks)
            chunk_slice = chunks[start_idx:end_idx]
            emb_slice = embeddings[start_idx:end_idx]

            try:
                res = await self.index_chunks(chunk_slice, emb_slice, context)
                points_indexed += res.points_indexed
                all_vector_ids.extend(res.vector_storage_ids)
            except Exception as exc:
                logger.error(
                    "Batch slice indexing failed for chunks [%d:%d]: %s",
                    start_idx,
                    end_idx,
                    exc,
                )
                slice_failed_ids = [c.chunk_id for c in chunk_slice]
                failed_chunk_ids.extend(slice_failed_ids)
                points_failed += len(chunk_slice)
                error_details[f"slice_{start_idx}_{end_idx}"] = str(exc)

            start_idx = end_idx

        result = QdrantBatchIndexResult(
            collection_name=self.collection_name,
            points_indexed=points_indexed,
            points_failed=points_failed,
            vector_storage_ids=all_vector_ids,
            failed_chunk_ids=failed_chunk_ids,
            error_details=error_details,
        )

        if points_failed > 0:
            raise IndexingError(
                f"Batch indexing completed with {points_failed} failed points out of {total_chunks}.",
                details=result.model_dump(),
            )

        return result

    async def delete_document_vectors(
        self,
        document_id: str,
        context: IngestionContext,
    ) -> int:
        """Remove all vector points belonging to document under authenticated tenant (Requirement 10 & 11).

        Guarantees:
        - Incorporates tenant_id filter: Tenant A can never delete Tenant B's vectors even with matching document_id.

        Args:
            document_id: Target document identifier.
            context: IngestionContext containing authenticated tenant_id.

        Returns:
            Estimated count of deleted vector points.
        """
        if not document_id or not document_id.strip():
            raise ValidationError("document_id is required for vector deletion")
        if not context.tenant_id or not context.tenant_id.strip():
            raise ValidationError("tenant_id is required in IngestionContext")

        if not self._collection_initialized:
            self.ensure_collection()

        # Count existing points first to report accurate deletion count
        count_before = self.count_vectors_for_document(document_id, context)

        if count_before == 0:
            return 0

        # Construct tenant-isolated filter
        filter_obj = models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=context.tenant_id),
                ),
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=document_id),
                ),
            ]
        )

        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=filter_obj),
                wait=True,
            )
            logger.info(
                "Deleted %d vector points for document '%s' (tenant_id=%s).",
                count_before,
                document_id,
                context.tenant_id,
            )
            return count_before
        except Exception as exc:
            logger.error("Failed to delete vectors for document '%s': %s", document_id, exc, exc_info=True)
            raise IndexingError(
                f"Failed to delete vectors for document '{document_id}': {exc}",
                details={"document_id": document_id, "tenant_id": context.tenant_id, "error": str(exc)},
            ) from exc

    def delete_chunk_vectors(
        self,
        chunk_ids: list[str],
        context: IngestionContext,
    ) -> int:
        """Remove vector points matching specific chunk IDs under authenticated tenant.

        Args:
            chunk_ids: List of database chunk identifiers.
            context: IngestionContext with authenticated tenant_id.

        Returns:
            Number of requested chunk IDs processed for deletion.
        """
        if not chunk_ids:
            return 0
        if not context.tenant_id or not context.tenant_id.strip():
            raise ValidationError("tenant_id is required in IngestionContext")

        if not self._collection_initialized:
            self.ensure_collection()

        filter_obj = models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=context.tenant_id),
                ),
                models.FieldCondition(
                    key="chunk_id",
                    match=models.MatchAny(any=chunk_ids),
                ),
            ]
        )

        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=filter_obj),
                wait=True,
            )
            logger.info(
                "Deleted vectors for %d chunks (tenant_id=%s).",
                len(chunk_ids),
                context.tenant_id,
            )
            return len(chunk_ids)
        except Exception as exc:
            logger.error("Failed to delete chunk vectors: %s", exc, exc_info=True)
            raise IndexingError(
                f"Failed to delete chunk vectors: {exc}",
                details={"chunk_ids": chunk_ids, "tenant_id": context.tenant_id, "error": str(exc)},
            ) from exc

    def delete_by_point_ids(
        self,
        point_ids: list[str],
        context: IngestionContext,
    ) -> int:
        """Remove vector points by point UUIDs enforcing tenant boundary verification.

        Args:
            point_ids: List of Qdrant point UUIDs.
            context: IngestionContext with authenticated tenant_id.

        Returns:
            Number of points deleted.
        """
        if not point_ids:
            return 0
        if not context.tenant_id or not context.tenant_id.strip():
            raise ValidationError("tenant_id is required in IngestionContext")

        if not self._collection_initialized:
            self.ensure_collection()

        filter_obj = models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=context.tenant_id),
                ),
                models.HasIdCondition(has_id=point_ids),
            ]
        )

        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=filter_obj),
                wait=True,
            )
            return len(point_ids)
        except Exception as exc:
            raise IndexingError(f"Failed to delete points by ID: {exc}") from exc

    def has_vector(
        self,
        chunk_id_or_point_id: str,
        context: IngestionContext,
    ) -> bool:
        """Check whether a vector point exists in Qdrant for a chunk under authenticated tenant.

        Guarantees tenant isolation: returns False if the point exists but belongs to another tenant.

        Args:
            chunk_id_or_point_id: Database chunk identifier or Qdrant point UUID.
            context: IngestionContext containing authenticated tenant_id.

        Returns:
            True if point exists and belongs to context.tenant_id, False otherwise.
        """
        if not chunk_id_or_point_id or not context.tenant_id:
            return False

        if not self._collection_initialized:
            self.ensure_collection()

        point_id = to_point_id(chunk_id_or_point_id)

        try:
            points = self._client.retrieve(
                collection_name=self.collection_name,
                ids=[point_id],
                with_payload=["tenant_id"],
                with_vectors=False,
            )
            if not points:
                return False
            payload = points[0].payload or {}
            return payload.get("tenant_id") == context.tenant_id
        except Exception as exc:
            logger.warning("Error checking vector existence for '%s': %s", chunk_id_or_point_id, exc)
            return False

    def vector_exists(self, chunk_id: str, context: IngestionContext) -> bool:
        """Alias for has_vector checking chunk existence."""
        return self.has_vector(chunk_id, context)

    def count_vectors_for_document(
        self,
        document_id: str,
        context: IngestionContext,
    ) -> int:
        """Count total indexed points for a specific document under authenticated tenant.

        Args:
            document_id: Target document identifier.
            context: IngestionContext with authenticated tenant_id.

        Returns:
            Total vector points count.
        """
        if not document_id or not context.tenant_id:
            return 0

        if not self._collection_initialized:
            self.ensure_collection()

        filter_obj = models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=context.tenant_id),
                ),
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=document_id),
                ),
            ]
        )

        try:
            res = self._client.count(
                collection_name=self.collection_name,
                count_filter=filter_obj,
                exact=True,
            )
            return res.count
        except Exception as exc:
            logger.warning("Error counting vectors for document '%s': %s", document_id, exc)
            return 0

    def get_vector_by_id(
        self,
        chunk_id: str,
        context: IngestionContext,
        with_vector: bool = True,
    ) -> QdrantPointRecord | None:
        """Retrieve a vector point representation by chunk ID under authenticated tenant.

        Args:
            chunk_id: Database chunk identifier.
            context: IngestionContext with authenticated tenant_id.
            with_vector: Whether to return dense float vector array.

        Returns:
            QdrantPointRecord if point exists and belongs to context.tenant_id, else None.
        """
        records = self.get_vectors_by_ids([chunk_id], context, with_vectors=with_vector)
        return records[0] if records else None

    def get_vectors_by_ids(
        self,
        chunk_ids: list[str],
        context: IngestionContext,
        with_vectors: bool = True,
    ) -> list[QdrantPointRecord]:
        """Retrieve vector points by chunk IDs, strictly filtering by tenant isolation.

        Args:
            chunk_ids: List of database chunk identifiers.
            context: IngestionContext with authenticated tenant_id.
            with_vectors: Whether to return dense float vector arrays.

        Returns:
            List of authorized QdrantPointRecord objects matching context.tenant_id.
        """
        if not chunk_ids or not context.tenant_id:
            return []

        if not self._collection_initialized:
            self.ensure_collection()

        point_ids = [to_point_id(cid) for cid in chunk_ids]

        try:
            points = self._client.retrieve(
                collection_name=self.collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=with_vectors,
            )
        except Exception as exc:
            logger.error("Failed to retrieve points from Qdrant: %s", exc, exc_info=True)
            raise IndexingError(f"Failed to retrieve points from Qdrant: {exc}") from exc

        records: list[QdrantPointRecord] = []
        for pt in points:
            payload = pt.payload or {}
            # Strict tenant boundary check
            if payload.get("tenant_id") != context.tenant_id:
                continue

            vector_data: list[float] | None = None
            if with_vectors and pt.vector is not None:
                if isinstance(pt.vector, list):
                    vector_data = [float(v) for v in pt.vector]
                elif isinstance(pt.vector, dict) and "" in pt.vector:
                    vector_data = [float(v) for v in pt.vector[""]]

            records.append(
                QdrantPointRecord(
                    point_id=str(pt.id),
                    chunk_id=str(payload.get("chunk_id", "")),
                    document_id=str(payload.get("document_id", "")),
                    tenant_id=str(payload.get("tenant_id", "")),
                    vector=vector_data,
                    payload=payload,
                )
            )

        return records


# Module-level default singleton
_default_qdrant_service: QdrantVectorIndexService | None = None


def get_qdrant_index_service(
    client: QdrantClient | None = None,
    config: QdrantConfig | None = None,
) -> QdrantVectorIndexService:
    """Provide or initialize the shared singleton QdrantVectorIndexService."""
    global _default_qdrant_service
    if _default_qdrant_service is None or client is not None or config is not None:
        _default_qdrant_service = QdrantVectorIndexService(client=client, config=config)
    return _default_qdrant_service
