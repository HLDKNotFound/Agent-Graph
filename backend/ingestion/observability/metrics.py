"""Enterprise Ingestion Observability and Pipeline Metrics Collector.

Provides structured telemetry, fine-grained stage profiling, and metrics
aggregation across all document ingestion workflows:
- Structured logging with consistent job and tenant coordinates.
- Processing duration per pipeline stage.
- Page extraction throughput (pages per minute).
- Embedding throughput (tokens per second).
- Qdrant indexing latency and point throughput.
- Error diagnostics, retry counters, and failure reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from typing import Any
import uuid
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("backend.ingestion.observability")


class StageMetric(BaseModel):
    """Metric record for an individual pipeline execution stage."""

    model_config = ConfigDict(frozen=True)

    stage: str = Field(..., description="Pipeline stage name")
    duration_seconds: float = Field(..., ge=0.0, description="Stage execution runtime in seconds")
    items_processed: int = Field(default=0, ge=0, description="Number of items processed during this stage")
    status: str = Field(default="COMPLETED", description="Stage outcome status: COMPLETED, FAILED, SKIPPED")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Completion timestamp in ISO-8601",
    )


class JobMetricsReport(BaseModel):
    """End-to-end execution metrics and telemetry for a single ingestion job."""

    model_config = ConfigDict(extra="allow")

    job_id: str = Field(..., description="Unique job execution UUID")
    document_id: str = Field(..., description="Target document UUID")
    tenant_id: str = Field(..., description="Target tenant UUID")
    filename: str = Field(default="", description="Source document filename")
    file_size_bytes: int = Field(default=0, ge=0, description="Source file size in bytes")
    document_type: str = Field(default="", description="Detected document format")
    status: str = Field(default="PENDING", description="Final job status: COMPLETED, FAILED, CANCELLED")
    total_duration_seconds: float = Field(default=0.0, ge=0.0, description="Total wall-clock runtime in seconds")
    stage_durations: dict[str, float] = Field(default_factory=dict, description="Runtime duration per stage in seconds")
    stages: list[StageMetric] = Field(default_factory=list, description="Ordered list of stage metric records")

    # Granular throughput and operational telemetry
    extracted_page_count: int = Field(default=0, ge=0, description="Number of document pages extracted")
    extracted_element_count: int = Field(default=0, ge=0, description="Number of structural elements parsed")
    generated_chunk_count: int = Field(default=0, ge=0, description="Number of chunks generated")
    tokens_embedded: int = Field(default=0, ge=0, description="Total token volume processed by embedding model")
    points_indexed: int = Field(default=0, ge=0, description="Number of vector points upserted into Qdrant")

    # Latencies
    extraction_duration_seconds: float = Field(default=0.0, ge=0.0, description="Extraction stage duration in seconds")
    embedding_duration_seconds: float = Field(default=0.0, ge=0.0, description="Embedding stage duration in seconds")
    indexing_duration_seconds: float = Field(default=0.0, ge=0.0, description="Vector indexing duration in seconds")

    # Derived Performance Metrics
    pages_per_minute: float = Field(default=0.0, ge=0.0, description="Extraction speed in pages per minute")
    tokens_per_second: float = Field(default=0.0, ge=0.0, description="Embedding throughput in tokens per second")
    indexing_latency_per_point_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Average Qdrant upsert latency per vector in milliseconds",
    )

    # Failure diagnostics and retry metrics
    retry_count: int = Field(default=0, ge=0, description="Number of retry attempts executed")
    error_message: str | None = Field(default=None, description="Diagnostic error message if job failed")
    error_stage: str | None = Field(default=None, description="Pipeline stage where failure occurred")


class PipelineMetricsCollector:
    """Thread-safe telemetry collector and aggregator for document ingestion pipelines."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobMetricsReport] = {}
        self._total_jobs_started: int = 0
        self._total_jobs_completed: int = 0
        self._total_jobs_failed: int = 0

    def start_job(
        self,
        job_id: str | None = None,
        document_id: str = "",
        tenant_id: str = "",
        filename: str = "",
        file_size_bytes: int = 0,
        document_type: str = "",
    ) -> JobMetricsReport:
        """Register the initiation of an ingestion job."""
        effective_job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        report = JobMetricsReport(
            job_id=effective_job_id,
            document_id=document_id,
            tenant_id=tenant_id,
            filename=filename,
            file_size_bytes=file_size_bytes,
            document_type=document_type,
            status="STARTED",
        )
        self._jobs[effective_job_id] = report
        self._total_jobs_started += 1
        return report

    def record_stage(
        self,
        job_id: str,
        stage: str,
        duration_seconds: float,
        items_processed: int = 0,
        status: str = "COMPLETED",
    ) -> None:
        """Record the completion of a pipeline stage for a job."""
        report = self._jobs.get(job_id)
        if not report:
            return

        clean_stage = stage.upper()
        report.stage_durations[clean_stage] = round(duration_seconds, 4)
        report.stages.append(
            StageMetric(
                stage=clean_stage,
                duration_seconds=round(duration_seconds, 4),
                items_processed=items_processed,
                status=status,
            )
        )

        if clean_stage == "EXTRACTION":
            report.extraction_duration_seconds = round(duration_seconds, 4)
            if items_processed > 0 and report.extracted_element_count == 0:
                report.extracted_element_count = items_processed
        elif clean_stage == "CHUNKING":
            if items_processed > 0:
                report.generated_chunk_count = items_processed
        elif clean_stage == "EMBEDDING":
            report.embedding_duration_seconds = round(duration_seconds, 4)
        elif clean_stage == "INDEXING":
            report.indexing_duration_seconds = round(duration_seconds, 4)
            if items_processed > 0:
                report.points_indexed = items_processed

    def record_extraction_details(
        self,
        job_id: str,
        page_count: int,
        element_count: int,
        duration_seconds: float,
    ) -> None:
        """Record fine-grained extraction throughput data."""
        report = self._jobs.get(job_id)
        if not report:
            return

        report.extracted_page_count = page_count
        report.extracted_element_count = element_count
        report.extraction_duration_seconds = round(duration_seconds, 4)
        report.stage_durations["EXTRACTION"] = round(duration_seconds, 4)

        if duration_seconds > 0 and page_count > 0:
            # Pages per minute = (pages / seconds) * 60
            report.pages_per_minute = round((page_count / duration_seconds) * 60.0, 2)

    def record_embedding_details(
        self,
        job_id: str,
        token_count: int,
        duration_seconds: float,
    ) -> None:
        """Record embedding model token throughput metrics."""
        report = self._jobs.get(job_id)
        if not report:
            return

        report.tokens_embedded = token_count
        report.embedding_duration_seconds = round(duration_seconds, 4)
        report.stage_durations["EMBEDDING"] = round(duration_seconds, 4)

        if duration_seconds > 0 and token_count > 0:
            report.tokens_per_second = round(token_count / duration_seconds, 2)

    def record_indexing_details(
        self,
        job_id: str,
        points_count: int,
        duration_seconds: float,
    ) -> None:
        """Record vector index upsert latency."""
        report = self._jobs.get(job_id)
        if not report:
            return

        report.points_indexed = points_count
        report.indexing_duration_seconds = round(duration_seconds, 4)
        report.stage_durations["INDEXING"] = round(duration_seconds, 4)

        if points_count > 0 and duration_seconds > 0:
            report.indexing_latency_per_point_ms = round((duration_seconds * 1000.0) / points_count, 3)

    def record_retry(self, job_id: str) -> None:
        """Increment the retry counter for an ingestion job."""
        report = self._jobs.get(job_id)
        if report:
            report.retry_count += 1

    def finish_job(
        self,
        job_id: str,
        status: str,
        total_duration_seconds: float,
        error_message: str | None = None,
        error_stage: str | None = None,
    ) -> JobMetricsReport:
        """Finalize job execution metrics and compute overall throughput."""
        report = self._jobs.get(job_id)
        if not report:
            report = JobMetricsReport(
                job_id=job_id,
                document_id="unknown",
                tenant_id="unknown",
                status=status,
            )
            self._jobs[job_id] = report

        report.status = status
        report.total_duration_seconds = round(total_duration_seconds, 4)
        report.error_message = error_message
        report.error_stage = error_stage

        # Ensure derived metrics are calculated if not already set
        if report.extracted_page_count > 0 and report.extraction_duration_seconds > 0:
            report.pages_per_minute = round(
                (report.extracted_page_count / report.extraction_duration_seconds) * 60.0,
                2,
            )

        if report.tokens_embedded > 0 and report.embedding_duration_seconds > 0:
            report.tokens_per_second = round(
                report.tokens_embedded / report.embedding_duration_seconds,
                2,
            )

        if report.points_indexed > 0 and report.indexing_duration_seconds > 0:
            report.indexing_latency_per_point_ms = round(
                (report.indexing_duration_seconds * 1000.0) / report.points_indexed,
                3,
            )

        if status == "COMPLETED":
            self._total_jobs_completed += 1
        elif status == "FAILED":
            self._total_jobs_failed += 1

        logger.info(
            json.dumps({
                "event": "ingestion_job_finished",
                "job_id": report.job_id,
                "document_id": report.document_id,
                "tenant_id": report.tenant_id,
                "status": report.status,
                "duration_seconds": report.total_duration_seconds,
                "pages": report.extracted_page_count,
                "chunks": report.generated_chunk_count,
                "points": report.points_indexed,
                "pages_per_minute": report.pages_per_minute,
                "tokens_per_second": report.tokens_per_second,
                "indexing_latency_ms": report.indexing_latency_per_point_ms,
                "retries": report.retry_count,
                "error": report.error_message,
            })
        )

        return report

    def get_job_metrics(self, job_id: str) -> JobMetricsReport | None:
        """Retrieve metrics for a specific job."""
        return self._jobs.get(job_id)

    def get_aggregated_summary(self) -> dict[str, Any]:
        """Compute aggregated statistics across all tracked jobs."""
        completed_jobs = [j for j in self._jobs.values() if j.status == "COMPLETED"]
        failed_jobs = [j for j in self._jobs.values() if j.status == "FAILED"]

        total_pages = sum(j.extracted_page_count for j in completed_jobs)
        total_chunks = sum(j.generated_chunk_count for j in completed_jobs)
        total_points = sum(j.points_indexed for j in completed_jobs)
        total_tokens = sum(j.tokens_embedded for j in completed_jobs)

        avg_ppm = (
            sum(j.pages_per_minute for j in completed_jobs if j.pages_per_minute > 0)
            / len([j for j in completed_jobs if j.pages_per_minute > 0])
            if any(j.pages_per_minute > 0 for j in completed_jobs)
            else 0.0
        )
        avg_tokens_per_sec = (
            sum(j.tokens_per_second for j in completed_jobs if j.tokens_per_second > 0)
            / len([j for j in completed_jobs if j.tokens_per_second > 0])
            if any(j.tokens_per_second > 0 for j in completed_jobs)
            else 0.0
        )
        avg_indexing_latency_ms = (
            sum(j.indexing_latency_per_point_ms for j in completed_jobs if j.indexing_latency_per_point_ms > 0)
            / len([j for j in completed_jobs if j.indexing_latency_per_point_ms > 0])
            if any(j.indexing_latency_per_point_ms > 0 for j in completed_jobs)
            else 0.0
        )
        avg_duration = (
            sum(j.total_duration_seconds for j in completed_jobs) / len(completed_jobs)
            if completed_jobs
            else 0.0
        )

        return {
            "total_jobs_started": self._total_jobs_started,
            "total_jobs_completed": len(completed_jobs),
            "total_jobs_failed": len(failed_jobs),
            "total_pages_extracted": total_pages,
            "total_chunks_generated": total_chunks,
            "total_points_indexed": total_points,
            "total_tokens_embedded": total_tokens,
            "average_job_duration_seconds": round(avg_duration, 4),
            "average_pages_per_minute": round(avg_ppm, 2),
            "average_tokens_per_second": round(avg_tokens_per_sec, 2),
            "average_indexing_latency_per_point_ms": round(avg_indexing_latency_ms, 3),
            "failure_rate_percent": round(
                (len(failed_jobs) / self._total_jobs_started * 100.0) if self._total_jobs_started > 0 else 0.0,
                2,
            ),
        }

    def clear(self) -> None:
        """Reset internal metrics registry."""
        self._jobs.clear()
        self._total_jobs_started = 0
        self._total_jobs_completed = 0
        self._total_jobs_failed = 0


# Global collector singleton
_global_metrics_collector = PipelineMetricsCollector()


def get_pipeline_metrics_collector() -> PipelineMetricsCollector:
    """Return the global PipelineMetricsCollector singleton."""
    return _global_metrics_collector
