"""Spreadsheet document extraction adapters for XLSX and CSV.

Provides production-grade structured tabular extraction preserving:
- Multiple worksheets and worksheet names.
- Column headers with duplicate and missing title disambiguation.
- Exact 1-based original physical worksheet row numbers, including header rows and gaps.
- Full column-to-value relationships in both structured TableElement and individual SpreadsheetRowElement records.
- Cell data type conversion (integers, floats, dates, datetimes, booleans, strings).
- Merged cell value propagation across merged cell spans.
- Detectable table boundaries (explicit OpenXML tables or bounding-box detection).
- Documented formula evaluation policy (cached value extraction with fallback to formula expressions).
- Memory-efficient processing: streaming read-only mode for large spreadsheets and row bounds.
- Rejection of encrypted, password-protected, or corrupted files.
- Complete source provenance traceability and canonical string offset slicing.
"""

import csv
import datetime
import hashlib
import io
import logging
import re
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO, Literal

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.exceptions import (
    CorruptDocumentError,
    EmptyFileError,
    EncryptedDocumentError,
    ExtractionError,
    FileSignatureMismatchError,
    MalformedFileError,
    UnsupportedDocumentTypeError,
)
from backend.ingestion.extraction.base import DocumentExtractor
from backend.ingestion.extraction.registry import register_extractor
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.coordinates import ChunkLocator
from backend.ingestion.types.elements import (
    ExtractedElement,
    HeadingElement,
    NormalizedElement,
    SpreadsheetRowElement,
    TableElement,
)
from backend.ingestion.types.enums import DocumentMimeType, DocumentType, ElementType
from backend.ingestion.types.extraction import ExtractionResult, ExtractionStats
from backend.ingestion.types.metadata import DocumentMetadata
from backend.ingestion.validation.signatures import (
    ALL_ZIP_MAGICS,
    EXECUTABLE_MAGICS,
    validate_csv_integrity,
    validate_xlsx_integrity,
)

logger = logging.getLogger(__name__)

# Standard OLE Compound Document signature (Excel 97-2004 or encrypted OpenXML container)
OLE_MAGIC_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

FORMULA_POLICY_DOCUMENTATION: str = """
Spreadsheet Formula Evaluation and Cached Value Policy:
1. Primary Mode (Cached Evaluations): In enterprise retrieval and RAG search, users
   query for computed numerical results (e.g., total expenses, quarterly revenues, tax amounts).
   The extractor loads workbooks in 'data_only=True' mode to access the precomputed cached values
   evaluated and stored by Excel.
2. Missing Cached Value Fallback: If a workbook was generated programmatically (e.g. without an
   Excel calculation engine) or cached values are absent (openpyxl returns None in data_only mode),
   the extractor detects this condition and falls back to extracting the literal formula string
   (e.g., '=SUM(B2:B10)'), annotating the row metadata with {'is_formula': True, 'formula': formula_text}
   so that calculation semantics are not lost.
3. Formula Errors: Standard spreadsheet error codes ('#DIV/0!', '#N/A', '#REF!', '#VALUE!',
   '#NAME?', '#NUM!', '#NULL!') are captured verbatim as strings and flagged in element metadata.
"""


def format_cell_value(val: Any) -> Any:
    """Normalize raw cell value into typed Python primitive or formatted string."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        # Normalize whole floats (e.g., 100.0) to integers (100)
        if isinstance(val, float) and val.is_integer():
            return int(val)
        return val
    if isinstance(val, datetime.datetime):
        if val.time() == datetime.time(0, 0, 0):
            return val.date().isoformat()
        return val.isoformat()
    if isinstance(val, datetime.date):
        return val.isoformat()
    if isinstance(val, datetime.time):
        return val.isoformat()
    if isinstance(val, str):
        s = val.strip()
        return s if s else None
    return str(val).strip()


def cell_to_markdown_str(val: Any) -> str:
    """Format cell value for safe inclusion in a GitHub-Flavored Markdown table cell."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val).strip().replace("\n", " ").replace("|", "\\|")


def disambiguate_headers(raw_headers: list[str]) -> list[str]:
    """Ensure column headers are non-empty and unique, appending numerical suffixes to duplicates."""
    counts: dict[str, int] = {}
    clean_headers: list[str] = []

    for idx, h in enumerate(raw_headers, start=1):
        name = str(h).strip() if h is not None and str(h).strip() else f"Column_{idx}"
        if name in counts:
            counts[name] += 1
            unique_name = f"{name}_{counts[name]}"
        else:
            counts[name] = 1
            unique_name = name
        clean_headers.append(unique_name)

    return clean_headers


@register_extractor()
class XlsxExtractor(DocumentExtractor):
    """Production-grade Microsoft Excel (XLSX) document extractor using openpyxl.

    Extracts multi-worksheet spreadsheets preserving:
    - Worksheet names and order.
    - Column headers and column-to-value associations.
    - Original 1-based worksheet row positions, including header rows and gaps.
    - Merged cells expanded across cell spans.
    - Table boundaries (explicit named tables or detected data bounds).
    - Normalized tabular representations (TableElement and SpreadsheetRowElement).
    - Read-only streaming for large spreadsheets with controlled memory footprint.
    - Documented formula evaluation policy (cached value with formula fallback).
    """

    def __init__(
        self,
        streaming_threshold_bytes: int | None = None,
        max_rows_per_sheet: int | None = None,
        formula_policy: str | None = None,
        emit_tables: bool = True,
        emit_rows: bool = True,
    ) -> None:
        """Initialize XlsxExtractor with configurable ingestion settings."""
        self._settings = get_ingestion_settings()
        self._streaming_threshold_bytes = (
            streaming_threshold_bytes
            if streaming_threshold_bytes is not None
            else self._settings.spreadsheet_streaming_threshold_bytes
        )
        self._max_rows_per_sheet = (
            max_rows_per_sheet
            if max_rows_per_sheet is not None
            else self._settings.spreadsheet_max_rows_per_sheet
        )
        self._formula_policy = (
            formula_policy
            if formula_policy is not None
            else self._settings.spreadsheet_formula_policy
        )
        self._emit_tables = emit_tables
        self._emit_rows = emit_rows

    @property
    def name(self) -> str:
        return "XlsxExtractor"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def supported_document_types(self) -> list[DocumentType]:
        return [DocumentType.XLSX]

    @property
    def supported_mime_types(self) -> list[str]:
        return [
            DocumentMimeType.XLSX.value,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "application/x-excel",
        ]

    @property
    def supports_ocr(self) -> bool:
        return False

    async def extract(
        self,
        content: bytes | BinaryIO | Path,
        context: IngestionContext,
        metadata: DocumentMetadata | None = None,
    ) -> ExtractionResult:
        """Asynchronously extract structured tabular content from an XLSX workbook."""
        start_time = time.perf_counter()

        # 1. Resolve content input and source filename
        raw_bytes, source_path, source_filename = self._resolve_content(content)
        file_size = len(raw_bytes) if raw_bytes is not None else source_path.stat().st_size

        if file_size == 0:
            raise EmptyFileError(f"Input XLSX spreadsheet '{source_filename}' is empty (0 bytes).")

        # 2. Verify file integrity and detect password-protected / encrypted files
        sample_bytes = raw_bytes[:4096] if raw_bytes is not None else source_path.read_bytes()[:4096]
        self._check_file_integrity(sample_bytes, raw_bytes, source_filename)

        # 3. Determine runtime options from context or defaults
        emit_tables = getattr(context.options, "emit_table_elements", self._emit_tables)
        emit_rows = getattr(context.options, "emit_row_elements", self._emit_rows)
        max_rows = (
            getattr(context.options, "max_rows_per_sheet", None)
            or self._max_rows_per_sheet
        )

        # Determine whether to use read-only streaming mode
        use_streaming = file_size > self._streaming_threshold_bytes

        # 4. Open and process workbook
        elements: list[ExtractedElement] = []
        warnings: list[str] = []
        sheet_summaries: list[dict[str, Any]] = []

        if use_streaming:
            elements, warnings, sheet_summaries = self._extract_streaming(
                source=source_path if source_path is not None else io.BytesIO(raw_bytes),
                source_filename=source_filename,
                max_rows=max_rows,
                emit_tables=emit_tables,
                emit_rows=emit_rows,
            )
        else:
            elements, warnings, sheet_summaries = self._extract_standard(
                source=source_path if source_path is not None else io.BytesIO(raw_bytes),
                source_filename=source_filename,
                raw_bytes=raw_bytes,
                max_rows=max_rows,
                emit_tables=emit_tables,
                emit_rows=emit_rows,
            )

        # 5. Check if entire workbook has no extractable elements
        if not elements:
            warning_msg = (
                f"XLSX spreadsheet '{source_filename}' contains no extractable data or rows."
            )
            warnings.append(warning_msg)
            logger.warning(warning_msg)

        # 6. Build enriched DocumentMetadata
        doc_metadata = self._build_metadata(
            source_path=source_path,
            raw_bytes=raw_bytes,
            context=context,
            metadata=metadata,
            source_filename=source_filename,
            file_size=file_size,
            sheet_summaries=sheet_summaries,
            total_elements=len(elements),
        )

        # 7. Compute execution stats
        duration = time.perf_counter() - start_time
        stats = ExtractionStats(
            extractor_name=self.name,
            extractor_version=self.version,
            duration_seconds=round(duration, 4),
            total_pages=len(sheet_summaries),  # Worksheets map to logical pages
            total_elements=len(elements),
            total_characters=sum(len(e.text_content) for e in elements),
            ocr_applied=False,
        )

        # 8. Construct ExtractionResult
        result = ExtractionResult(
            document_id=context.document_id,
            document_type=DocumentType.XLSX,
            mime_type=DocumentMimeType.XLSX.value,
            elements=elements,
            metadata=doc_metadata,
            warnings=warnings,
            stats=stats,
        )

        # 9. Compute exact canonical char offsets and preserve reading order
        result.compute_canonical_offsets(separator="\n\n")

        # 10. Validate source traceability coordinates
        result.validate_traceability(strict=True)

        return result

    def _resolve_content(
        self,
        content: bytes | BinaryIO | Path,
    ) -> tuple[bytes | None, Path | None, str]:
        """Normalize content input into raw bytes or file path."""
        if isinstance(content, Path):
            if not content.exists() or not content.is_file():
                raise CorruptDocumentError(f"Target XLSX file does not exist: {content}")
            return None, content, content.name

        if isinstance(content, bytes):
            return content, None, "workbook.xlsx"

        if hasattr(content, "read"):
            data = content.read()
            if isinstance(data, str):
                data = data.encode("utf-8")
            name = getattr(content, "name", "workbook.xlsx")
            return data, None, Path(str(name)).name

        raise UnsupportedDocumentTypeError(
            f"Unsupported content type for XLSX extraction: {type(content)}"
        )

    def _check_file_integrity(
        self,
        sample_bytes: bytes,
        raw_bytes: bytes | None,
        filename: str,
    ) -> None:
        """Verify binary headers and detect encrypted / password-protected workbooks."""
        # 1. Check for OLE Compound Document (legacy .xls or encrypted OpenXML container)
        if sample_bytes.startswith(OLE_MAGIC_BYTES):
            full_header = raw_bytes[:16384] if raw_bytes is not None else sample_bytes
            if b"EncryptedPackage" in full_header or b"EncryptionInfo" in full_header:
                raise EncryptedDocumentError(
                    f"XLSX document '{filename}' is password-protected or encrypted and cannot be parsed."
                )
            raise UnsupportedDocumentTypeError(
                f"Binary legacy Excel format (.xls) is not supported for '{filename}'. Please convert to modern XLSX format."
            )

        # 2. Check for standard ZIP magic bytes
        if not any(sample_bytes.startswith(m) for m in ALL_ZIP_MAGICS):
            raise CorruptDocumentError(
                f"File '{filename}' does not have a valid OpenXML/ZIP header signature."
            )

        # 3. If raw_bytes available, validate internal OpenXML layout
        if raw_bytes is not None:
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    namelist = set(zf.namelist())
                    # Disallow DOCX renamed to XLSX
                    if "word/document.xml" in namelist:
                        raise FileSignatureMismatchError(
                            f"File '{filename}' contains DOCX document structure, not XLSX spreadsheet."
                        )
                    has_xl = "xl/workbook.xml" in namelist or any(n.startswith("xl/") for n in namelist)
                    if not has_xl and "[Content_Types].xml" not in namelist:
                        raise CorruptDocumentError(
                            f"Archive '{filename}' is missing essential XLSX parts ('xl/workbook.xml')."
                        )
            except zipfile.BadZipFile as e:
                raise CorruptDocumentError(f"Corrupted ZIP archive for XLSX document '{filename}': {e}") from e

    def _extract_standard(
        self,
        source: Any,
        source_filename: str,
        raw_bytes: bytes | None,
        max_rows: int,
        emit_tables: bool,
        emit_rows: bool,
    ) -> tuple[list[ExtractedElement], list[str], list[dict[str, Any]]]:
        """Extract workbook in standard in-memory mode with full merged cell propagation."""
        elements: list[ExtractedElement] = []
        warnings: list[str] = []
        sheet_summaries: list[dict[str, Any]] = []

        # Open in data_only mode (retrieves evaluated cached values)
        try:
            wb = openpyxl.load_workbook(source, data_only=True)
        except Exception as e:
            err_str = str(e).lower()
            if "encrypted" in err_str or "password" in err_str:
                raise EncryptedDocumentError(
                    f"XLSX document '{source_filename}' is encrypted or password-protected: {e}"
                ) from e
            raise CorruptDocumentError(f"Failed to open XLSX document '{source_filename}': {e}") from e

        # Optional second workbook handle for formula fallback if needed
        wb_formulas: openpyxl.Workbook | None = None

        try:
            for ws in wb.worksheets:
                sheet_name = ws.title
                summary = {
                    "sheet_name": sheet_name,
                    "max_row": ws.max_row,
                    "max_column": ws.max_column,
                    "total_rows": 0,
                    "total_data_rows": 0,
                    "table_boundaries": None,
                }

                # 1. Build merged cell lookup map
                merged_map = self._build_merged_cells_map(ws)

                # 2. Detect table boundaries and non-empty rows
                data_bounds = self._detect_sheet_bounds(ws, merged_map)
                if data_bounds is None:
                    warnings.append(f"Worksheet '{sheet_name}' is empty.")
                    sheet_summaries.append(summary)
                    continue

                min_row, max_row_val, min_col, max_col = data_bounds
                summary["table_boundaries"] = {
                    "min_row": min_row,
                    "max_row": max_row_val,
                    "min_col": min_col,
                    "max_col": max_col,
                }

                # 3. Detect column headers row
                header_row_idx, raw_headers = self._detect_header_row(
                    ws=ws,
                    min_row=min_row,
                    max_row=max_row_val,
                    min_col=min_col,
                    max_col=max_col,
                    merged_map=merged_map,
                )
                clean_headers = disambiguate_headers(raw_headers)

                # 4. Extract data rows preserving original 1-based row indices and gaps
                table_body_rows: list[list[Any]] = []
                original_row_indices: list[int] = []
                data_row_count = 0
                gaps: list[int] = []

                # Lazy load formula workbook if formula fallback is required
                def get_formula_val(r_idx: int, c_idx: int) -> Any:
                    nonlocal wb_formulas
                    if wb_formulas is None:
                        try:
                            if raw_bytes is not None:
                                wb_formulas = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
                            elif isinstance(source, Path):
                                wb_formulas = openpyxl.load_workbook(source, data_only=False)
                        except Exception:
                            return None
                    if wb_formulas and sheet_name in wb_formulas.sheetnames:
                        return wb_formulas[sheet_name].cell(row=r_idx, column=c_idx).value
                    return None

                for row_idx in range(header_row_idx + 1, max_row_val + 1):
                    if data_row_count >= max_rows:
                        warnings.append(
                            f"Worksheet '{sheet_name}' truncated at {max_rows} rows limit."
                        )
                        break

                    row_cells_dict: dict[str, Any] = {}
                    row_values_list: list[Any] = []
                    has_data = False

                    for col_offset, col_idx in enumerate(range(min_col, max_col + 1)):
                        header_name = clean_headers[col_offset]
                        cell = ws.cell(row=row_idx, column=col_idx)
                        raw_val = cell.value

                        # Check merged cell map if None
                        if raw_val is None and (row_idx, col_idx) in merged_map:
                            raw_val = merged_map[(row_idx, col_idx)]

                        # Check formula fallback if None
                        if raw_val is None and self._formula_policy in ("cached_values", "formulas", "both"):
                            form_val = get_formula_val(row_idx, col_idx)
                            if form_val is not None and str(form_val).startswith("="):
                                raw_val = str(form_val)

                        formatted_val = format_cell_value(raw_val)
                        if formatted_val is not None:
                            has_data = True

                        row_cells_dict[header_name] = formatted_val
                        row_values_list.append(formatted_val if formatted_val is not None else "")

                    # Check for empty gap row
                    if not has_data:
                        gaps.append(row_idx)
                        continue

                    data_row_count += 1
                    original_row_indices.append(row_idx)
                    table_body_rows.append(row_values_list)

                    # Emit individual SpreadsheetRowElement
                    if emit_rows:
                        row_elem = SpreadsheetRowElement(
                            sheet_name=sheet_name,
                            row_index=row_idx,
                            headers=clean_headers,
                            cells=row_cells_dict,
                            row_range=f"Row {row_idx}",
                            locator=ChunkLocator(
                                sheet_name=sheet_name,
                                row_range=f"Row {row_idx}",
                            ),
                            metadata={
                                "sheet_name": sheet_name,
                                "original_row_number": row_idx,
                                "column_count": len(clean_headers),
                            },
                        )
                        elements.append(row_elem)

                summary["total_rows"] = max_row_val
                summary["total_data_rows"] = data_row_count
                sheet_summaries.append(summary)

                # Emit structured TableElement
                if emit_tables and table_body_rows:
                    table_elem = TableElement(
                        headers=clean_headers,
                        rows=table_body_rows,
                        caption=f"Worksheet: {sheet_name}",
                        sheet_name=sheet_name,
                        row_range=f"Row {header_row_idx}:{max_row_val}",
                        locator=ChunkLocator(
                            sheet_name=sheet_name,
                            row_range=f"Row {header_row_idx}:{max_row_val}",
                        ),
                        metadata={
                            "sheet_name": sheet_name,
                            "header_row_index": header_row_idx,
                            "original_row_indices": original_row_indices,
                            "column_headers": clean_headers,
                            "row_count": len(table_body_rows),
                            "col_count": len(clean_headers),
                            "table_boundaries": summary["table_boundaries"],
                            "gaps": gaps,
                        },
                    )
                    elements.append(table_elem)

        finally:
            wb.close()
            if wb_formulas is not None:
                wb_formulas.close()

        return elements, warnings, sheet_summaries

    def _extract_streaming(
        self,
        source: Any,
        source_filename: str,
        max_rows: int,
        emit_tables: bool,
        emit_rows: bool,
    ) -> tuple[list[ExtractedElement], list[str], list[dict[str, Any]]]:
        """Extract large workbook in read-only streaming mode with minimal memory usage."""
        elements: list[ExtractedElement] = []
        warnings: list[str] = []
        sheet_summaries: list[dict[str, Any]] = []

        try:
            wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
        except Exception as e:
            err_str = str(e).lower()
            if "encrypted" in err_str or "password" in err_str:
                raise EncryptedDocumentError(
                    f"XLSX document '{source_filename}' is encrypted: {e}"
                ) from e
            raise CorruptDocumentError(f"Failed to open streaming XLSX document: {e}") from e

        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                summary = {
                    "sheet_name": sheet_name,
                    "total_data_rows": 0,
                    "table_boundaries": None,
                }

                header_row_idx: int | None = None
                clean_headers: list[str] = []
                table_body_rows: list[list[Any]] = []
                original_row_indices: list[int] = []
                data_row_count = 0
                gaps: list[int] = []

                physical_row = 0
                for row in ws.iter_rows(values_only=True):
                    physical_row += 1

                    # Check row data presence
                    formatted_row = [format_cell_value(val) for val in row]
                    has_data = any(v is not None for v in formatted_row)

                    if not has_data:
                        if header_row_idx is not None:
                            gaps.append(physical_row)
                        continue

                    # First non-empty row serves as column headers
                    if header_row_idx is None:
                        header_row_idx = physical_row
                        raw_headers = [str(v) if v is not None else "" for v in formatted_row]
                        clean_headers = disambiguate_headers(raw_headers)
                        continue

                    # Data rows
                    if data_row_count >= max_rows:
                        warnings.append(
                            f"Streaming worksheet '{sheet_name}' reached row limit of {max_rows}."
                        )
                        break

                    data_row_count += 1
                    original_row_indices.append(physical_row)

                    # Build key-value cells dictionary
                    row_cells_dict: dict[str, Any] = {}
                    row_vals_list: list[Any] = []
                    for col_idx, header_name in enumerate(clean_headers):
                        val = formatted_row[col_idx] if col_idx < len(formatted_row) else None
                        row_cells_dict[header_name] = val
                        row_vals_list.append(val if val is not None else "")

                    table_body_rows.append(row_vals_list)

                    if emit_rows:
                        row_elem = SpreadsheetRowElement(
                            sheet_name=sheet_name,
                            row_index=physical_row,
                            headers=clean_headers,
                            cells=row_cells_dict,
                            row_range=f"Row {physical_row}",
                            locator=ChunkLocator(
                                sheet_name=sheet_name,
                                row_range=f"Row {physical_row}",
                            ),
                            metadata={
                                "sheet_name": sheet_name,
                                "original_row_number": physical_row,
                                "column_count": len(clean_headers),
                            },
                        )
                        elements.append(row_elem)

                summary["total_data_rows"] = data_row_count
                sheet_summaries.append(summary)

                if emit_tables and table_body_rows:
                    table_elem = TableElement(
                        headers=clean_headers,
                        rows=table_body_rows,
                        caption=f"Worksheet: {sheet_name}",
                        sheet_name=sheet_name,
                        row_range=f"Row {header_row_idx}:{physical_row}",
                        locator=ChunkLocator(
                            sheet_name=sheet_name,
                            row_range=f"Row {header_row_idx}:{physical_row}",
                        ),
                        metadata={
                            "sheet_name": sheet_name,
                            "header_row_index": header_row_idx,
                            "original_row_indices": original_row_indices,
                            "column_headers": clean_headers,
                            "row_count": len(table_body_rows),
                            "col_count": len(clean_headers),
                            "gaps": gaps,
                        },
                    )
                    elements.append(table_elem)
        finally:
            wb.close()

        return elements, warnings, sheet_summaries

    def _build_merged_cells_map(self, ws: Worksheet) -> dict[tuple[int, int], Any]:
        """Construct a lookup map from cell coordinate (row, col) to merged top-left value."""
        merged_map: dict[tuple[int, int], Any] = {}
        if not hasattr(ws, "merged_cells") or not ws.merged_cells:
            return merged_map

        for rng in ws.merged_cells.ranges:
            top_left_val = ws.cell(row=rng.min_row, column=rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    merged_map[(r, c)] = top_left_val

        return merged_map

    def _detect_sheet_bounds(
        self,
        ws: Worksheet,
        merged_map: dict[tuple[int, int], Any],
    ) -> tuple[int, int, int, int] | None:
        """Scan cells to compute true bounding box of non-empty content (min_row, max_row, min_col, max_col)."""
        actual_min_row: int | None = None
        actual_max_row: int | None = None
        actual_min_col: int | None = None
        actual_max_col: int | None = None

        max_scan_row = min(ws.max_row or 1, 10000)
        max_scan_col = min(ws.max_column or 1, 1000)

        for r in range(1, max_scan_row + 1):
            for c in range(1, max_scan_col + 1):
                cell_val = ws.cell(row=r, column=c).value
                if cell_val is None and (r, c) in merged_map:
                    cell_val = merged_map[(r, c)]

                if cell_val is not None and str(cell_val).strip():
                    if actual_min_row is None or r < actual_min_row:
                        actual_min_row = r
                    if actual_max_row is None or r > actual_max_row:
                        actual_max_row = r
                    if actual_min_col is None or c < actual_min_col:
                        actual_min_col = c
                    if actual_max_col is None or c > actual_max_col:
                        actual_max_col = c

        if actual_min_row is None:
            return None

        return actual_min_row, actual_max_row, actual_min_col, actual_max_col  # type: ignore

    def _detect_header_row(
        self,
        ws: Worksheet,
        min_row: int,
        max_row: int,
        min_col: int,
        max_col: int,
        merged_map: dict[tuple[int, int], Any],
    ) -> tuple[int, list[str]]:
        """Identify candidate header row, skipping potential single-cell sheet titles."""
        for r in range(min_row, max_row + 1):
            row_vals: list[str] = []
            non_empty_count = 0

            for c in range(min_col, max_col + 1):
                val = ws.cell(row=r, column=c).value
                if val is None and (r, c) in merged_map:
                    val = merged_map[(r, c)]
                str_val = str(val).strip() if val is not None else ""
                row_vals.append(str_val)
                if str_val:
                    non_empty_count += 1

            # If row has more than 1 non-empty cell or if this is the only row, use as header
            if non_empty_count > 1 or (non_empty_count == 1 and min_row == max_row):
                return r, row_vals

            # If single non-empty cell but subsequent row has multiple cells, current is title
            if non_empty_count == 1 and r < max_row:
                continue

            if non_empty_count >= 1:
                return r, row_vals

        # Fallback to default generated column names
        default_headers = [f"Column_{i+1}" for i in range(max_col - min_col + 1)]
        return min_row, default_headers

    def _build_metadata(
        self,
        source_path: Path | None,
        raw_bytes: bytes | None,
        context: IngestionContext,
        metadata: DocumentMetadata | None,
        source_filename: str,
        file_size: int,
        sheet_summaries: list[dict[str, Any]],
        total_elements: int,
    ) -> DocumentMetadata:
        """Construct normalized DocumentMetadata enriching input metadata with workbook attributes."""
        file_hash = (
            metadata.file_hash_sha256
            if (metadata and getattr(metadata, "file_hash_sha256", None))
            else hashlib.sha256(raw_bytes if raw_bytes is not None else source_path.read_bytes()).hexdigest()
        )

        sheet_names = [s["sheet_name"] for s in sheet_summaries]
        custom_attrs: dict[str, Any] = {
            "sheet_names": sheet_names,
            "total_sheets": len(sheet_names),
            "total_elements": total_elements,
            "is_empty": total_elements == 0,
            "sheet_summaries": sheet_summaries,
            "formula_policy": self._formula_policy,
        }

        if metadata and getattr(metadata, "custom_metadata", None):
            custom_attrs.update(metadata.custom_metadata)

        return DocumentMetadata(
            document_id=context.document_id,
            tenant_id=context.tenant_id,
            filename=metadata.filename if (metadata and getattr(metadata, "filename", None)) else source_filename,
            file_hash_sha256=file_hash,
            file_size_bytes=file_size,
            mime_type=DocumentMimeType.XLSX.value,
            document_type=DocumentType.XLSX,
            classification=context.classification,
            department_id=context.department_id,
            uploaded_by_id=context.user_id,
            title=(metadata.title if metadata and metadata.title else None) or Path(source_filename).stem,
            total_pages=len(sheet_names),
            custom_metadata=custom_attrs,
            custom_attributes=custom_attrs,
        )


@register_extractor()
class CsvExtractor(DocumentExtractor):
    """Production-grade Comma-Separated Values (CSV) extractor using Python's standard csv module.

    Extracts plain text tabular content preserving:
    - Automatic dialect and delimiter detection (comma, tab, semicolon, pipe).
    - Robust character encoding detection (UTF-8, UTF-8-BOM, Latin-1, CP1252).
    - Column headers and column-to-value associations.
    - Original 1-based line positions, including header rows and blank line gaps.
    - Streaming row-by-row iteration for large CSV files with controlled memory.
    - Consistent TableElement and SpreadsheetRowElement output models identical to XLSX.
    """

    def __init__(
        self,
        max_rows: int | None = None,
        emit_tables: bool = True,
        emit_rows: bool = True,
    ) -> None:
        """Initialize CsvExtractor."""
        self._settings = get_ingestion_settings()
        self._max_rows = (
            max_rows
            if max_rows is not None
            else self._settings.spreadsheet_max_rows_per_sheet
        )
        self._emit_tables = emit_tables
        self._emit_rows = emit_rows

    @property
    def name(self) -> str:
        return "CsvExtractor"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def supported_document_types(self) -> list[DocumentType]:
        return [DocumentType.CSV]

    @property
    def supported_mime_types(self) -> list[str]:
        return [
            DocumentMimeType.CSV.value,
            "text/csv",
            "text/plain",
            "application/csv",
        ]

    @property
    def supports_ocr(self) -> bool:
        return False

    async def extract(
        self,
        content: bytes | BinaryIO | Path,
        context: IngestionContext,
        metadata: DocumentMetadata | None = None,
    ) -> ExtractionResult:
        """Asynchronously extract structured tabular content from a CSV document."""
        start_time = time.perf_counter()

        # 1. Resolve content bytes and filename
        raw_bytes, source_path, source_filename = self._resolve_content(content)
        file_size = len(raw_bytes) if raw_bytes is not None else source_path.stat().st_size

        if file_size == 0:
            raise EmptyFileError(f"Input CSV document '{source_filename}' is empty (0 bytes).")

        # 2. Validate structural integrity
        validate_bytes = raw_bytes if raw_bytes is not None else source_path.read_bytes()
        validate_csv_integrity(validate_bytes)

        # 3. Decode text with encoding fallback
        decoded_text = self._decode_text(validate_bytes)

        # 4. Detect delimiter and dialect
        delimiter = self._detect_delimiter(decoded_text[:8192])

        # 5. Extract rows using csv.reader
        elements: list[ExtractedElement] = []
        warnings: list[str] = []
        sheet_name = Path(source_filename).stem or "Sheet1"

        emit_tables = getattr(context.options, "emit_table_elements", self._emit_tables)
        emit_rows = getattr(context.options, "emit_row_elements", self._emit_rows)
        max_rows = (
            getattr(context.options, "max_rows_per_sheet", None)
            or self._max_rows
        )

        stream = io.StringIO(decoded_text)
        reader = csv.reader(stream, delimiter=delimiter)

        header_row_idx: int | None = None
        clean_headers: list[str] = []
        table_body_rows: list[list[Any]] = []
        original_row_indices: list[int] = []
        gaps: list[int] = []
        data_row_count = 0
        physical_line_num = 0

        for row in reader:
            physical_line_num += 1

            # Check if row is empty / whitespace-only
            cleaned_row = [str(cell).strip() for cell in row]
            has_data = any(len(c) > 0 for c in cleaned_row)

            if not has_data:
                if header_row_idx is not None:
                    gaps.append(physical_line_num)
                continue

            # First non-empty line serves as column headers
            if header_row_idx is None:
                header_row_idx = physical_line_num
                clean_headers = disambiguate_headers(cleaned_row)
                continue

            # Data rows
            if data_row_count >= max_rows:
                warnings.append(f"CSV document truncated at {max_rows} rows limit.")
                break

            data_row_count += 1
            original_row_indices.append(physical_line_num)

            # Map columns to values
            row_cells_dict: dict[str, Any] = {}
            row_values_list: list[Any] = []

            for col_idx, header_name in enumerate(clean_headers):
                raw_cell = cleaned_row[col_idx] if col_idx < len(cleaned_row) else None
                formatted_val = format_cell_value(raw_cell)
                row_cells_dict[header_name] = formatted_val
                row_values_list.append(formatted_val if formatted_val is not None else "")

            table_body_rows.append(row_values_list)

            if emit_rows:
                row_elem = SpreadsheetRowElement(
                    sheet_name=sheet_name,
                    row_index=physical_line_num,
                    headers=clean_headers,
                    cells=row_cells_dict,
                    row_range=f"Row {physical_line_num}",
                    locator=ChunkLocator(
                        sheet_name=sheet_name,
                        row_range=f"Row {physical_line_num}",
                    ),
                    metadata={
                        "sheet_name": sheet_name,
                        "original_row_number": physical_line_num,
                        "column_count": len(clean_headers),
                    },
                )
                elements.append(row_elem)

        # Emit structured TableElement
        if emit_tables and table_body_rows:
            table_elem = TableElement(
                headers=clean_headers,
                rows=table_body_rows,
                caption=f"Worksheet: {sheet_name}",
                sheet_name=sheet_name,
                row_range=f"Row {header_row_idx}:{physical_line_num}",
                locator=ChunkLocator(
                    sheet_name=sheet_name,
                    row_range=f"Row {header_row_idx}:{physical_line_num}",
                ),
                metadata={
                    "sheet_name": sheet_name,
                    "header_row_index": header_row_idx,
                    "original_row_indices": original_row_indices,
                    "column_headers": clean_headers,
                    "row_count": len(table_body_rows),
                    "col_count": len(clean_headers),
                    "delimiter": delimiter,
                    "gaps": gaps,
                },
            )
            elements.append(table_elem)

        # 6. Check for empty document
        if not elements:
            warnings.append(f"CSV document '{source_filename}' contains no extractable data or rows.")

        # 7. Construct DocumentMetadata
        file_hash = (
            metadata.file_hash_sha256
            if (metadata and getattr(metadata, "file_hash_sha256", None))
            else hashlib.sha256(validate_bytes).hexdigest()
        )
        custom_attrs: dict[str, Any] = {
            "sheet_names": [sheet_name],
            "total_sheets": 1,
            "total_elements": len(elements),
            "delimiter": delimiter,
            "is_empty": len(elements) == 0,
            "total_data_rows": data_row_count,
        }
        if metadata and getattr(metadata, "custom_metadata", None):
            custom_attrs.update(metadata.custom_metadata)

        doc_metadata = DocumentMetadata(
            document_id=context.document_id,
            tenant_id=context.tenant_id,
            filename=metadata.filename if (metadata and getattr(metadata, "filename", None)) else source_filename,
            file_hash_sha256=file_hash,
            file_size_bytes=file_size,
            mime_type=DocumentMimeType.CSV.value,
            document_type=DocumentType.CSV,
            classification=context.classification,
            department_id=context.department_id,
            uploaded_by_id=context.user_id,
            title=(metadata.title if metadata and metadata.title else None) or Path(source_filename).stem,
            total_pages=1,
            custom_metadata=custom_attrs,
            custom_attributes=custom_attrs,
        )

        # 8. Stats & telemetry
        duration = time.perf_counter() - start_time
        stats = ExtractionStats(
            extractor_name=self.name,
            extractor_version=self.version,
            duration_seconds=round(duration, 4),
            total_pages=1,
            total_elements=len(elements),
            total_characters=sum(len(e.text_content) for e in elements),
            ocr_applied=False,
        )

        result = ExtractionResult(
            document_id=context.document_id,
            document_type=DocumentType.CSV,
            mime_type=DocumentMimeType.CSV.value,
            elements=elements,
            metadata=doc_metadata,
            warnings=warnings,
            stats=stats,
        )

        result.compute_canonical_offsets(separator="\n\n")
        result.validate_traceability(strict=True)

        return result

    def _resolve_content(
        self,
        content: bytes | BinaryIO | Path,
    ) -> tuple[bytes | None, Path | None, str]:
        """Normalize content input into raw bytes or file path."""
        if isinstance(content, Path):
            if not content.exists() or not content.is_file():
                raise CorruptDocumentError(f"Target CSV file does not exist: {content}")
            return None, content, content.name

        if isinstance(content, bytes):
            return content, None, "document.csv"

        if hasattr(content, "read"):
            data = content.read()
            if isinstance(data, str):
                data = data.encode("utf-8")
            name = getattr(content, "name", "document.csv")
            return data, None, Path(str(name)).name

        raise UnsupportedDocumentTypeError(
            f"Unsupported content type for CSV extraction: {type(content)}"
        )

    def _decode_text(self, raw_bytes: bytes) -> str:
        """Attempt multi-encoding decode with graceful fallback."""
        for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252", "ascii"):
            try:
                return raw_bytes.decode(enc)
            except UnicodeDecodeError:
                continue
        raise MalformedFileError("Failed to decode CSV content with UTF-8 or Latin-1.")

    def _detect_delimiter(self, sample_text: str) -> str:
        """Detect column delimiter using csv.Sniffer with safe heuristic fallback."""
        try:
            dialect = csv.Sniffer().sniff(sample_text)
            if dialect.delimiter in (",", "\t", ";", "|"):
                return dialect.delimiter
        except Exception:
            pass

        # Fallback to character frequency in first non-empty line
        first_line = sample_text.splitlines()[0] if sample_text.splitlines() else ""
        for delim in (",", "\t", ";", "|"):
            if delim in first_line:
                return delim

        return ","


@register_extractor()
class SpreadsheetExtractor(DocumentExtractor):
    """Composite spreadsheet extractor routing between XLSX and CSV implementations."""

    def __init__(self) -> None:
        self._xlsx_extractor = XlsxExtractor()
        self._csv_extractor = CsvExtractor()

    @property
    def name(self) -> str:
        return "SpreadsheetExtractor"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def supported_document_types(self) -> list[DocumentType]:
        return [DocumentType.XLSX, DocumentType.CSV]

    @property
    def supported_mime_types(self) -> list[str]:
        return self._xlsx_extractor.supported_mime_types + self._csv_extractor.supported_mime_types

    async def extract(
        self,
        content: bytes | BinaryIO | Path,
        context: IngestionContext,
        metadata: DocumentMetadata | None = None,
    ) -> ExtractionResult:
        """Route to appropriate XLSX or CSV extractor based on filename or signature."""
        target_name = ""
        if isinstance(content, Path):
            target_name = content.name.lower()
        elif hasattr(content, "name"):
            target_name = str(getattr(content, "name")).lower()

        if target_name.endswith(".csv"):
            return await self._csv_extractor.extract(content, context, metadata)
        if target_name.endswith((".xlsx", ".xls")):
            return await self._xlsx_extractor.extract(content, context, metadata)

        # Inspect bytes for signature
        if isinstance(content, bytes):
            if any(content.startswith(m) for m in ALL_ZIP_MAGICS):
                return await self._xlsx_extractor.extract(content, context, metadata)
            return await self._csv_extractor.extract(content, context, metadata)

        # Default to XLSX
        return await self._xlsx_extractor.extract(content, context, metadata)


# Backwards-compatible aliases
OpenXMLXlsxExtractor = XlsxExtractor
