"""Ingestion orchestration and cross-storage consistency subsystem."""

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
from backend.ingestion.orchestration.orchestrator import DefaultIngestionOrchestrator
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
from backend.ingestion.orchestration.state_machine import (
    IngestionStateMachine,
    InvalidStateTransitionError,
    StageExecutionRecord,
    StageStatus,
)

__all__ = [
    # Core Orchestration
    "IngestionOrchestrator",
    "DefaultIngestionOrchestrator",
    "IngestionPipelineResult",
    # State Machine
    "IngestionStateMachine",
    "StageExecutionRecord",
    "StageStatus",
    "InvalidStateTransitionError",
    # Transactional Outbox
    "OutboxEvent",
    "OutboxRepository",
    "PostgresOutboxRepository",
    "InMemoryOutboxRepository",
    "OutboxDispatcher",
    # Relational Chunk Persistence
    "ChunkRepository",
    "PostgresChunkRepository",
    "InMemoryChunkRepository",
    # Saga Compensation
    "CompensationManager",
    # Cross-Storage Reconciliation
    "CrossStorageReconciler",
    "ReconciliationReport",
]
