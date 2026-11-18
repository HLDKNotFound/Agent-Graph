"""Document deduplication service implementing exact SHA-256 deduplication and race-safe policies."""

import logging
from pathlib import Path
from typing import Any, BinaryIO

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.deduplication.base import (
    DeduplicationResult,
    DeduplicationStrategy,
)
from backend.ingestion.deduplication.hasher import (
    StreamHasher,
    compute_content_sha256,
)
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
from backend.ingestion.exceptions import (
    DuplicateDocumentError,
    EmptyFileError,
    ValidationError,
)
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext

logger = logging.getLogger(__name__)


class DocumentDeduplicator:
    """Core deduplication engine executing pre-extraction streaming hash verification.

    Enforces:
    1. Zero-trust tenant isolation (content hash checked strictly within context.tenant_id).
    2. Streaming I/O chunking (never loads whole file into memory to compute hash).
    3. Soft-delete awareness (documents where deleted_at IS NOT NULL do not block ingestion).
    4. Deterministic policy handling (RETURN_EXISTING, REJECT, or NEW_VERSION).
    5. Race-safety against concurrent uploads of identical files.
    """

    def __init__(
        self,
        repository: DocumentRepository | None = None,
        hasher: StreamHasher | None = None,
        settings: IngestionSettings | None = None,
    ) -> None:
        self.settings = settings or get_ingestion_settings()
        self.hasher = hasher or StreamHasher(chunk_size=self.settings.deduplication_chunk_size_bytes)
        self.repository = repository or PostgresDocumentRepository(connection_url=self.settings.database_url)

    def compute_hash(
        self,
        content: bytes | BinaryIO | Path | str,
        rewind_after: bool = True,
        reject_empty: bool = True,
    ) -> tuple[str, int]:
        """Compute SHA-256 hash and byte length via streaming reads without parsing."""
        return self.hasher.hash_content(
            content=content,
            rewind_after=rewind_after,
            reject_empty=reject_empty,
        )

    async def check_hash(
        self,
        content_hash: str,
        context: IngestionContext,
        policy: DuplicateHandlingPolicy | str | None = None,
        file_size_bytes: int | None = None,
        raise_on_duplicate: bool = True,
    ) -> DeduplicationResult:
        """Check whether an active document with content_hash exists under context.tenant_id.

        Args:
            content_hash: SHA-256 hexadecimal hash string.
            context: IngestionContext providing tenant_id and execution options.
            policy: DuplicateHandlingPolicy override (defaults to context option or settings).
            file_size_bytes: Optional pre-calculated file size.
            raise_on_duplicate: If True and policy is REJECT, raises DuplicateDocumentError.

        Returns:
            DeduplicationResult detailing match status and action taken.
        """
        if not content_hash or len(content_hash) != 64:
            raise ValidationError(f"Invalid SHA-256 hash length ({len(content_hash) if content_hash else 0})")

        tenant_id = context.tenant_id
        if not tenant_id:
            raise ValidationError("Tenant ID is required for deduplication check")

        # Determine effective duplicate policy
        effective_policy_str = (
            policy
            or getattr(context.options, "duplicate_handling_policy", None)
            or self.settings.deduplication_default_policy
        )
        if isinstance(effective_policy_str, DuplicateHandlingPolicy):
            effective_policy = effective_policy_str
        else:
            try:
                effective_policy = DuplicateHandlingPolicy(str(effective_policy_str).upper())
            except ValueError:
                effective_policy = DuplicateHandlingPolicy.RETURN_EXISTING

        # Check repository for active duplicate
        existing = await self.repository.find_active_by_hash(
            tenant_id=tenant_id,
            content_hash=content_hash,
        )

        if not existing:
            # No active duplicate exists in this tenant
            return DeduplicationResult(
                is_duplicate=False,
                action=DuplicateAction.PROCEED,
                similarity_score=0.0,
                content_hash=content_hash,
                file_size_bytes=file_size_bytes,
                existing_document_id=None,
                existing_document=None,
                message="No duplicate active document detected; proceed to extraction.",
            )

        # Duplicate detected! Apply policy
        logger.info(
            "Exact document duplicate detected in tenant '%s': existing document ID '%s', hash '%s'",
            tenant_id,
            existing.document_id,
            content_hash,
        )

        if effective_policy == DuplicateHandlingPolicy.REJECT:
            if raise_on_duplicate:
                raise DuplicateDocumentError(
                    f"Duplicate document detected in tenant '{tenant_id}': identical active document "
                    f"'{existing.original_filename}' (ID: {existing.document_id}) already exists.",
                    tenant_id=tenant_id,
                    content_hash=content_hash,
                    existing_document_id=existing.document_id,
                    details={
                        "existing_filename": existing.original_filename,
                        "existing_document_id": existing.document_id,
                        "content_hash": content_hash,
                    },
                )
            return DeduplicationResult(
                is_duplicate=True,
                action=DuplicateAction.REJECTED,
                similarity_score=1.0,
                content_hash=content_hash,
                file_size_bytes=file_size_bytes,
                existing_document_id=existing.document_id,
                existing_document=existing,
                message=f"Duplicate document rejected by policy (existing ID: {existing.document_id}).",
            )

        if effective_policy == DuplicateHandlingPolicy.NEW_VERSION:
            # Create incremental version snapshot
            version_input = DocumentVersionCreateInput(
                document_id=existing.document_id,
                tenant_id=tenant_id,
                created_by_id=context.user_id,
                original_filename=existing.original_filename,
                storage_uri=existing.storage_uri or "",
                mime_type=existing.mime_type or "application/octet-stream",
                file_size_bytes=existing.file_size_bytes,
                content_hash=content_hash,
                status=existing.status,
                metadata={"created_via": "deduplication_new_version_policy"},
                is_current=True,
            )
            updated_summary = await self.repository.create_version(version_input)
            return DeduplicationResult(
                is_duplicate=True,
                action=DuplicateAction.NEW_VERSION_CREATED,
                similarity_score=1.0,
                content_hash=content_hash,
                file_size_bytes=file_size_bytes,
                existing_document_id=existing.document_id,
                existing_document=updated_summary,
                message=(
                    f"Created new version snapshot {updated_summary.version_number} "
                    f"for document {existing.document_id}."
                ),
            )

        # Default: RETURN_EXISTING
        return DeduplicationResult(
            is_duplicate=True,
            action=DuplicateAction.RETURN_EXISTING,
            similarity_score=1.0,
            content_hash=content_hash,
            file_size_bytes=file_size_bytes,
            existing_document_id=existing.document_id,
            existing_document=existing,
            message=(
                f"Existing active document '{existing.original_filename}' (ID: {existing.document_id}) "
                f"reused; extraction bypassed."
            ),
        )

    async def deduplicate_content(
        self,
        content: bytes | BinaryIO | Path | str,
        filename: str,
        context: IngestionContext,
        policy: DuplicateHandlingPolicy | str | None = None,
        rewind_after: bool = True,
        raise_on_duplicate: bool = True,
    ) -> DeduplicationResult:
        """Calculate streaming SHA-256 from content and check duplicate status in tenant.

        Executes completely before extraction to short-circuit redundant pipeline stages.
        """
        content_hash, file_size_bytes = self.compute_hash(
            content=content,
            rewind_after=rewind_after,
            reject_empty=True,
        )

        return await self.check_hash(
            content_hash=content_hash,
            context=context,
            policy=policy,
            file_size_bytes=file_size_bytes,
            raise_on_duplicate=raise_on_duplicate,
        )

    async def register_document_safely(
        self,
        doc_input: DocumentCreateInput,
    ) -> tuple[ExistingDocumentSummary, bool]:
        """Atomically register a document in the database with race condition recovery.

        Returns:
            Tuple of (summary: ExistingDocumentSummary, is_new: bool).
            If a concurrent upload inserted the same hash first, is_new will be False.
        """
        # Pre-check
        existing = await self.repository.find_active_by_hash(doc_input.tenant_id, doc_input.content_hash)
        if existing:
            return existing, False

        # Attempt atomic registration (Postgres unique constraint handles concurrent race)
        summary = await self.repository.register_document(doc_input)
        is_new = summary.document_id == (doc_input.document_id or summary.document_id)
        return summary, is_new


class Sha256ExactDeduplicationStrategy(DeduplicationStrategy):
    """Production implementation of DeduplicationStrategy conforming to the subsystem interface."""

    def __init__(
        self,
        deduplicator: DocumentDeduplicator | None = None,
        repository: DocumentRepository | None = None,
    ) -> None:
        self.deduplicator = deduplicator or DocumentDeduplicator(repository=repository)

    @property
    def name(self) -> str:
        return "Sha256ExactDeduplication"

    async def check_document_duplicate(
        self,
        file_hash_sha256: str,
        context: IngestionContext,
    ) -> DeduplicationResult:
        """Verify whether an identical document SHA-256 hash exists under the authenticated tenant."""
        return await self.deduplicator.check_hash(
            content_hash=file_hash_sha256,
            context=context,
            raise_on_duplicate=False,
        )

    async def filter_duplicate_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: IngestionContext,
    ) -> list[DocumentChunkInput]:
        """Near-duplicate chunk filtering contract (reserved for MinHash LSH task)."""
        # In Task 08, chunk filtering passes through until MinHash LSH is implemented
        return chunks

    async def deduplicate_content(
        self,
        content: bytes | BinaryIO | Path | str,
        filename: str,
        context: IngestionContext,
        policy: DuplicateHandlingPolicy | str | None = None,
        rewind_after: bool = True,
        raise_on_duplicate: bool = False,
    ) -> DeduplicationResult:
        """Calculate streaming SHA-256 from content and execute tenant deduplication check."""
        return await self.deduplicator.deduplicate_content(
            content=content,
            filename=filename,
            context=context,
            policy=policy,
            rewind_after=rewind_after,
            raise_on_duplicate=raise_on_duplicate,
        )
