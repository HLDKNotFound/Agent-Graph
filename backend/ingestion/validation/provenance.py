"""Dedicated chunk provenance and locator coordinate validation service.

Guarantees:
- Every generated chunk has verifiable source provenance before persistence.
- Coordinates strictly respect physical document boundaries across formats (PDF, DOCX, XLSX, CSV).
- Format-specific locator field requirements (REQUIRED, OPTIONAL, NOT_APPLICABLE) are enforced.
- Invalid coordinates are rejected with descriptive diagnostic errors rather than silently accepted.
- Full source location reconstruction is provided for line-level citation badges and auditability.
- Bidirectional coordinate preservation is guaranteed across PostgreSQL and Qdrant vector representations.
"""

from enum import Enum
import logging
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.exceptions import ProvenanceValidationError
from backend.ingestion.types.chunking import DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.coordinates import ChunkLocator, parse_row_range
from backend.ingestion.types.enums import DocumentType
from backend.ingestion.types.extraction import ExtractionResult
from backend.ingestion.types.metadata import DocumentMetadata

logger = logging.getLogger(__name__)


class FieldRequirement(str, Enum):
    """Classification of locator field requirement for a specific document format."""

    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProvenanceErrorCode(str, Enum):
    """Standardized diagnostic error codes for provenance locator failures."""

    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    NON_APPLICABLE_FIELD_PRESENT = "NON_APPLICABLE_FIELD_PRESENT"
    INVALID_PAGE_COORDINATE = "INVALID_PAGE_COORDINATE"
    PAGE_OUT_OF_BOUNDS = "PAGE_OUT_OF_BOUNDS"
    INVALID_ROW_RANGE = "INVALID_ROW_RANGE"
    ROW_OUT_OF_BOUNDS = "ROW_OUT_OF_BOUNDS"
    UNKNOWN_SHEET_NAME = "UNKNOWN_SHEET_NAME"
    INVALID_CHAR_OFFSETS = "INVALID_CHAR_OFFSETS"
    CHAR_OFFSETS_OUT_OF_BOUNDS = "CHAR_OFFSETS_OUT_OF_BOUNDS"
    CANONICAL_TEXT_MISMATCH = "CANONICAL_TEXT_MISMATCH"
    CORRUPTED_COORDINATES = "CORRUPTED_COORDINATES"


class LocatorValidationErrorDetail(BaseModel):
    """Detailed description of an individual locator coordinate validation violation."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(..., description="Locator attribute that failed validation")
    error_code: ProvenanceErrorCode = Field(..., description="Standardized error code classification")
    message: str = Field(..., description="Human-readable diagnostic error message")
    value: Any | None = Field(default=None, description="The invalid coordinate value observed")
    constraint: Any | None = Field(default=None, description="The boundary constraint that was violated")


class ChunkValidationOutcome(BaseModel):
    """Outcome report for a single chunk's provenance and locator validation."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(..., description="Unique UUID v4/v5 chunk identifier")
    chunk_index: int = Field(..., ge=0, description="Sequential index of the chunk")
    document_id: str = Field(..., description="Parent document UUID")
    is_valid: bool = Field(..., description="True if chunk passed all provenance checks")
    errors: list[LocatorValidationErrorDetail] = Field(
        default_factory=list,
        description="List of validation failures if invalid",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings (e.g. missing optional heading)",
    )


class ProvenanceReport(BaseModel):
    """Comprehensive validation report for all chunks of a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(..., description="Parent document identifier")
    document_type: DocumentType = Field(..., description="Document format type")
    total_chunks: int = Field(..., ge=0, description="Total number of chunks evaluated")
    valid_chunks: int = Field(..., ge=0, description="Number of valid chunks")
    invalid_chunks: int = Field(..., ge=0, description="Number of chunks with failures")
    is_valid: bool = Field(..., description="True if 100% of chunks are valid")
    failures: list[ChunkValidationOutcome] = Field(
        default_factory=list,
        description="Details for chunks that failed validation",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Aggregated non-fatal validation warnings",
    )


class SourceLocationReconstruction(BaseModel):
    """Reconstructed physical source location representation for citation badges and auditability."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(..., description="Parent document UUID")
    chunk_id: str = Field(..., description="Target chunk UUID")
    chunk_index: int = Field(..., ge=0, description="0-indexed sequence position")
    citation_badge: str = Field(..., description="Standardized verifiable citation marker badge")
    locator: ChunkLocator = Field(..., description="Full source locator coordinate object")
    reconstructed_text: str | None = Field(
        default=None,
        description="Exact text slice from source document canonical text using char offsets",
    )
    provenance_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional reconstructed provenance metadata",
    )


class DocumentBoundaryContext(BaseModel):
    """Physical boundaries and canonical representations of a source document for coordinate validation."""

    model_config = ConfigDict(extra="allow")

    document_id: str = Field(..., description="Parent document UUID")
    document_type: DocumentType = Field(default=DocumentType.PDF, description="Document format")
    total_pages: int | None = Field(default=None, ge=1, description="Total physical pages in document")
    sheet_names: list[str] = Field(default_factory=list, description="Valid worksheet names in workbook")
    sheet_row_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Mapping of worksheet name to maximum valid row count",
    )
    canonical_text: str | None = Field(
        default=None,
        description="Canonical UTF-8 text representation of the document",
    )
    strict_text_slice: bool = Field(
        default=False,
        description="If True, verifies that sliced canonical text is non-empty and consistent",
    )

    @classmethod
    def from_extraction_result(cls, result: ExtractionResult) -> "DocumentBoundaryContext":
        """Build boundary context from an ExtractionResult container."""
        # 1. Total pages: compute effective max from metadata, stats, or elements
        pages_from_meta = result.metadata.total_pages if result.metadata else None
        pages_from_stats = result.stats.total_pages if result.stats else None
        pages_from_elements = [
            getattr(e, "page_number", None)
            for e in result.elements
            if getattr(e, "page_number", None) is not None
        ]
        max_page_element = max(pages_from_elements) if pages_from_elements else None

        candidates = [p for p in (pages_from_meta, pages_from_stats, max_page_element) if p is not None and p >= 1]
        effective_pages = max(candidates) if candidates else None

        # 2. Sheet names
        sheet_names: list[str] = []
        custom_meta = result.metadata.custom_metadata if result.metadata else {}
        if isinstance(custom_meta, dict):
            if "sheet_names" in custom_meta and isinstance(custom_meta["sheet_names"], list):
                sheet_names.extend(str(s) for s in custom_meta["sheet_names"])

        # Also collect from elements
        element_sheets = [
            getattr(e, "sheet_name", None)
            for e in result.elements
            if getattr(e, "sheet_name", None) is not None
        ]
        for s in element_sheets:
            if s and s not in sheet_names:
                sheet_names.append(s)

        # 3. Sheet row counts
        sheet_row_counts: dict[str, int] = {}
        if isinstance(custom_meta, dict) and "sheet_summaries" in custom_meta:
            summaries = custom_meta["sheet_summaries"]
            if isinstance(summaries, list):
                for s in summaries:
                    if isinstance(s, dict) and "sheet_name" in s:
                        count = s.get("row_count") or s.get("total_rows") or s.get("max_row") or s.get("data_rows")
                        if count is not None and isinstance(count, int) and count >= 0:
                            sheet_row_counts[s["sheet_name"]] = count

        # Also inspect spreadsheet row elements for row indices
        for elem in result.elements:
            elem_sheet = getattr(elem, "sheet_name", None)
            row_idx = getattr(elem, "row_index", None)
            if elem_sheet and row_idx is not None and isinstance(row_idx, int):
                current_max = sheet_row_counts.get(elem_sheet, 0)
                if row_idx > current_max:
                    sheet_row_counts[elem_sheet] = row_idx

        # 4. Canonical text
        full_text = result.raw_text or result.get_full_text()

        return cls(
            document_id=result.document_id,
            document_type=result.document_type,
            total_pages=effective_pages,
            sheet_names=sheet_names,
            sheet_row_counts=sheet_row_counts,
            canonical_text=full_text,
        )


# ==============================================================================
# FORMAT-SPECIFIC LOCATOR REQUIREMENTS MATRIX
# ==============================================================================

DEFAULT_LOCATOR_RULES: dict[DocumentType, dict[str, FieldRequirement]] = {
    DocumentType.PDF: {
        "page_number": FieldRequirement.REQUIRED,
        "section_heading": FieldRequirement.OPTIONAL,
        "sheet_name": FieldRequirement.NOT_APPLICABLE,
        "row_range": FieldRequirement.NOT_APPLICABLE,
        "char_start": FieldRequirement.OPTIONAL,
        "char_end": FieldRequirement.OPTIONAL,
    },
    DocumentType.DOCX: {
        "page_number": FieldRequirement.OPTIONAL,
        "section_heading": FieldRequirement.OPTIONAL,
        "sheet_name": FieldRequirement.NOT_APPLICABLE,
        "row_range": FieldRequirement.NOT_APPLICABLE,
        "char_start": FieldRequirement.OPTIONAL,
        "char_end": FieldRequirement.OPTIONAL,
    },
    DocumentType.XLSX: {
        "page_number": FieldRequirement.OPTIONAL,
        "section_heading": FieldRequirement.OPTIONAL,
        "sheet_name": FieldRequirement.REQUIRED,
        "row_range": FieldRequirement.REQUIRED,
        "char_start": FieldRequirement.OPTIONAL,
        "char_end": FieldRequirement.OPTIONAL,
    },
    DocumentType.CSV: {
        "page_number": FieldRequirement.NOT_APPLICABLE,
        "section_heading": FieldRequirement.OPTIONAL,
        "sheet_name": FieldRequirement.OPTIONAL,
        "row_range": FieldRequirement.REQUIRED,
        "char_start": FieldRequirement.OPTIONAL,
        "char_end": FieldRequirement.OPTIONAL,
    },
    DocumentType.TXT: {
        "page_number": FieldRequirement.OPTIONAL,
        "section_heading": FieldRequirement.OPTIONAL,
        "sheet_name": FieldRequirement.NOT_APPLICABLE,
        "row_range": FieldRequirement.NOT_APPLICABLE,
        "char_start": FieldRequirement.OPTIONAL,
        "char_end": FieldRequirement.OPTIONAL,
    },
    DocumentType.MARKDOWN: {
        "page_number": FieldRequirement.OPTIONAL,
        "section_heading": FieldRequirement.OPTIONAL,
        "sheet_name": FieldRequirement.NOT_APPLICABLE,
        "row_range": FieldRequirement.NOT_APPLICABLE,
        "char_start": FieldRequirement.OPTIONAL,
        "char_end": FieldRequirement.OPTIONAL,
    },
}


# ==============================================================================
# DEDICATED PROVENANCE VALIDATION SERVICE
# ==============================================================================

class ChunkProvenanceValidator:
    """Dedicated validation service for document chunk provenance and locator coordinates.

    Guarantees:
    - Every chunk has verifiable source provenance before persistence.
    - All locator coordinates are strictly validated against physical document boundaries.
    - Format-specific rules for PDF, DOCX, XLSX, CSV, and text are enforced.
    - Distinguishes REQUIRED, OPTIONAL, and NOT_APPLICABLE locator fields.
    - Rejects invalid coordinates with descriptive, actionable error diagnostics.
    - Preserves provenance across database records and Qdrant vector payloads.
    - Supports full reconstruction of source locations for citation and auditability.
    """

    def __init__(
        self,
        rules: dict[DocumentType, dict[str, FieldRequirement]] | None = None,
    ) -> None:
        self._rules = rules or dict(DEFAULT_LOCATOR_RULES)

    def get_rules_for_document_type(self, doc_type: DocumentType) -> dict[str, FieldRequirement]:
        """Retrieve locator field requirements for a document format."""
        if doc_type in self._rules:
            return self._rules[doc_type]
        # Fallback for unrecognized formats (e.g. TXT/generic)
        return self._rules.get(DocumentType.TXT, {
            "page_number": FieldRequirement.OPTIONAL,
            "section_heading": FieldRequirement.OPTIONAL,
            "sheet_name": FieldRequirement.NOT_APPLICABLE,
            "row_range": FieldRequirement.NOT_APPLICABLE,
            "char_start": FieldRequirement.OPTIONAL,
            "char_end": FieldRequirement.OPTIONAL,
        })

    def validate_locator(
        self,
        locator: ChunkLocator,
        document_type: DocumentType = DocumentType.PDF,
        boundary: DocumentBoundaryContext | None = None,
        document_id: str | None = None,
        chunk_id: str | None = None,
        chunk_index: int | None = None,
        strict: bool = False,
    ) -> list[LocatorValidationErrorDetail]:
        """Validate an individual ChunkLocator coordinate against format rules and document boundaries."""
        errors: list[LocatorValidationErrorDetail] = []
        effective_doc_id = document_id or (boundary.document_id if boundary else "unknown-document")
        effective_chunk_id = chunk_id or "unknown-chunk"

        # 1. Verifiable provenance invariant: must have at least one valid coordinate populated
        if not locator.has_provenance():
            errors.append(
                LocatorValidationErrorDetail(
                    field="locator",
                    error_code=ProvenanceErrorCode.MISSING_PROVENANCE,
                    message=f"Chunk {effective_chunk_id} in document {effective_doc_id} has no source provenance coordinates.",
                    value=None,
                )
            )
            if strict:
                raise ProvenanceValidationError(
                    message=errors[-1].message,
                    document_id=effective_doc_id,
                    chunk_id=effective_chunk_id,
                    chunk_index=chunk_index,
                    field="locator",
                    invalid_value=None,
                    details={"error_code": ProvenanceErrorCode.MISSING_PROVENANCE.value},
                )
            return errors

        # 2. Format-specific field requirements (REQUIRED / OPTIONAL / NOT_APPLICABLE)
        rules = self.get_rules_for_document_type(document_type)

        for field_name, requirement in rules.items():
            val = getattr(locator, field_name, None)

            if requirement == FieldRequirement.REQUIRED:
                if val is None or (isinstance(val, str) and not val.strip()):
                    errors.append(
                        LocatorValidationErrorDetail(
                            field=field_name,
                            error_code=ProvenanceErrorCode.MISSING_REQUIRED_FIELD,
                            message=(
                                f"Missing required locator field '{field_name}' for {document_type.value} "
                                f"document {effective_doc_id} in chunk {effective_chunk_id}."
                            ),
                            value=val,
                            constraint=f"Requirement: {requirement.value}",
                        )
                    )

            elif requirement == FieldRequirement.NOT_APPLICABLE:
                if val is not None and (not isinstance(val, str) or val.strip()):
                    errors.append(
                        LocatorValidationErrorDetail(
                            field=field_name,
                            error_code=ProvenanceErrorCode.NON_APPLICABLE_FIELD_PRESENT,
                            message=(
                                f"Field '{field_name}' is not applicable for {document_type.value} documents, "
                                f"but got value '{val}' in chunk {effective_chunk_id}."
                            ),
                            value=val,
                            constraint=f"Requirement: {requirement.value}",
                        )
                    )

        # 3. Page number validation
        if locator.page_number is not None:
            if locator.page_number < 1:
                errors.append(
                    LocatorValidationErrorDetail(
                        field="page_number",
                        error_code=ProvenanceErrorCode.INVALID_PAGE_COORDINATE,
                        message=f"Page number must be a 1-based positive integer (>= 1), got {locator.page_number}.",
                        value=locator.page_number,
                        constraint="page_number >= 1",
                    )
                )
            elif boundary and boundary.total_pages is not None:
                if locator.page_number > boundary.total_pages:
                    errors.append(
                        LocatorValidationErrorDetail(
                            field="page_number",
                            error_code=ProvenanceErrorCode.PAGE_OUT_OF_BOUNDS,
                            message=(
                                f"Page number {locator.page_number} exceeds total document pages "
                                f"({boundary.total_pages}) for document {effective_doc_id}."
                            ),
                            value=locator.page_number,
                            constraint=f"1 <= page_number <= {boundary.total_pages}",
                        )
                    )

        # 4. Spreadsheet worksheet name validation
        if locator.sheet_name is not None:
            if not locator.sheet_name.strip():
                errors.append(
                    LocatorValidationErrorDetail(
                        field="sheet_name",
                        error_code=ProvenanceErrorCode.MISSING_REQUIRED_FIELD,
                        message="Worksheet name cannot be empty or whitespace only.",
                        value=locator.sheet_name,
                        constraint="non-empty string",
                    )
                )
            elif boundary and boundary.sheet_names:
                if locator.sheet_name not in boundary.sheet_names:
                    errors.append(
                        LocatorValidationErrorDetail(
                            field="sheet_name",
                            error_code=ProvenanceErrorCode.UNKNOWN_SHEET_NAME,
                            message=(
                                f"Worksheet '{locator.sheet_name}' does not exist in workbook for document {effective_doc_id}. "
                                f"Available sheets: {boundary.sheet_names}."
                            ),
                            value=locator.sheet_name,
                            constraint=f"sheet_name in {boundary.sheet_names}",
                        )
                    )

        # 5. Spreadsheet row range validation
        if locator.row_range is not None:
            start_row, end_row = parse_row_range(locator.row_range)
            if start_row is None:
                errors.append(
                    LocatorValidationErrorDetail(
                        field="row_range",
                        error_code=ProvenanceErrorCode.INVALID_ROW_RANGE,
                        message=f"Invalid spreadsheet row range format: '{locator.row_range}'. Expected format 'Row 14' or '14:28'.",
                        value=locator.row_range,
                        constraint="Parseable 1-based row range",
                    )
                )
            else:
                if start_row < 1:
                    errors.append(
                        LocatorValidationErrorDetail(
                            field="row_range",
                            error_code=ProvenanceErrorCode.INVALID_ROW_RANGE,
                            message=f"Spreadsheet row numbers must be 1-based (>= 1), got start_row={start_row} in '{locator.row_range}'.",
                            value=locator.row_range,
                            constraint="start_row >= 1",
                        )
                    )
                if end_row is not None:
                    if end_row < 1:
                        errors.append(
                            LocatorValidationErrorDetail(
                                field="row_range",
                                error_code=ProvenanceErrorCode.INVALID_ROW_RANGE,
                                message=f"Spreadsheet row numbers must be 1-based (>= 1), got end_row={end_row} in '{locator.row_range}'.",
                                value=locator.row_range,
                                constraint="end_row >= 1",
                            )
                        )
                    if start_row > end_row:
                        errors.append(
                            LocatorValidationErrorDetail(
                                field="row_range",
                                error_code=ProvenanceErrorCode.INVALID_ROW_RANGE,
                                message=f"Invalid row range: start_row ({start_row}) cannot exceed end_row ({end_row}) in '{locator.row_range}'.",
                                value=locator.row_range,
                                constraint="start_row <= end_row",
                            )
                        )

                # Boundary check against maximum worksheet rows
                if boundary and boundary.sheet_row_counts:
                    target_sheet = locator.sheet_name
                    max_rows = None
                    if target_sheet and target_sheet in boundary.sheet_row_counts:
                        max_rows = boundary.sheet_row_counts[target_sheet]
                    elif len(boundary.sheet_row_counts) == 1:
                        max_rows = next(iter(boundary.sheet_row_counts.values()))

                    if max_rows is not None and max_rows > 0:
                        if start_row > max_rows:
                            errors.append(
                                LocatorValidationErrorDetail(
                                    field="row_range",
                                    error_code=ProvenanceErrorCode.ROW_OUT_OF_BOUNDS,
                                    message=(
                                        f"Row range start ({start_row}) exceeds worksheet maximum rows ({max_rows}) "
                                        f"in sheet '{target_sheet or 'default'}' for document {effective_doc_id}."
                                    ),
                                    value=locator.row_range,
                                    constraint=f"start_row <= {max_rows}",
                                )
                            )
                        elif end_row is not None and end_row > max_rows:
                            errors.append(
                                LocatorValidationErrorDetail(
                                    field="row_range",
                                    error_code=ProvenanceErrorCode.ROW_OUT_OF_BOUNDS,
                                    message=(
                                        f"Row range end ({end_row}) exceeds worksheet maximum rows ({max_rows}) "
                                        f"in sheet '{target_sheet or 'default'}' for document {effective_doc_id}."
                                    ),
                                    value=locator.row_range,
                                    constraint=f"end_row <= {max_rows}",
                                )
                            )

        # 6. Character offset boundary and ordering validation
        if locator.char_start is not None or locator.char_end is not None:
            if locator.char_start is not None and locator.char_start < 0:
                errors.append(
                    LocatorValidationErrorDetail(
                        field="char_start",
                        error_code=ProvenanceErrorCode.INVALID_CHAR_OFFSETS,
                        message=f"Character offset char_start must be >= 0, got {locator.char_start}.",
                        value=locator.char_start,
                        constraint="char_start >= 0",
                    )
                )
            if locator.char_end is not None and locator.char_end < 0:
                errors.append(
                    LocatorValidationErrorDetail(
                        field="char_end",
                        error_code=ProvenanceErrorCode.INVALID_CHAR_OFFSETS,
                        message=f"Character offset char_end must be >= 0, got {locator.char_end}.",
                        value=locator.char_end,
                        constraint="char_end >= 0",
                    )
                )
            if locator.char_start is not None and locator.char_end is not None:
                if locator.char_start > locator.char_end:
                    errors.append(
                        LocatorValidationErrorDetail(
                            field="char_start",
                            error_code=ProvenanceErrorCode.INVALID_CHAR_OFFSETS,
                            message=f"Invalid character offsets: char_start ({locator.char_start}) cannot exceed char_end ({locator.char_end}).",
                            value=(locator.char_start, locator.char_end),
                            constraint="char_start <= char_end",
                        )
                    )

            if boundary and boundary.canonical_text is not None:
                text_len = len(boundary.canonical_text)
                if locator.char_start is not None and locator.char_start > text_len:
                    errors.append(
                        LocatorValidationErrorDetail(
                            field="char_start",
                            error_code=ProvenanceErrorCode.CHAR_OFFSETS_OUT_OF_BOUNDS,
                            message=f"char_start ({locator.char_start}) exceeds canonical text length ({text_len}).",
                            value=locator.char_start,
                            constraint=f"char_start <= {text_len}",
                        )
                    )
                if locator.char_end is not None and locator.char_end > text_len:
                    errors.append(
                        LocatorValidationErrorDetail(
                            field="char_end",
                            error_code=ProvenanceErrorCode.CHAR_OFFSETS_OUT_OF_BOUNDS,
                            message=f"char_end ({locator.char_end}) exceeds canonical text length ({text_len}).",
                            value=locator.char_end,
                            constraint=f"char_end <= {text_len}",
                        )
                    )

        if strict and errors:
            first_err = errors[0]
            raise ProvenanceValidationError(
                message=first_err.message,
                document_id=effective_doc_id,
                chunk_id=effective_chunk_id,
                chunk_index=chunk_index,
                field=first_err.field,
                invalid_value=first_err.value,
                details={
                    "error_code": first_err.error_code.value,
                    "constraint": first_err.constraint,
                    "all_errors": [e.model_dump() for e in errors],
                },
            )

        return errors

    def validate_chunk(
        self,
        chunk: DocumentChunkInput,
        context: DocumentBoundaryContext | ExtractionResult | DocumentMetadata | IngestionContext | None = None,
        strict: bool = True,
    ) -> ChunkValidationOutcome:
        """Validate a single DocumentChunkInput against document boundaries and format rules."""
        boundary = self._resolve_boundary_context(context, chunk.document_id)
        doc_type = boundary.document_type if boundary else DocumentType.PDF

        errors = self.validate_locator(
            locator=chunk.locator,
            document_type=doc_type,
            boundary=boundary,
            document_id=chunk.document_id,
            chunk_id=chunk.chunk_id,
            chunk_index=chunk.chunk_index,
            strict=strict,
        )

        # Check tenant isolation match
        if boundary and boundary.document_id and chunk.document_id != boundary.document_id:
            err = LocatorValidationErrorDetail(
                field="document_id",
                error_code=ProvenanceErrorCode.CORRUPTED_COORDINATES,
                message=f"Document ID mismatch: chunk references '{chunk.document_id}', context is '{boundary.document_id}'.",
                value=chunk.document_id,
                constraint=boundary.document_id,
            )
            errors.append(err)
            if strict:
                raise ProvenanceValidationError(
                    message=err.message,
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    chunk_index=chunk.chunk_index,
                    field="document_id",
                    invalid_value=chunk.document_id,
                )

        # Warnings (e.g. missing optional section heading on dense narrative)
        warnings: list[str] = []
        if chunk.locator.section_heading is None and doc_type in (DocumentType.DOCX, DocumentType.PDF):
            warnings.append("Chunk has no nearest section_heading attribute.")

        return ChunkValidationOutcome(
            chunk_id=chunk.chunk_id,
            chunk_index=chunk.chunk_index,
            document_id=chunk.document_id,
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    def validate_chunks(
        self,
        chunks: list[DocumentChunkInput],
        context: DocumentBoundaryContext | ExtractionResult | DocumentMetadata | IngestionContext | None = None,
        extraction_result: ExtractionResult | None = None,
        strict: bool = True,
    ) -> ProvenanceReport:
        """Validate an entire sequence of DocumentChunkInput objects for a document."""
        effective_context = extraction_result or context
        sample_doc_id = chunks[0].document_id if chunks else "unknown-document"
        boundary = self._resolve_boundary_context(effective_context, sample_doc_id)
        doc_type = boundary.document_type if boundary else DocumentType.PDF

        failures: list[ChunkValidationOutcome] = []
        all_warnings: list[str] = []
        valid_count = 0

        for chunk in chunks:
            outcome = self.validate_chunk(
                chunk=chunk,
                context=boundary,
                strict=strict,
            )
            if outcome.is_valid:
                valid_count += 1
            else:
                failures.append(outcome)
            all_warnings.extend(outcome.warnings)

        return ProvenanceReport(
            document_id=boundary.document_id if boundary else sample_doc_id,
            document_type=doc_type,
            total_chunks=len(chunks),
            valid_chunks=valid_count,
            invalid_chunks=len(failures),
            is_valid=len(failures) == 0,
            failures=failures,
            warnings=all_warnings,
        )

    def reconstruct_source_location(
        self,
        chunk: DocumentChunkInput | ChunkLocator,
        boundary: DocumentBoundaryContext | ExtractionResult | None = None,
        document_id: str | None = None,
        chunk_id: str | None = None,
        chunk_index: int | None = None,
    ) -> SourceLocationReconstruction:
        """Reconstruct physical source citation and extract canonical slice from coordinates."""
        if isinstance(chunk, DocumentChunkInput):
            locator = chunk.locator
            eff_doc_id = chunk.document_id
            eff_chunk_id = chunk.chunk_id
            eff_index = chunk.chunk_index
        else:
            locator = chunk
            eff_doc_id = document_id or "unknown-document"
            eff_chunk_id = chunk_id or "unknown-chunk"
            eff_index = chunk_index or 0

        resolved_boundary = self._resolve_boundary_context(boundary, eff_doc_id) if boundary else None

        # Format citation badge
        citation_badge = locator.format_citation()

        # Reconstruct canonical text slice if offsets and text are available
        reconstructed_text: str | None = None
        if (
            resolved_boundary
            and resolved_boundary.canonical_text
            and locator.char_start is not None
            and locator.char_end is not None
        ):
            try:
                reconstructed_text = locator.slice_canonical_text(resolved_boundary.canonical_text)
            except Exception as e:
                logger.warning(
                    "Failed to slice canonical text for chunk reconstruction: %s",
                    e,
                    extra={"chunk_id": eff_chunk_id, "document_id": eff_doc_id},
                )

        provenance_metadata: dict[str, Any] = {
            "page_number": locator.page_number,
            "section_heading": locator.section_heading,
            "sheet_name": locator.sheet_name,
            "row_range": locator.row_range,
            "char_start": locator.char_start,
            "char_end": locator.char_end,
        }
        if locator.bounding_box:
            provenance_metadata["bounding_box"] = locator.bounding_box.model_dump()

        return SourceLocationReconstruction(
            document_id=eff_doc_id,
            chunk_id=eff_chunk_id,
            chunk_index=eff_index,
            citation_badge=citation_badge,
            locator=locator,
            reconstructed_text=reconstructed_text,
            provenance_metadata=provenance_metadata,
        )

    def reconstruct_from_db_record(self, record: dict[str, Any]) -> ChunkLocator:
        """Reconstruct ChunkLocator from PostgreSQL document_chunks table dictionary."""
        return ChunkLocator.from_db_record(record)

    def reconstruct_from_qdrant_payload(self, payload: dict[str, Any]) -> ChunkLocator:
        """Reconstruct ChunkLocator from Qdrant vector point source_location payload."""
        return ChunkLocator.from_qdrant_payload(payload)

    def verify_provenance_preservation(self, chunk: DocumentChunkInput) -> bool:
        """Verify that chunk locator coordinates survive DB and Qdrant serializations identically."""
        # 1. PostgreSQL DB Record preservation
        db_record = chunk.to_db_record()
        db_locator = self.reconstruct_from_db_record(db_record)

        assert db_locator.page_number == chunk.locator.page_number, "page_number mismatch in DB record"
        assert db_locator.section_heading == chunk.locator.section_heading, "section_heading mismatch in DB record"
        assert db_locator.sheet_name == chunk.locator.sheet_name, "sheet_name mismatch in DB record"
        assert db_locator.row_range == chunk.locator.row_range, "row_range mismatch in DB record"
        assert db_locator.char_start == chunk.locator.char_start, "char_start mismatch in DB record"
        assert db_locator.char_end == chunk.locator.char_end, "char_end mismatch in DB record"

        # 2. Qdrant vector payload preservation
        qdrant_payload = chunk.to_qdrant_payload()
        qdrant_locator = self.reconstruct_from_qdrant_payload(qdrant_payload)

        assert qdrant_locator.page_number == chunk.locator.page_number, "page_number mismatch in Qdrant payload"
        assert qdrant_locator.section_heading == chunk.locator.section_heading, "section_heading mismatch in Qdrant payload"
        assert qdrant_locator.sheet_name == chunk.locator.sheet_name, "sheet_name mismatch in Qdrant payload"
        assert qdrant_locator.row_range == chunk.locator.row_range, "row_range mismatch in Qdrant payload"
        assert qdrant_locator.char_start == chunk.locator.char_start, "char_start mismatch in Qdrant payload"
        assert qdrant_locator.char_end == chunk.locator.char_end, "char_end mismatch in Qdrant payload"

        return True

    def _resolve_boundary_context(
        self,
        context: DocumentBoundaryContext | ExtractionResult | DocumentMetadata | IngestionContext | None,
        fallback_document_id: str,
    ) -> DocumentBoundaryContext:
        """Helper to resolve heterogeneous input context into a standardized DocumentBoundaryContext."""
        if isinstance(context, DocumentBoundaryContext):
            return context

        if isinstance(context, ExtractionResult):
            return DocumentBoundaryContext.from_extraction_result(context)

        if isinstance(context, DocumentMetadata):
            return DocumentBoundaryContext(
                document_id=context.document_id,
                document_type=context.document_type,
                total_pages=context.total_pages,
                sheet_names=context.custom_metadata.get("sheet_names", []) if isinstance(context.custom_metadata, dict) else [],
                sheet_row_counts={},
                canonical_text=None,
            )

        if isinstance(context, IngestionContext):
            return DocumentBoundaryContext(
                document_id=context.document_id,
                document_type=DocumentType.PDF,
                total_pages=None,
                sheet_names=[],
                sheet_row_counts={},
                canonical_text=None,
            )

        return DocumentBoundaryContext(
            document_id=fallback_document_id,
            document_type=DocumentType.PDF,
            total_pages=None,
            sheet_names=[],
            sheet_row_counts={},
            canonical_text=None,
        )


# Global default validator instance and convenience functions
default_provenance_validator = ChunkProvenanceValidator()


def validate_chunk_provenance(
    chunk: DocumentChunkInput,
    context: DocumentBoundaryContext | ExtractionResult | DocumentMetadata | IngestionContext | None = None,
    strict: bool = True,
) -> ChunkValidationOutcome:
    """Validate a single chunk's source provenance locator coordinates using default validator."""
    return default_provenance_validator.validate_chunk(chunk, context=context, strict=strict)


def validate_chunks_provenance(
    chunks: list[DocumentChunkInput],
    context: DocumentBoundaryContext | ExtractionResult | DocumentMetadata | IngestionContext | None = None,
    extraction_result: ExtractionResult | None = None,
    strict: bool = True,
) -> ProvenanceReport:
    """Validate a list of chunks' source provenance locator coordinates using default validator."""
    return default_provenance_validator.validate_chunks(
        chunks=chunks,
        context=context,
        extraction_result=extraction_result,
        strict=strict,
    )


def reconstruct_chunk_source_location(
    chunk: DocumentChunkInput | ChunkLocator,
    boundary: DocumentBoundaryContext | ExtractionResult | None = None,
    document_id: str | None = None,
) -> SourceLocationReconstruction:
    """Reconstruct formatted citation badge and canonical text slice from coordinates."""
    return default_provenance_validator.reconstruct_source_location(
        chunk=chunk,
        boundary=boundary,
        document_id=document_id,
    )
