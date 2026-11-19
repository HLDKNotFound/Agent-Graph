"""Central End-to-End Ingestion Orchestrator and Cross-Storage Consistency Engine.

Coordinates all ingestion subsystems into a reliable, verifiable workflow:
1. File validation (Format, signatures, size, sanitization)
2. SHA-256 deduplication (Exact duplicate detection and handling policy)
3. Format-specific extraction (PDF native/scanned, DOCX, XLSX, CSV)
4. Structure-aware chunking (Recursive structural, sliding window, table-aware)
5. Provenance validation (Coordinates verified against document boundaries)
6. MinHash LSH near-duplicate detection (128 permutation signatures)
7. Dense vector embedding generation (BGE-M3 1024-dim dense vectors)
8. PostgreSQL relational persistence with Transactional Outbox
9. Idempotent Qdrant vector indexing via Outbox Dispatcher
10. Saga-pattern compensation and cross-storage consistency reconciliation
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import logging
from pathlib import Path
import time
from typing import Any, BinaryIO, Sequence
import uuid

from backend.ingestion.chunking import (
    ChunkingEngine,
    default_chunking_engine,
)
from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.deduplication.chunk_deduplicator import (
    MinHashChunkDeduplicator,
    get_chunk_deduplicator,
)
from backend.ingestion.deduplication.hasher import compute_content_sha256
from backend.ingestion.deduplication.models import (
    DocumentCreateInput,
    DocumentVersionCreateInput,
    DuplicateAction,
    DuplicateHandlingPolicy,
    ExistingDocumentSummary,
)
from backend.ingestion.deduplication.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
    PostgresDocumentRepository,
)
from backend.ingestion.deduplication.service import DocumentDeduplicator
from backend.ingestion.embedding import (
    BgeM3EmbeddingService,
    EmbeddingProvider,
    get_embedding_service,
)
from backend.ingestion.exceptions import (
    DuplicateDocumentError,
    ExtractionError,
    IndexingError,
    IngestionError,
    OrchestrationError,
    ValidationError,
)
from backend.ingestion.extraction import (
    ExtractorRegistry,
    default_extractor_registry,
)
from backend.ingestion.indexing import (
    QdrantVectorIndexService,
    get_qdrant_index_service,
)
from backend.ingestion.indexing.models import to_point_id
from backend.ingestion.orchestration.base import (
    IngestionOrchestrator,
    IngestionPipelineResult,
)
from backend.ingestion.orchestration.chunk_repository import (
    ChunkRepository,
    InMemoryChunkRepository,
    PostgresChunkRepository,
)
from backend.ingestion.orchestration.compensation import CompensationManager
from backend.ingestion.orchestration.outbox import (
    InMemoryOutboxRepository,
    OutboxDispatcher,
    OutboxEvent,
    OutboxRepository,
    PostgresOutboxRepository,
)
from backend.ingestion.orchestration.reconciliation import (
    CrossStorageReconciler,
    ReconciliationReport,
)
from backend.ingestion.orchestration.state_machine import IngestionStateMachine
from backend.ingestion.storage.interfaces import StorageBackend
from backend.ingestion.storage import StorageService, get_storage_service
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.enums import (
    DocumentClassification,
    DocumentIngestionStatus,
    DocumentType,
    PipelineStage,
)
from backend.ingestion.types.metadata import DocumentMetadata
from backend.ingestion.validation.interfaces import DocumentValidator
from backend.ingestion.validation.provenance import ChunkProvenanceValidator
from backend.ingestion.validation.validator import DefaultDocumentValidator

logger = logging.getLogger(__name__)


class DefaultIngestionOrchestrator(IngestionOrchestrator):
    """Production implementation of IngestionOrchestrator coordinating all ingestion subsystems.

    Enforces:
    - Zero-trust tenant isolation.
    - Idempotent processing and safe boundary resumption.
    - Transactional Outbox pattern committing PostgreSQL records and outbox events together.
    - Saga-pattern compensating vector deletion on failures.
    - Idempotent Qdrant vector upserts.
    """

    def __init__(
        self,
        validator: DocumentValidator | None = None,
        deduplicator: DocumentDeduplicator | None = None,
        extractor_registry: ExtractorRegistry | None = None,
        chunking_engine: ChunkingEngine | None = None,
        provenance_validator: ChunkProvenanceValidator | None = None,
        chunk_deduplicator: MinHashChunkDeduplicator | None = None,
        embedding_service: EmbeddingProvider | None = None,
        qdrant_service: Any = None,
        chunk_repo: ChunkRepository | None = None,
        outbox_repo: OutboxRepository | None = None,
        document_repo: DocumentRepository | None = None,
        storage_service: StorageService | None = None,
        settings: IngestionSettings | None = None,
    ) -> None:
        self.settings = settings or get_ingestion_settings()
        self.validator = validator or DefaultDocumentValidator(self.settings)
        self.extractor_registry = extractor_registry or default_extractor_registry
        self.chunking_engine = chunking_engine or default_chunking_engine
        self.provenance_validator = provenance_validator or ChunkProvenanceValidator()
        self.chunk_deduplicator = chunk_deduplicator or get_chunk_deduplicator()

        # Database and Storage Repositories
        self.document_repo = document_repo or PostgresDocumentRepository(connection_url=self.settings.database_url)
        self.outbox_repo = outbox_repo or PostgresOutboxRepository(connection_url=self.settings.database_url)
        self.chunk_repo = chunk_repo or PostgresChunkRepository(
            connection_url=self.settings.database_url,
            outbox_repo=self.outbox_repo,
        )
        self.storage_service = storage_service or get_storage_service()

        # Deduplicator engine
        self.deduplicator = deduplicator or DocumentDeduplicator(
            repository=self.document_repo,
            settings=self.settings,
        )

        # Vector Indexing & Embeddings
        self._embedding_service = embedding_service
        self._qdrant_service = qdrant_service

        # Outbox Dispatcher
        self._outbox_dispatcher: OutboxDispatcher | None = None

        # Compensation Manager
        self._compensation_manager: CompensationManager | None = None

        # Cross-Storage Reconciler
        self._reconciler: CrossStorageReconciler | None = None

        # Concurrency Lock Map: document_id -> asyncio.Lock
        self._document_locks: dict[str, asyncio.Lock] = {}
        self._lock_mgmt = asyncio.Lock()

    @property
    def embedding_service(self) -> EmbeddingProvider:
        if self._embedding_service is None:
            self._embedding_service = get_embedding_service(self.settings)
        return self._embedding_service

    @property
    def qdrant_service(self) -> Any:
        if self._qdrant_service is None:
            self._qdrant_service = get_qdrant_index_service(settings=self.settings)
        return self._qdrant_service

    @property
    def outbox_dispatcher(self) -> OutboxDispatcher:
        if self._outbox_dispatcher is None:
            self._outbox_dispatcher = OutboxDispatcher(
                outbox_repo=self.outbox_repo,
                qdrant_service=self.qdrant_service,
                chunk_repo=self.chunk_repo,
            )
        return self._outbox_dispatcher

    @property
    def compensation_manager(self) -> CompensationManager:
        if self._compensation_manager is None:
            self._compensation_manager = CompensationManager(
                qdrant_service=self.qdrant_service,
                chunk_repo=self.chunk_repo,
                outbox_repo=self.outbox_repo,
                document_repo=self.document_repo,
                storage_service=self.storage_service,
            )
        return self._compensation_manager

    @property
    def reconciler(self) -> CrossStorageReconciler:
        if self._reconciler is None:
            self._reconciler = CrossStorageReconciler(
                chunk_repo=self.chunk_repo,
                qdrant_service=self.qdrant_service,
                embedding_service=self.embedding_service,
                outbox_repo=self.outbox_repo,
                document_repo=self.document_repo,
            )
        return self._reconciler

    async def _get_document_lock(self, document_id: str) -> asyncio.Lock:
        async with self._lock_mgmt:
            if document_id not in self._document_locks:
                self._document_locks[document_id] = asyncio.Lock()
            return self._document_locks[document_id]

    async def process_document(
        self,
        content: bytes | BinaryIO | Path,
        filename: str,
        context: IngestionContext,
        resume_document_id: str | None = None,
    ) -> IngestionPipelineResult:
        """Execute end-to-end document ingestion workflow."""
        start_time = time.perf_counter()
        tenant_id = context.tenant_id
        document_id = resume_document_id or (context.document_id if context and context.document_id else str(uuid.uuid4()))

        doc_lock = await self._get_document_lock(document_id)
        async with doc_lock:
            state_machine = IngestionStateMachine(
                document_id=document_id,
                tenant_id=tenant_id,
                initial_status=DocumentIngestionStatus.PENDING,
            )

            # Resolve contiguous bytes
            file_bytes = self._resolve_bytes(content)
            staged_point_ids: list[str] = []
            storage_path: str | None = None

            try:
                # -------------------------------------------------------------
                # Stage 1: File Validation
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.VALIDATION)
                val_result = await self.validator.validate(file_bytes, filename, context)
                if not val_result.is_valid:
                    raise ValidationError(f"File validation failed: {val_result.errors}")
                state_machine.complete_stage(PipelineStage.VALIDATION, items_processed=1)

                # -------------------------------------------------------------
                # Stage 2: Pre-Ingestion SHA-256 Deduplication
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.DEDUPLICATION)
                content_hash, _ = compute_content_sha256(file_bytes)
                existing_doc = await self.document_repo.find_active_by_hash(tenant_id, content_hash)

                if existing_doc:
                    policy = context.options.duplicate_handling_policy
                    if policy == DuplicateHandlingPolicy.REJECT.value or policy == "REJECT":
                        raise DuplicateDocumentError(
                            f"Identical active document already exists with hash {content_hash}",
                            tenant_id=tenant_id,
                            content_hash=content_hash,
                            existing_document_id=existing_doc.document_id,
                        )
                    elif policy == DuplicateHandlingPolicy.RETURN_EXISTING.value or policy == "RETURN_EXISTING":
                        # Short-circuit: Return existing without re-processing
                        state_machine.skip_stage(PipelineStage.DEDUPLICATION, reason="Identical active document found")
                        state_machine.complete_pipeline(message=f"Duplicate bypassed; reused document {existing_doc.document_id}")
                        duration = time.perf_counter() - start_time
                        return IngestionPipelineResult(
                            document_id=existing_doc.document_id,
                            tenant_id=tenant_id,
                            status=DocumentIngestionStatus.COMPLETED,
                            final_stage=PipelineStage.DEDUPLICATION,
                            chunks_created=0,
                            points_indexed=0,
                            duration_seconds=round(duration, 4),
                            metadata=None,
                            warnings=[f"Exact duplicate of {existing_doc.document_id}"],
                        )

                # Safe Boundary Resumption Check:
                # If resume_document_id was provided and chunks already exist in DB with an outbox event,
                # we can resume directly from outbox dispatch!
                if resume_document_id:
                    existing_chunks = await self.chunk_repo.find_chunks_by_document(tenant_id, resume_document_id)
                    outbox_events = await self.outbox_repo.fetch_by_document(tenant_id, resume_document_id)
                    pending_outbox = [ev for ev in outbox_events if ev.status == "PENDING"]

                    if existing_chunks and pending_outbox:
                        logger.info(
                            "[doc=%s tenant=%s] Resuming from safe boundary: %d chunks exist, dispatching outbox.",
                            resume_document_id,
                            tenant_id,
                            len(existing_chunks),
                        )
                        state_machine.skip_stage(PipelineStage.EXTRACTION, reason="Resuming with existing chunks")
                        state_machine.skip_stage(PipelineStage.CHUNKING, reason="Resuming with existing chunks")
                        state_machine.skip_stage(PipelineStage.EMBEDDING, reason="Resuming with existing chunks")
                        state_machine.skip_stage(PipelineStage.PERSISTENCE, reason="Chunks already persisted")

                        # Dispatch outbox
                        state_machine.enter_stage(PipelineStage.INDEXING)
                        for ev in pending_outbox:
                            await self.outbox_dispatcher.dispatch_event(ev, context=context)
                        state_machine.complete_stage(PipelineStage.INDEXING, items_processed=len(existing_chunks))

                        state_machine.complete_pipeline("Resumption completed successfully")
                        duration = time.perf_counter() - start_time
                        return IngestionPipelineResult(
                            document_id=resume_document_id,
                            tenant_id=tenant_id,
                            status=DocumentIngestionStatus.COMPLETED,
                            final_stage=PipelineStage.INDEXING,
                            chunks_created=len(existing_chunks),
                            points_indexed=len(existing_chunks),
                            duration_seconds=round(duration, 4),
                        )

                # Persist raw document record in PostgreSQL
                doc_input = DocumentCreateInput(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    department_id=context.department_id,
                    uploaded_by_id=context.user_id,
                    original_filename=filename,
                    normalized_filename=filename,
                    storage_uri=f"tenants/{tenant_id}/docs/{document_id}/{filename}",
                    mime_type=val_result.detected_mime_type or "application/octet-stream",
                    file_size_bytes=len(file_bytes),
                    content_hash=content_hash,
                    classification=context.classification or DocumentClassification.INTERNAL,
                    status=DocumentIngestionStatus.PROCESSING,
                    metadata={"pipeline": "orchestrator_v1"},
                )
                saved_doc = await self.document_repo.register_document(doc_input)
                document_id = saved_doc.document_id
                state_machine.complete_stage(PipelineStage.DEDUPLICATION, items_processed=1)

                # -------------------------------------------------------------
                # Stage 3: Format-Specific Extraction
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.EXTRACTION)
                doc_type = DocumentType.from_filename(filename)
                extractor = self.extractor_registry.get_extractor(doc_type)
                extraction_result = await extractor.extract(file_bytes, context)
                if not extraction_result.elements:
                    raise ExtractionError(f"No textual elements extracted from file: {filename}")
                state_machine.complete_stage(PipelineStage.EXTRACTION, items_processed=len(extraction_result.elements))

                # -------------------------------------------------------------
                # Stage 4: Structure-Aware Chunking
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.CHUNKING)
                chunks = await self.chunking_engine.chunk_document(extraction_result, context)
                if not chunks:
                    raise IngestionError(f"Chunking engine produced 0 chunks for document {document_id}")
                # Ensure document_id is set across all chunks
                for c in chunks:
                    c.document_id = document_id
                state_machine.complete_stage(PipelineStage.CHUNKING, items_processed=len(chunks))

                # -------------------------------------------------------------
                # Stage 5: Provenance Coordinates Validation
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.VALIDATION, message="Validating chunk provenance locators")
                prov_report = self.provenance_validator.validate_chunks(
                    chunks=chunks,
                    context=context,
                    extraction_result=extraction_result,
                )
                if not prov_report.is_valid:
                    raise ValidationError(
                        f"Chunk provenance validation failed ({prov_report.invalid_chunks} invalid chunks)",
                        details={"failures": [f.model_dump(mode="json") for f in prov_report.failures[:5]]},
                    )
                state_machine.complete_stage(PipelineStage.VALIDATION, items_processed=len(chunks))

                # -------------------------------------------------------------
                # Stage 6: MinHash Near-Duplicate Chunk Detection
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.DEDUPLICATION, message="Computing MinHash chunk signatures")
                chunks = self.chunk_deduplicator.annotate_chunks(chunks, context=context)
                state_machine.complete_stage(PipelineStage.DEDUPLICATION, items_processed=len(chunks))

                # -------------------------------------------------------------
                # Stage 7: Dense Embedding Generation (BGE-M3)
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.EMBEDDING)
                chunk_texts = [c.content for c in chunks]
                vectors = await self.embedding_service.embed_texts(chunk_texts)
                state_machine.complete_stage(PipelineStage.EMBEDDING, items_processed=len(vectors))

                # Prepare staged point IDs for compensation tracking
                staged_point_ids = [to_point_id(c.chunk_id) for c in chunks]

                # -------------------------------------------------------------
                # Stage 8: Relational Persistence + Transactional Outbox
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.PERSISTENCE)
                # Build outbox event payload
                points_payload = [
                    {
                        "chunk": c.model_dump(mode="json"),
                        "vector": v,
                    }
                    for c, v in zip(chunks, vectors)
                ]
                outbox_event = OutboxEvent(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    event_type="UPSERT_VECTOR",
                    status="PENDING",
                    payload={"points": points_payload},
                )

                # Persist chunks and outbox event in the SAME transaction
                saved_chunks_count, saved_outbox = await self.chunk_repo.save_chunks_and_outbox(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    chunks=chunks,
                    outbox_event=outbox_event,
                )
                state_machine.complete_stage(PipelineStage.PERSISTENCE, items_processed=saved_chunks_count)

                # -------------------------------------------------------------
                # Stage 9: Qdrant Indexing via Outbox Dispatcher
                # -------------------------------------------------------------
                state_machine.enter_stage(PipelineStage.INDEXING)
                await self.outbox_dispatcher.dispatch_event(saved_outbox, context=context)
                state_machine.complete_stage(PipelineStage.INDEXING, items_processed=len(chunks))

                # -------------------------------------------------------------
                # Stage 10: Complete Pipeline & Update Relational Status
                # -------------------------------------------------------------
                state_machine.complete_pipeline("Document ingestion and cross-storage indexing completed successfully")

                duration = time.perf_counter() - start_time
                return IngestionPipelineResult(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    status=DocumentIngestionStatus.COMPLETED,
                    final_stage=PipelineStage.INDEXING,
                    chunks_created=len(chunks),
                    points_indexed=len(chunks),
                    duration_seconds=round(duration, 4),
                    metadata=extraction_result.metadata,
                    warnings=state_machine.warnings,
                )

            except Exception as e:
                logger.error(
                    "[doc=%s tenant=%s] Ingestion pipeline failed: %s",
                    document_id,
                    tenant_id,
                    e,
                    exc_info=True,
                )
                current_stg = state_machine.current_stage or PipelineStage.VALIDATION
                state_machine.fail_stage(current_stg, e, mark_document_failed=True)

                # Execute Saga Compensation: purge any partially indexed vectors
                if staged_point_ids:
                    try:
                        await self.compensation_manager.compensate_indexing_failure(
                            tenant_id=tenant_id,
                            document_id=document_id,
                            point_ids=staged_point_ids,
                            error_message=str(e),
                            context=context,
                        )
                    except Exception as comp_err:
                        logger.error("Error executing compensation: %s", comp_err)

                duration = time.perf_counter() - start_time
                return IngestionPipelineResult(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    status=DocumentIngestionStatus.FAILED,
                    final_stage=current_stg,
                    chunks_created=0,
                    points_indexed=0,
                    duration_seconds=round(duration, 4),
                    warnings=state_machine.warnings,
                    error_message=str(e),
                )

    def _resolve_bytes(self, content: bytes | BinaryIO | Path) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, Path):
            return content.read_bytes()
        if hasattr(content, "read"):
            data = content.read()
            if isinstance(data, str):
                return data.encode("utf-8")
            return data
        raise ValidationError(f"Unsupported content type: {type(content)}")
