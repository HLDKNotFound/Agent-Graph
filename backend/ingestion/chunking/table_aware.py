"""Semantic table-aware chunking strategy for tabular data (XLSX, CSV) and extracted document tables."""

from typing import Any
import logging

from backend.ingestion.chunking.base import ChunkingStrategy
from backend.ingestion.chunking.recursive import RecursiveStructuralChunker
from backend.ingestion.chunking.tokens import (
    compute_chunk_hash,
    count_tokens,
    generate_chunk_id,
)
from backend.ingestion.types.chunking import ChunkingConfig, DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.coordinates import ChunkLocator, parse_row_range
from backend.ingestion.types.elements import (
    ExtractedElement,
    SpreadsheetRowElement,
    TableElement,
)
from backend.ingestion.types.enums import ChunkingStrategyType
from backend.ingestion.types.extraction import ExtractionResult

logger = logging.getLogger(__name__)


class SemanticTableAwareChunker(ChunkingStrategy):
    """Semantic table-aware chunking engine for enterprise spreadsheets and structured tables.

    Guarantees:
    - Converts tabular matrices (XLSX, CSV, PDF/DOCX tables) into standard GitHub Flavored Markdown tables.
    - Explicitly includes worksheet names and column headers in chunk text.
    - Preserves original 1-based worksheet row numbers and ranges across chunk boundaries.
    - Partitions oversized tables into smaller, retrieval-bounded row groups.
    - Repeats column headers and worksheet identifiers on every generated table chunk.
    - Strictly populates source locator coordinates (sheet_name, row_range, char offsets).
    - Preserves non-tabular content without silent discarding.
    """

    def __init__(self) -> None:
        self._fallback_structural_chunker = RecursiveStructuralChunker()

    @property
    def strategy_type(self) -> ChunkingStrategyType:
        return ChunkingStrategyType.SEMANTIC_TABLE_AWARE

    async def chunk(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> list[DocumentChunkInput]:
        """Execute semantic table-aware chunking on extracted document results."""
        active_config = config or context.options.chunking_config
        chunk_size = active_config.chunk_size_tokens

        # Ensure canonical offsets are populated
        if extraction_result.raw_text is None:
            extraction_result.compute_canonical_offsets()

        elements = extraction_result.get_elements_in_reading_order()

        # Handle empty document gracefully
        if not elements and not (extraction_result.raw_text or "").strip():
            logger.debug(
                "Empty document encountered in table-aware chunker",
                document_id=context.document_id,
            )
            return []

        # 1. Classify tabular vs non-tabular elements
        table_elements: list[TableElement] = []
        spreadsheet_rows_by_sheet: dict[str, list[SpreadsheetRowElement]] = {}
        non_table_elements: list[ExtractedElement] = []

        # Identify sheets that already have TableElement representations to prevent duplication
        sheets_with_tables: set[str] = set()

        for elem in elements:
            if isinstance(elem, TableElement):
                table_elements.append(elem)
                if elem.sheet_name:
                    sheets_with_tables.add(elem.sheet_name)
            elif isinstance(elem, SpreadsheetRowElement):
                sname = elem.sheet_name or "Sheet1"
                if sname not in spreadsheet_rows_by_sheet:
                    spreadsheet_rows_by_sheet[sname] = []
                spreadsheet_rows_by_sheet[sname].append(elem)
            else:
                non_table_elements.append(elem)

        # 2. Chunk explicit TableElement instances
        all_chunks: list[DocumentChunkInput] = []
        chunk_index = 0

        for table in table_elements:
            sheet_chunks = self._chunk_table_element(
                table=table,
                context=context,
                chunk_size=chunk_size,
                start_index=chunk_index,
            )
            all_chunks.extend(sheet_chunks)
            chunk_index += len(sheet_chunks)

        # 3. Chunk standalone SpreadsheetRowElement records (for sheets lacking a TableElement)
        for sname, rows in spreadsheet_rows_by_sheet.items():
            if sname in sheets_with_tables:
                # TableElement already chunked this sheet
                continue
            row_chunks = self._chunk_spreadsheet_rows(
                sheet_name=sname,
                rows=rows,
                context=context,
                chunk_size=chunk_size,
                start_index=chunk_index,
            )
            all_chunks.extend(row_chunks)
            chunk_index += len(row_chunks)

        # 4. Chunk non-table elements (preamble, headings, narrative paragraphs) so content is never discarded
        if non_table_elements:
            fallback_result = ExtractionResult(
                document_id=extraction_result.document_id,
                document_type=extraction_result.document_type,
                mime_type=extraction_result.mime_type,
                elements=non_table_elements,
                metadata=extraction_result.metadata,
                warnings=extraction_result.warnings,
            )
            fallback_chunks = await self._fallback_structural_chunker.chunk(
                extraction_result=fallback_result,
                context=context,
                config=active_config,
            )
            # Re-index fallback chunks to maintain sequential deterministic ordering
            for c in fallback_chunks:
                c.chunk_index = chunk_index
                c.chunk_id = generate_chunk_id(
                    tenant_id=context.tenant_id,
                    document_id=context.document_id,
                    chunk_index=chunk_index,
                    content_hash=c.content_hash,
                )
                all_chunks.append(c)
                chunk_index += 1

        logger.info(
            "Semantic table-aware chunking complete",
            document_id=context.document_id,
            total_chunks=len(all_chunks),
            table_count=len(table_elements),
            spreadsheet_sheet_count=len(spreadsheet_rows_by_sheet),
        )
        return all_chunks

    def _chunk_table_element(
        self,
        table: TableElement,
        context: IngestionContext,
        chunk_size: int,
        start_index: int,
    ) -> list[DocumentChunkInput]:
        """Split a TableElement into token-bounded Markdown table chunks with repeated headers."""
        chunks: list[DocumentChunkInput] = []
        sheet_name = table.sheet_name or "Sheet1"
        headers = [str(h).strip().replace("\n", " ") for h in (table.headers or [])]
        rows = table.rows or []

        if not rows:
            # Table with only headers or empty
            empty_table_md = self._render_markdown_table(sheet_name, headers, [], "1")
            content_hash = compute_chunk_hash(empty_table_md)
            chunk_id = generate_chunk_id(context.tenant_id, context.document_id, start_index, content_hash)
            locator = ChunkLocator(
                page_number=table.page_number,
                section_heading=f"Worksheet: {sheet_name}",
                sheet_name=sheet_name,
                row_range=table.row_range or "Row 1",
                char_start=table.char_start,
                char_end=table.char_end,
            )
            return [
                DocumentChunkInput(
                    chunk_id=chunk_id,
                    tenant_id=context.tenant_id,
                    document_id=context.document_id,
                    document_version_id=context.version_id,
                    chunk_index=start_index,
                    content=empty_table_md,
                    content_hash=content_hash,
                    token_count=count_tokens(empty_table_md),
                    locator=locator,
                    acl=context.to_acl_payload(),
                    metadata={
                        "strategy": self.strategy_type.value,
                        "sheet_name": sheet_name,
                        "row_range": table.row_range or "Row 1",
                        "column_headers": headers,
                        "row_count": 0,
                    },
                )
            ]

        # Extract original 1-based row indices
        meta = table.metadata or {}
        orig_indices = meta.get("original_row_indices")
        if not orig_indices or len(orig_indices) != len(rows):
            start_row = 2
            if table.row_range:
                parsed_s, _ = parse_row_range(table.row_range)
                if parsed_s is not None:
                    start_row = parsed_s + 1
            orig_indices = [start_row + i for i in range(len(rows))]

        # Group rows into chunks bounded by chunk_size
        chunk_index = start_index
        row_cursor = 0
        total_rows = len(rows)

        while row_cursor < total_rows:
            group_rows: list[list[Any]] = []
            group_indices: list[int] = []

            # Always add at least the first available row
            group_rows.append(rows[row_cursor])
            group_indices.append(orig_indices[row_cursor])
            row_cursor += 1

            # Greedily accumulate subsequent rows while within chunk_size
            while row_cursor < total_rows:
                candidate_rows = group_rows + [rows[row_cursor]]
                candidate_indices = group_indices + [orig_indices[row_cursor]]
                range_label = self._format_row_range(candidate_indices[0], candidate_indices[-1])
                candidate_md = self._render_markdown_table(sheet_name, headers, candidate_rows, range_label)

                if count_tokens(candidate_md) <= chunk_size:
                    group_rows.append(rows[row_cursor])
                    group_indices.append(orig_indices[row_cursor])
                    row_cursor += 1
                else:
                    break

            # Render chunk markdown table with repeated column headers
            start_row_num = group_indices[0]
            end_row_num = group_indices[-1]
            row_range_str = self._format_row_range(start_row_num, end_row_num)
            chunk_content = self._render_markdown_table(sheet_name, headers, group_rows, row_range_str)

            token_count = count_tokens(chunk_content)
            content_hash = compute_chunk_hash(chunk_content)
            chunk_id = generate_chunk_id(context.tenant_id, context.document_id, chunk_index, content_hash)

            locator = ChunkLocator(
                page_number=table.page_number,
                section_heading=f"Worksheet: {sheet_name}",
                sheet_name=sheet_name,
                row_range=row_range_str,
                char_start=table.char_start,
                char_end=table.char_end,
            )

            chunk_record = DocumentChunkInput(
                chunk_id=chunk_id,
                tenant_id=context.tenant_id,
                document_id=context.document_id,
                document_version_id=context.version_id,
                chunk_index=chunk_index,
                content=chunk_content,
                content_hash=content_hash,
                token_count=token_count,
                locator=locator,
                acl=context.to_acl_payload(),
                metadata={
                    "strategy": self.strategy_type.value,
                    "sheet_name": sheet_name,
                    "row_range": row_range_str,
                    "start_row": start_row_num,
                    "end_row": end_row_num,
                    "row_count": len(group_rows),
                    "column_headers": headers,
                    "original_row_indices": group_indices,
                },
            )
            chunks.append(chunk_record)
            chunk_index += 1

        return chunks

    def _chunk_spreadsheet_rows(
        self,
        sheet_name: str,
        rows: list[SpreadsheetRowElement],
        context: IngestionContext,
        chunk_size: int,
        start_index: int,
    ) -> list[DocumentChunkInput]:
        """Aggregate standalone SpreadsheetRowElements into table chunks with repeated headers."""
        if not rows:
            return []

        # Derive headers from first row
        first_row = rows[0]
        headers = first_row.headers if first_row.headers else list(first_row.cells.keys())

        # Construct rows matrix and indices
        row_matrices: list[list[Any]] = []
        row_indices: list[int] = []
        char_starts: list[int] = []
        char_ends: list[int] = []

        for r in rows:
            row_matrices.append([r.cells.get(h, "") for h in headers])
            row_indices.append(r.row_index)
            if r.char_start is not None:
                char_starts.append(r.char_start)
            if r.char_end is not None:
                char_ends.append(r.char_end)

        dummy_table = TableElement(
            headers=headers,
            rows=row_matrices,
            sheet_name=sheet_name,
            row_range=f"Row {row_indices[0]}:{row_indices[-1]}" if len(row_indices) > 1 else f"Row {row_indices[0]}",
            char_start=min(char_starts) if char_starts else None,
            char_end=max(char_ends) if char_ends else None,
            metadata={"original_row_indices": row_indices},
        )
        return self._chunk_table_element(dummy_table, context, chunk_size, start_index)

    def _render_markdown_table(
        self,
        sheet_name: str,
        headers: list[str],
        rows: list[list[Any]],
        row_range_str: str,
    ) -> str:
        """Render markdown representation with worksheet title, repeated column headers, and data rows."""
        lines: list[str] = [
            f"### Worksheet: {sheet_name} ({row_range_str})\n",
        ]

        if not headers and rows:
            headers = [f"Column {i + 1}" for i in range(len(rows[0]))]

        if headers:
            header_line = " | ".join(str(h).strip().replace("\n", " ") for h in headers)
            sep_line = " | ".join(["---"] * len(headers))
            lines.append(f"| {header_line} |")
            lines.append(f"| {sep_line} |")

        for row in rows:
            cells = [str(cell).strip().replace("\n", " ") if cell is not None else "" for cell in row]
            # Pad row if fewer cells than headers
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            row_line = " | ".join(cells)
            lines.append(f"| {row_line} |")

        return "\n".join(lines)

    def _format_row_range(self, start_row: int, end_row: int) -> str:
        """Format 1-based row interval string (e.g. 'Row 2:25' or 'Row 2')."""
        if start_row == end_row:
            return f"Row {start_row}"
        return f"Row {start_row}:{end_row}"


# Backward compatibility and alias
TableAwareChunker = SemanticTableAwareChunker
