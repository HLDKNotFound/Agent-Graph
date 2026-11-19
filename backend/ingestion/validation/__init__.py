"""Validation subsystem contracts, validators, and sanitization utilities."""

from backend.ingestion.validation.interfaces import (
    DocumentValidator,
    ValidationResult,
)
from backend.ingestion.validation.sanitizer import (
    sanitize_filename,
    validate_safe_path,
)
from backend.ingestion.validation.signatures import (
    detect_file_format,
    validate_csv_integrity,
    validate_docx_integrity,
    validate_pdf_integrity,
    validate_xlsx_integrity,
)
from backend.ingestion.validation.provenance import (
    ChunkProvenanceValidator,
    ChunkValidationOutcome,
    DocumentBoundaryContext,
    FieldRequirement,
    LocatorValidationErrorDetail,
    ProvenanceErrorCode,
    ProvenanceReport,
    SourceLocationReconstruction,
    default_provenance_validator,
    reconstruct_chunk_source_location,
    validate_chunk_provenance,
    validate_chunks_provenance,
)
from backend.ingestion.validation.validator import DefaultDocumentValidator

__all__ = [
    "DocumentValidator",
    "ValidationResult",
    "DefaultDocumentValidator",
    "sanitize_filename",
    "validate_safe_path",
    "detect_file_format",
    "validate_pdf_integrity",
    "validate_docx_integrity",
    "validate_xlsx_integrity",
    "validate_csv_integrity",
    # Provenance and Locator Validation
    "ChunkProvenanceValidator",
    "ChunkValidationOutcome",
    "DocumentBoundaryContext",
    "FieldRequirement",
    "LocatorValidationErrorDetail",
    "ProvenanceErrorCode",
    "ProvenanceReport",
    "SourceLocationReconstruction",
    "default_provenance_validator",
    "validate_chunk_provenance",
    "validate_chunks_provenance",
    "reconstruct_chunk_source_location",
]
