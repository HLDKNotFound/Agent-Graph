"""Fixed-size sliding window chunking implementation with 10% overlap and source locator preservation."""

from typing import Any
import logging

from backend.ingestion.chunking.base import ChunkingStrategy
from backend.ingestion.chunking.tokens import (
    compute_chunk_hash,
    decode_tokens,
    generate_chunk_id,
    get_tokenizer,
)
from backend.ingestion.types.chunking import ChunkingConfig, DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.coordinates import ChunkLocator
from backend.ingestion.types.elements import ExtractedElement, HeadingElement
from backend.ingestion.types.enums import ChunkingStrategyType
from backend.ingestion.types.extraction import ExtractionResult

logger = logging.getLogger(__name__)


class FixedSlidingWindowChunker(ChunkingStrategy):
    """Token-based sliding window chunking strategy applying 10% overlap across consecutive chunks.

    Guarantees:
    - Never splits in the middle of subword tokens.
    - Preserves 10% overlap (or configurable percentage) between adjacent windows.
    - Preserves source locator coordinates (page_number, section_heading, char_start, char_end) across boundaries.
    - Enforces configured maximum token limits strictly.
    """

    @property
    def strategy_type(self) -> ChunkingStrategyType:
        return ChunkingStrategyType.FIXED_SLIDING_WINDOW

    async def chunk(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> list[DocumentChunkInput]:
        """Execute sliding window chunking on extraction results."""
        active_config = config or context.options.chunking_config

        # Ensure canonical offsets and raw_text are populated
        if extraction_result.raw_text is None:
            extraction_result.compute_canonical_offsets()

        raw_text = extraction_result.raw_text or ""
        elements = extraction_result.get_elements_in_reading_order()

        # Handle empty document content gracefully
        if not raw_text.strip() and not elements:
            logger.debug(
                "Empty document encountered in sliding window chunker",
                document_id=context.document_id,
            )
            return []

        # Tokenize entire canonical text stream
        enc = get_tokenizer()
        tokens = enc.encode(raw_text, disallowed_special=())
        total_tokens = len(tokens)

        if total_tokens == 0:
            return []

        # Sizing and 10% overlap calculation
        chunk_size = active_config.chunk_size_tokens
        overlap = active_config.get_effective_overlap()
        overlap = max(0, min(overlap, chunk_size - 1))
        step = max(1, chunk_size - overlap)

        # Precompute byte offsets of tokens to map exact char_start and char_end
        byte_chunks = enc.decode_tokens_bytes(tokens)
        cum_bytes = [0]
        for b in byte_chunks:
            cum_bytes.append(cum_bytes[-1] + len(b))
        raw_bytes = raw_text.encode("utf-8")

        chunks: list[DocumentChunkInput] = []
        chunk_index = 0
        start_idx = 0

        while start_idx < total_tokens:
            end_idx = min(start_idx + chunk_size, total_tokens)
            window_tokens = tokens[start_idx:end_idx]

            # Decode text without splitting middle of tokens
            chunk_content = decode_tokens(window_tokens)
            token_count = len(window_tokens)

            # Character boundary resolution within raw_text
            b_start = cum_bytes[start_idx]
            b_end = cum_bytes[end_idx]
            char_start = len(raw_bytes[:b_start].decode("utf-8", errors="ignore"))
            char_end = len(raw_bytes[:b_end].decode("utf-8", errors="ignore"))
            char_start = max(0, min(char_start, len(raw_text)))
            char_end = max(char_start, min(char_end, len(raw_text)))

            # Identify overlapping source elements to inherit provenance locators
            overlapping = self._find_overlapping_elements(elements, char_start, char_end)
            page_number = self._resolve_page_number(overlapping, elements, char_start)
            section_heading = self._resolve_section_heading(overlapping, elements, char_start)
            sheet_name, row_range = self._resolve_spreadsheet_coords(overlapping)

            # Build provenance locator container
            locator = ChunkLocator(
                page_number=page_number,
                section_heading=section_heading,
                sheet_name=sheet_name,
                row_range=row_range,
                char_start=char_start,
                char_end=char_end,
            )

            # Deduplication hash & deterministic UUID
            content_hash = compute_chunk_hash(chunk_content)
            chunk_id = generate_chunk_id(
                tenant_id=context.tenant_id,
                document_id=context.document_id,
                chunk_index=chunk_index,
                content_hash=content_hash,
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
                    "window_start_token": start_idx,
                    "window_end_token": end_idx,
                    "window_size_tokens": chunk_size,
                    "overlap_tokens": overlap,
                    "overlap_ratio": round(overlap / chunk_size, 4) if chunk_size > 0 else 0.0,
                    "overlapping_elements_count": len(overlapping),
                },
            )
            chunks.append(chunk_record)
            chunk_index += 1

            if end_idx >= total_tokens:
                break
            start_idx += step

        logger.info(
            "Sliding window chunking complete",
            document_id=context.document_id,
            total_tokens=total_tokens,
            chunks_created=len(chunks),
            chunk_size=chunk_size,
            overlap_tokens=overlap,
        )
        return chunks

    def _find_overlapping_elements(
        self,
        elements: list[ExtractedElement],
        char_start: int,
        char_end: int,
    ) -> list[ExtractedElement]:
        """Filter extracted elements that intersect with the [char_start, char_end) interval."""
        overlapping: list[ExtractedElement] = []
        for elem in elements:
            if elem.char_start is None or elem.char_end is None:
                continue
            if max(char_start, elem.char_start) < min(char_end, elem.char_end):
                overlapping.append(elem)
        return overlapping

    def _resolve_page_number(
        self,
        overlapping: list[ExtractedElement],
        all_elements: list[ExtractedElement],
        char_start: int,
    ) -> int | None:
        """Find the earliest relevant page number covering this window."""
        for elem in overlapping:
            if elem.page_number is not None:
                return elem.page_number

        # Fallback to nearest preceding element with a page number
        last_known_page = None
        for elem in all_elements:
            if elem.char_end is not None and elem.char_end <= char_start:
                if elem.page_number is not None:
                    last_known_page = elem.page_number
            elif elem.char_start is not None and elem.char_start > char_start:
                break
        return last_known_page

    def _resolve_section_heading(
        self,
        overlapping: list[ExtractedElement],
        all_elements: list[ExtractedElement],
        char_start: int,
    ) -> str | None:
        """Find the active section heading in or immediately preceding this window."""
        # 1. Check if an explicit heading element is in the overlapping slice
        for elem in overlapping:
            if isinstance(elem, HeadingElement):
                return elem.text
            if elem.section_heading:
                return elem.section_heading

        # 2. Trace back to the most recent preceding heading element
        last_heading = None
        for elem in all_elements:
            if elem.char_end is not None and elem.char_end <= char_start:
                if isinstance(elem, HeadingElement):
                    last_heading = elem.text
                elif elem.section_heading:
                    last_heading = elem.section_heading
            elif elem.char_start is not None and elem.char_start > char_start:
                break
        return last_heading

    def _resolve_spreadsheet_coords(
        self,
        overlapping: list[ExtractedElement],
    ) -> tuple[str | None, str | None]:
        """Extract worksheet name and row ranges if elements are from spreadsheets."""
        sheet_name = None
        row_ranges: list[str] = []
        for elem in overlapping:
            if elem.sheet_name and sheet_name is None:
                sheet_name = elem.sheet_name
            if elem.row_range:
                row_ranges.append(elem.row_range)

        if not row_ranges:
            return sheet_name, None

        if len(row_ranges) == 1:
            return sheet_name, row_ranges[0]

        # Combine ranges (e.g. 'Row 2', 'Row 3' -> 'Row 2:3')
        min_row = None
        max_row = None
        for r in row_ranges:
            digits = "".join(c for c in r if c.isdigit() or c == ":")
            if ":" in digits:
                parts = digits.split(":")
                try:
                    s, e = int(parts[0]), int(parts[1])
                    min_row = s if min_row is None else min(min_row, s)
                    max_row = e if max_row is None else max(max_row, e)
                except ValueError:
                    pass
            elif digits:
                try:
                    val = int(digits)
                    min_row = val if min_row is None else min(min_row, val)
                    max_row = val if max_row is None else max(max_row, val)
                except ValueError:
                    pass

        if min_row is not None and max_row is not None:
            return sheet_name, f"Row {min_row}:{max_row}" if min_row != max_row else f"Row {min_row}"
        return sheet_name, row_ranges[0]
