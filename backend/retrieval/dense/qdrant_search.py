"""Dense semantic vector search engine leveraging Qdrant with zero-trust RBAC pre-filtering."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Sequence

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from backend.retrieval.config import RetrievalSettings, get_retrieval_settings
from backend.retrieval.exceptions import (
    DenseRetrievalError,
    EmptyRetrievalResultWarning,
    RetrievalSecurityViolationError,
)
from backend.retrieval.types.chunk import (
    ChunkLocatorCoordinates,
    RetrievedChunk,
    RetrievedChunkSource,
)
from backend.retrieval.types.context import RetrievalContext

logger = logging.getLogger(__name__)


class QdrantDenseRetriever:
    """Stage-1 Dense Semantic Retrieval Service backed by Qdrant (v1.8+).

    Capabilities:
    1. Zero-Trust Security Pre-Filtering: Injects tenant isolation and RBAC role/department constraints
       at vector exploration time before passages enter memory.
    2. HNSW Search Optimization: Executes queries with runtime exploration factor ef=64.
    3. Coordinate & Provenance Mapping: Extracts physical coordinates (page, heading, sheet, row range)
       into immutable RetrievedChunk contracts.
    4. Resilient Error Handling: Gracefully traps network timeouts and Qdrant errors.
    """

    def __init__(
        self,
        settings: RetrievalSettings | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        self.settings = settings or get_retrieval_settings()
        self.collection_name = self.settings.qdrant_collection
        self._client = client or self._init_client(self.settings)

    @staticmethod
    def _init_client(settings: RetrievalSettings) -> QdrantClient:
        """Instantiate QdrantClient from dynamic settings."""
        if settings.qdrant_url == ":memory:" or settings.qdrant_host == ":memory:":
            logger.info("Initializing in-memory QdrantClient (':memory:') for dense retrieval")
            return QdrantClient(":memory:")
        if settings.qdrant_url:
            logger.info("Connecting to Qdrant via URL: %s", settings.qdrant_url)
            return QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                timeout=10.0,
            )
        logger.info(
            "Connecting to Qdrant at %s:%d (collection: %s)",
            settings.qdrant_host,
            settings.qdrant_port,
            settings.qdrant_collection,
        )
        return QdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            grpc_port=settings.qdrant_grpc_port,
            api_key=settings.qdrant_api_key,
            timeout=10.0,
        )

    def build_rbac_filter(self, context: RetrievalContext) -> models.Filter:
        """Construct Stage-1 zero-trust Qdrant boolean filter.

        Enforces:
        - MUST: tenant_id == context.tenant_id
        - MUST (Security Barrier):
            SHOULD:
              - role in allowed_roles
              - role in acl.roles
              - department in allowed_departments
              - department in acl.departments
              - user_id in allowed_users
              - classification in context.classifications OR classification == 'PUBLIC'

        Args:
            context: Validated RetrievalContext for authenticated caller.

        Returns:
            qdrant_client.models.Filter containing combined security expressions.
        """
        if not context.tenant_id:
            raise RetrievalSecurityViolationError("Tenant ID is required for dense retrieval")

        # Invariant 1: Mandatory Tenant Isolation
        tenant_condition = models.FieldCondition(
            key="tenant_id",
            match=models.MatchValue(value=str(context.tenant_id)),
        )

        # Invariant 2: RBAC and Clearance Access Barriers
        acl_should_clauses: list[models.Condition] = []

        # 2a. Public documents are universally readable within tenant
        acl_should_clauses.append(
            models.FieldCondition(
                key="classification",
                match=models.MatchValue(value="PUBLIC"),
            )
        )
        acl_should_clauses.append(
            models.FieldCondition(
                key="acl.classification",
                match=models.MatchValue(value="PUBLIC"),
            )
        )

        # 2b. Classification clearance levels (e.g. INTERNAL, CONFIDENTIAL)
        if context.classifications:
            clean_classifications = [c.strip().upper() for c in context.classifications]
            acl_should_clauses.append(
                models.FieldCondition(
                    key="classification",
                    match=models.MatchAny(any=clean_classifications),
                )
            )
            acl_should_clauses.append(
                models.FieldCondition(
                    key="acl.classification",
                    match=models.MatchAny(any=clean_classifications),
                )
            )

        # 2c. Role-based matching
        if context.roles:
            clean_roles = [r.strip() for r in context.roles if r.strip()]
            if clean_roles:
                acl_should_clauses.append(
                    models.FieldCondition(
                        key="allowed_roles",
                        match=models.MatchAny(any=clean_roles),
                    )
                )
                acl_should_clauses.append(
                    models.FieldCondition(
                        key="acl.roles",
                        match=models.MatchAny(any=clean_roles),
                    )
                )

        # 2d. Department-based matching
        if context.departments:
            clean_depts = [d.strip() for d in context.departments if d.strip()]
            if clean_depts:
                acl_should_clauses.append(
                    models.FieldCondition(
                        key="allowed_departments",
                        match=models.MatchAny(any=clean_depts),
                    )
                )
                acl_should_clauses.append(
                    models.FieldCondition(
                        key="acl.departments",
                        match=models.MatchAny(any=clean_depts),
                    )
                )

        # 2e. Direct user assignment
        if context.user_id:
            acl_should_clauses.append(
                models.FieldCondition(
                    key="allowed_users",
                    match=models.MatchValue(value=str(context.user_id)),
                )
            )

        # Combine: tenant_id MUST match AND at least one ACL should clause MUST match
        rbac_barrier = models.Filter(should=acl_should_clauses)
        return models.Filter(must=[tenant_condition, rbac_barrier])

    def search_sync(
        self,
        query_vector: Sequence[float],
        context: RetrievalContext,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Synchronously execute Qdrant dense vector similarity search.

        Args:
            query_vector: 1024-dimensional normalized dense embedding.
            context: Zero-trust RetrievalContext.
            top_k: Candidate ceiling (defaults to settings.dense_top_k = 40).

        Returns:
            Ranked list of RetrievedChunk instances.
        """
        limit = top_k or self.settings.dense_top_k
        qdrant_filter = self.build_rbac_filter(context)
        search_params = models.SearchParams(
            hnsw_ef=self.settings.qdrant_hnsw_ef,
            exact=False,
        )

        try:
            if hasattr(self._client, "query_points"):
                response = self._client.query_points(
                    collection_name=self.collection_name,
                    query=list(query_vector),
                    query_filter=qdrant_filter,
                    search_params=search_params,
                    limit=limit,
                    with_payload=True,
                    with_vectors=False,
                )
                points = response.points
            elif hasattr(self._client, "search"):
                points = self._client.search(
                    collection_name=self.collection_name,
                    query_vector=list(query_vector),
                    query_filter=qdrant_filter,
                    search_params=search_params,
                    limit=limit,
                    with_payload=True,
                    with_vectors=False,
                )
            else:
                raise AttributeError("QdrantClient has neither 'query_points' nor 'search' method")
        except Exception as exc:
            logger.error("Qdrant dense vector search failed: %s", exc, exc_info=True)
            raise DenseRetrievalError(
                f"Qdrant dense vector search failed on collection '{self.collection_name}': {exc}",
                details={"collection": self.collection_name, "error": str(exc)},
            ) from exc

        chunks: list[RetrievedChunk] = []
        for rank, point in enumerate(points, start=1):
            payload = point.payload or {}
            chunk = self._map_point_to_retrieved_chunk(point.id, point.score, rank, payload)
            chunks.append(chunk)

        if not chunks:
            logger.warning(
                "Dense retrieval returned 0 passages for tenant=%s, user=%s",
                context.tenant_id,
                context.user_id,
            )

        return chunks

    async def search(
        self,
        query_vector: Sequence[float],
        context: RetrievalContext,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Asynchronously execute Qdrant dense vector similarity search in thread pool."""
        return await asyncio.to_thread(self.search_sync, query_vector, context, top_k)

    def _map_point_to_retrieved_chunk(
        self,
        point_id: Any,
        score: float,
        rank: int,
        payload: dict[str, Any],
    ) -> RetrievedChunk:
        """Map raw Qdrant point payload to typed RetrievedChunk model."""
        # Chunk ID extraction with fallback to point ID
        raw_chunk_id = payload.get("chunk_id") or str(point_id)
        try:
            chunk_id = uuid.UUID(str(raw_chunk_id))
        except ValueError:
            chunk_id = uuid.uuid5(uuid.NAMESPACE_DNS, str(raw_chunk_id))

        # Document ID extraction
        raw_doc_id = payload.get("document_id") or str(uuid.uuid4())
        try:
            doc_id = uuid.UUID(str(raw_doc_id))
        except ValueError:
            doc_id = uuid.uuid5(uuid.NAMESPACE_DNS, str(raw_doc_id))

        # Document Version ID extraction
        raw_ver_id = payload.get("document_version_id")
        doc_version_id = None
        if raw_ver_id:
            try:
                doc_version_id = uuid.UUID(str(raw_ver_id))
            except ValueError:
                doc_version_id = uuid.uuid5(uuid.NAMESPACE_DNS, str(raw_ver_id))

        # Department ID extraction
        raw_dept_id = payload.get("department_id")
        department_id = None
        if raw_dept_id:
            try:
                department_id = uuid.UUID(str(raw_dept_id))
            except ValueError:
                pass

        # Locator coordinates provenance
        source_loc = payload.get("source_location") or {}
        locator = ChunkLocatorCoordinates(
            page_number=payload.get("page_number") or source_loc.get("page_number"),
            section_heading=payload.get("section_heading") or source_loc.get("section_heading"),
            sheet_name=payload.get("sheet_name") or source_loc.get("sheet_name"),
            row_range=payload.get("row_range") or source_loc.get("row_range"),
            char_start=payload.get("char_start") or source_loc.get("char_start"),
            char_end=payload.get("char_end") or source_loc.get("char_end"),
            bounding_box=source_loc.get("bounding_box"),
        )

        classification = (
            payload.get("classification")
            or payload.get("acl", {}).get("classification")
            or "INTERNAL"
        )

        return RetrievedChunk(
            chunk_id=chunk_id,
            document_id=doc_id,
            document_version_id=doc_version_id,
            content=payload.get("content", ""),
            chunk_index=payload.get("chunk_index", 0),
            token_count=payload.get("token_count", 0),
            locator=locator,
            dense_score=float(score),
            dense_rank=rank,
            source=RetrievedChunkSource.DENSE,
            classification=str(classification).upper(),
            department_id=department_id,
            document_title=payload.get("original_filename") or payload.get("document_title"),
            metadata=dict(payload.get("metadata", {})),
        )
