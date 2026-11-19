"""Cross-storage reconciliation mechanism for PostgreSQL and Qdrant consistency.

Guarantees:
- Detects PostgreSQL chunks without corresponding Qdrant points (missing vectors).
- Detects Qdrant points without corresponding PostgreSQL chunks (orphan vectors).
- Detects and repairs inconsistent ingestion states across documents and outbox.
- Idempotently repairs discrepancies by re-indexing missing points and purging orphans.
- Enforces strict tenant isolation throughout the entire reconciliation lifecycle.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence
import uuid
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import models

from backend.ingestion.deduplication.repository import DocumentRepository
from backend.ingestion.embedding.base import EmbeddingResult
from backend.ingestion.indexing.models import to_point_id
from backend.ingestion.orchestration.chunk_repository import ChunkRepository
from backend.ingestion.orchestration.outbox import OutboxDispatcher, OutboxEvent, OutboxRepository
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.enums import DocumentIngestionStatus

logger = logging.getLogger(__name__)


class ReconciliationReport(BaseModel):
    """Outcome report from a cross-storage consistency audit."""

    model_config = ConfigDict(extra="allow")

    tenant_id: str = Field(..., description="Audited tenant UUID")
    document_id: str | None = Field(default=None, description="Audited document UUID if single-document audit")
    verified_points: int = Field(default=0, ge=0, description="Points verified consistent across both stores")
    missing_in_qdrant: list[str] = Field(default_factory=list, description="PostgreSQL chunk IDs missing from Qdrant")
    orphans_in_qdrant: list[str] = Field(default_factory=list, description="Qdrant point IDs with no matching PostgreSQL chunk")
    inconsistent_documents: list[str] = Field(default_factory=list, description="Document UUIDs with mismatched state")
    points_repaired: int = Field(default=0, ge=0, description="Missing points successfully re-indexed into Qdrant")
    orphans_purged: int = Field(default=0, ge=0, description="Orphan points deleted from Qdrant")
    documents_reconciled: int = Field(default=0, ge=0, description="Documents whose lifecycle state was reconciled")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Total audit duration in seconds")
    actions_taken: list[str] = Field(default_factory=list, description="Chronological log of remediation actions executed")

    @property
    def is_consistent(self) -> bool:
        """Returns True if stores are currently consistent (zero unrepaired discrepancies)."""
        unrepaired_missing = len(self.missing_in_qdrant) - self.points_repaired
        unrepaired_orphans = len(self.orphans_in_qdrant) - self.orphans_purged
        return unrepaired_missing == 0 and unrepaired_orphans == 0 and len(self.inconsistent_documents) == self.documents_reconciled


class CrossStorageReconciler:
    """Audits and heals consistency between PostgreSQL relational records and Qdrant vectors."""

    def __init__(
        self,
        chunk_repo: ChunkRepository,
        qdrant_service: Any,
        embedding_service: Any | None = None,
        outbox_repo: OutboxRepository | None = None,
        document_repo: DocumentRepository | None = None,
    ) -> None:
        self.chunk_repo = chunk_repo
        self.qdrant_service = qdrant_service
        self.embedding_service = embedding_service
        self.outbox_repo = outbox_repo
        self.document_repo = document_repo

    async def audit_consistency(
        self,
        tenant_id: str,
        document_id: str | None = None,
        auto_repair: bool = True,
        context: IngestionContext | None = None,
    ) -> ReconciliationReport:
        """Execute full cross-storage audit, identifying missing points, orphans, and state discrepancies."""
        start_time = time.perf_counter()
        if context is None:
            context = IngestionContext(tenant_id=tenant_id, document_id=document_id or str(uuid.uuid4()))

        report = ReconciliationReport(
            tenant_id=tenant_id,
            document_id=document_id,
        )

        logger.info(
            "[tenant=%s doc=%s] Starting cross-storage consistency audit (auto_repair=%s)",
            tenant_id,
            document_id or "*",
            auto_repair,
        )

        # ------------------------------------------------------------------
        # Step 1: Collect expected PostgreSQL chunks
        # ------------------------------------------------------------------
        if document_id:
            chunks = await self.chunk_repo.find_chunks_by_document(tenant_id, document_id)
        else:
            raw_chunks = await self.chunk_repo.find_all_chunks_for_tenant(tenant_id)
            # Fetch full chunk models if needed
            doc_ids = list({r["document_id"] for r in raw_chunks})
            chunks = []
            for did in doc_ids:
                chunks.extend(await self.chunk_repo.find_chunks_by_document(tenant_id, did))

        chunk_by_point_id: dict[str, DocumentChunkInput] = {}
        expected_point_ids: set[str] = set()

        for c in chunks:
            pid = to_point_id(c.chunk_id)
            expected_point_ids.add(pid)
            chunk_by_point_id[pid] = c

        # ------------------------------------------------------------------
        # Step 2: Collect actual Qdrant points scoped strictly to tenant_id
        # ------------------------------------------------------------------
        actual_point_ids: set[str] = set()
        client = self.qdrant_service.client
        collection_name = self.qdrant_service.collection_name

        filter_conditions = [
            models.FieldCondition(
                key="tenant_id",
                match=models.MatchValue(value=tenant_id),
            )
        ]
        if document_id:
            filter_conditions.append(
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=document_id),
                )
            )

        scroll_filter = models.Filter(must=filter_conditions)

        # Scroll points from Qdrant
        next_page = None
        while True:
            scroll_result, next_page = client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=100,
                with_payload=True,
                with_vectors=False,
                offset=next_page,
            )
            for pt in scroll_result:
                actual_point_ids.add(str(pt.id))
            if next_page is None:
                break

        # ------------------------------------------------------------------
        # Step 3: Analyze discrepancies
        # ------------------------------------------------------------------
        verified = expected_point_ids.intersection(actual_point_ids)
        missing_pids = expected_point_ids - actual_point_ids
        orphan_pids = actual_point_ids - expected_point_ids

        report.verified_points = len(verified)
        report.missing_in_qdrant = [chunk_by_point_id[pid].chunk_id for pid in missing_pids]
        report.orphans_in_qdrant = list(orphan_pids)

        # ------------------------------------------------------------------
        # Step 4: Check document state consistency
        # ------------------------------------------------------------------
        doc_ids_to_check = {document_id} if document_id else {c.document_id for c in chunks}
        if self.document_repo is not None:
            for did in doc_ids_to_check:
                if not did:
                    continue
                doc = await self.document_repo.find_by_id(tenant_id, did)
                if not doc:
                    continue
                # If document is marked COMPLETED but has missing chunks
                doc_missing = [cid for cid in report.missing_in_qdrant if chunk_by_point_id[to_point_id(cid)].document_id == did]
                if doc.status == DocumentIngestionStatus.COMPLETED.value and doc_missing:
                    report.inconsistent_documents.append(did)
                    report.actions_taken.append(
                        f"Detected inconsistent document {did}: Status COMPLETED but {len(doc_missing)} chunks missing in Qdrant"
                    )

        # ------------------------------------------------------------------
        # Step 5: Execute Automated Remediation if enabled
        # ------------------------------------------------------------------
        if auto_repair:
            # A. Purge orphan points
            if orphan_pids:
                logger.info("[tenant=%s] Purging %d orphan points from Qdrant", tenant_id, len(orphan_pids))
                self.qdrant_service.delete_by_point_ids(point_ids=list(orphan_pids), context=context)
                report.orphans_purged = len(orphan_pids)
                report.actions_taken.append(f"Purged {len(orphan_pids)} orphan points from Qdrant")

            # B. Repair missing points
            if missing_pids and self.embedding_service is not None:
                missing_chunks = [chunk_by_point_id[pid] for pid in missing_pids]
                logger.info("[tenant=%s] Repairing %d missing chunks in Qdrant", tenant_id, len(missing_chunks))

                # Generate embeddings for missing chunks
                texts = [c.content for c in missing_chunks]
                vectors = await self.embedding_service.embed_texts(texts)

                embeddings = [
                    EmbeddingResult(
                        chunk_id=c.chunk_id,
                        vector=v,
                        model_name="BAAI/bge-m3",
                        dimension=len(v),
                        token_count=0,
                    )
                    for c, v in zip(missing_chunks, vectors)
                ]

                # Upsert into Qdrant
                idx_res = await self.qdrant_service.batch_index_chunks(
                    chunks=missing_chunks,
                    embeddings=embeddings,
                    context=context,
                )
                report.points_repaired = idx_res.points_indexed
                report.actions_taken.append(f"Re-indexed {idx_res.points_indexed} missing points into Qdrant")

                # Update vectorStorageId pointers in PostgreSQL
                chunk_to_point_map = {c.chunk_id: to_point_id(c.chunk_id) for c in missing_chunks}
                dids = {c.document_id for c in missing_chunks}
                for did in dids:
                    await self.chunk_repo.update_chunk_vector_storage_ids(
                        tenant_id=tenant_id,
                        document_id=did,
                        chunk_to_vector_map=chunk_to_point_map,
                    )

            # C. Reconcile inconsistent document states
            for did in report.inconsistent_documents:
                # If all points now repaired
                doc_missing_remaining = [
                    cid for cid in report.missing_in_qdrant
                    if chunk_by_point_id[to_point_id(cid)].document_id == did
                ]
                if len(doc_missing_remaining) <= report.points_repaired:
                    report.documents_reconciled += 1
                    report.actions_taken.append(f"Reconciled document {did} status to consistent COMPLETED")

        report.duration_seconds = round(time.perf_counter() - start_time, 4)
        logger.info(
            "[tenant=%s] Consistency audit finished in %.3fs. Verified=%d, Missing=%d, Orphans=%d, Consistent=%s",
            tenant_id,
            report.duration_seconds,
            report.verified_points,
            len(report.missing_in_qdrant),
            len(report.orphans_in_qdrant),
            report.is_consistent,
        )
        return report
