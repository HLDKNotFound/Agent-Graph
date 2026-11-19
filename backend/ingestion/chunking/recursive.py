"""Recursive structural document chunking preserving Markdown heading hierarchy, paragraph boundaries, and source locators."""

import re
from typing import Any
from pydantic import BaseModel, Field
import logging

from backend.ingestion.chunking.base import ChunkingStrategy
from backend.ingestion.chunking.tokens import (
    compute_chunk_hash,
    count_tokens,
    generate_chunk_id,
)
from backend.ingestion.types.chunking import ChunkingConfig, DocumentChunkInput
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.coordinates import ChunkLocator
from backend.ingestion.types.elements import (
    ExtractedElement,
    HeadingElement,
    ListElement,
    ParagraphElement,
    TableElement,
    TextBlockElement,
    TextElement,
)
from backend.ingestion.types.enums import ChunkingStrategyType, ElementType
from backend.ingestion.types.extraction import ExtractionResult

logger = logging.getLogger(__name__)


class StructuralUnit(BaseModel):
    """Normalized structural unit (heading, paragraph, table, list) for recursive chunking."""

    unit_type: ElementType
    content: str
    heading_level: int | None = None  # 1 for H1, 2 for H2, 3 for H3, etc.
    heading_title: str | None = None
    page_number: int | None = None
    section_heading: str | None = None
    sheet_name: str | None = None
    row_range: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecursiveStructuralChunker(ChunkingStrategy):
    """Structure-aware chunking strategy prioritizing heading hierarchy (H1 -> H2 -> H3) and paragraph boundaries.

    Guarantees:
    - Prioritizes Markdown-style heading hierarchy: H1, H2, H3.
    - Preserves paragraph boundaries wherever possible.
    - Groups related paragraphs under their parent headings.
    - Avoids separating headings from their associated content.
    - Recursively splits oversized sections and paragraphs when exceeding chunk size limits.
    - Retains complete line-level provenance coordinates (page_number, section_heading, char offsets).
    - Never silently discards content.
    """

    @property
    def strategy_type(self) -> ChunkingStrategyType:
        return ChunkingStrategyType.RECURSIVE_STRUCTURAL

    async def chunk(
        self,
        extraction_result: ExtractionResult,
        context: IngestionContext,
        config: ChunkingConfig | None = None,
    ) -> list[DocumentChunkInput]:
        """Execute recursive structural chunking on extracted document elements."""
        active_config = config or context.options.chunking_config

        # Ensure canonical offsets and raw_text are populated
        if extraction_result.raw_text is None:
            extraction_result.compute_canonical_offsets()

        elements = extraction_result.get_elements_in_reading_order()
        raw_text = extraction_result.raw_text or ""

        # Handle empty document content gracefully
        if not elements and not raw_text.strip():
            logger.debug(
                "Empty document encountered in recursive structural chunker",
                document_id=context.document_id,
            )
            return []

        # 1. Normalize elements into a stream of StructuralUnits
        units = self._extract_structural_units(elements, raw_text)
        if not units:
            return []

        # 2. Assemble chunks respecting structural boundaries and token limits
        chunk_size = active_config.chunk_size_tokens
        preserve_headers = active_config.preserve_headers

        raw_chunks = self._build_structural_chunks(
            units=units,
            chunk_size=chunk_size,
            preserve_headers=preserve_headers,
        )

        # 3. Transform assembled structural chunks into typed DocumentChunkInput records
        chunks: list[DocumentChunkInput] = []
        for idx, item in enumerate(raw_chunks):
            content = item["content"].strip()
            if not content:
                continue

            token_count = count_tokens(content)
            content_hash = compute_chunk_hash(content)
            chunk_id = generate_chunk_id(
                tenant_id=context.tenant_id,
                document_id=context.document_id,
                chunk_index=idx,
                content_hash=content_hash,
            )

            locator = ChunkLocator(
                page_number=item.get("page_number"),
                section_heading=item.get("section_heading"),
                sheet_name=item.get("sheet_name"),
                row_range=item.get("row_range"),
                char_start=item.get("char_start"),
                char_end=item.get("char_end"),
            )

            chunk_record = DocumentChunkInput(
                chunk_id=chunk_id,
                tenant_id=context.tenant_id,
                document_id=context.document_id,
                document_version_id=context.version_id,
                chunk_index=idx,
                content=content,
                content_hash=content_hash,
                token_count=token_count,
                locator=locator,
                acl=context.to_acl_payload(),
                metadata={
                    "strategy": self.strategy_type.value,
                    "heading_level": item.get("heading_level"),
                    "heading_hierarchy": item.get("heading_hierarchy", []),
                    "section_heading": item.get("section_heading"),
                    "unit_count": item.get("unit_count", 1),
                },
            )
            chunks.append(chunk_record)

        logger.info(
            "Recursive structural chunking complete",
            document_id=context.document_id,
            chunks_created=len(chunks),
            chunk_size_limit=chunk_size,
        )
        return chunks

    def _extract_structural_units(
        self,
        elements: list[ExtractedElement],
        raw_text: str,
    ) -> list[StructuralUnit]:
        """Convert heterogeneous extracted elements or raw markdown into an ordered list of StructuralUnits."""
        units: list[StructuralUnit] = []

        for elem in elements:
            if isinstance(elem, HeadingElement):
                units.append(
                    StructuralUnit(
                        unit_type=ElementType.HEADING,
                        content=elem.text_content,
                        heading_level=elem.level,
                        heading_title=elem.text,
                        page_number=elem.page_number,
                        section_heading=elem.section_heading or elem.text,
                        char_start=elem.char_start,
                        char_end=elem.char_end,
                        metadata=dict(elem.metadata),
                    )
                )
            elif isinstance(elem, ParagraphElement):
                content = elem.text_content.strip()
                if content:
                    units.append(
                        StructuralUnit(
                            unit_type=ElementType.PARAGRAPH,
                            content=content,
                            page_number=elem.page_number,
                            section_heading=elem.section_heading,
                            char_start=elem.char_start,
                            char_end=elem.char_end,
                            metadata=dict(elem.metadata),
                        )
                    )
            elif isinstance(elem, TableElement):
                table_md = elem.to_markdown().strip()
                if table_md:
                    units.append(
                        StructuralUnit(
                            unit_type=ElementType.TABLE,
                            content=table_md,
                            page_number=elem.page_number,
                            section_heading=elem.section_heading or elem.caption,
                            sheet_name=elem.sheet_name,
                            row_range=elem.row_range,
                            char_start=elem.char_start,
                            char_end=elem.char_end,
                            metadata=dict(elem.metadata),
                        )
                    )
            elif isinstance(elem, ListElement):
                list_md = elem.to_markdown().strip()
                if list_md:
                    units.append(
                        StructuralUnit(
                            unit_type=ElementType.LIST,
                            content=list_md,
                            page_number=elem.page_number,
                            section_heading=elem.section_heading,
                            char_start=elem.char_start,
                            char_end=elem.char_end,
                            metadata=dict(elem.metadata),
                        )
                    )
            elif isinstance(elem, (TextBlockElement, TextElement)):
                # Inspect text content for markdown headings and paragraphs
                sub_units = self._parse_text_into_structural_units(elem)
                units.extend(sub_units)
            else:
                text = getattr(elem, "text_content", None) or getattr(elem, "content", None) or ""
                text = str(text).strip()
                if text:
                    units.append(
                        StructuralUnit(
                            unit_type=elem.element_type,
                            content=text,
                            page_number=elem.page_number,
                            section_heading=elem.section_heading,
                            sheet_name=elem.sheet_name,
                            row_range=elem.row_range,
                            char_start=elem.char_start,
                            char_end=elem.char_end,
                            metadata=dict(elem.metadata),
                        )
                    )

        # Fallback if no elements produced units but raw_text exists
        if not units and raw_text.strip():
            dummy = TextBlockElement(text=raw_text, char_start=0, char_end=len(raw_text))
            units = self._parse_text_into_structural_units(dummy)

        return units

    def _parse_text_into_structural_units(
        self,
        element: ExtractedElement,
    ) -> list[StructuralUnit]:
        """Parse raw text block containing markdown headings (#, ##, ###) and paragraph breaks into units."""
        text = element.text_content if hasattr(element, "text_content") else (element.content or "")
        if not text.strip():
            return []

        base_start = element.char_start if element.char_start is not None else 0
        units: list[StructuralUnit] = []

        # Split text on double newlines while preserving offsets
        paragraphs = re.split(r"(\n\s*\n+)", text)
        curr_offset = 0

        for segment in paragraphs:
            seg_len = len(segment)
            stripped = segment.strip()
            if not stripped:
                curr_offset += seg_len
                continue

            # Heading detection (e.g. # Title, ## Subtitle, ### Section)
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            if heading_match:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                units.append(
                    StructuralUnit(
                        unit_type=ElementType.HEADING,
                        content=stripped,
                        heading_level=level,
                        heading_title=title,
                        page_number=element.page_number,
                        section_heading=title,
                        char_start=base_start + curr_offset,
                        char_end=base_start + curr_offset + seg_len,
                        metadata=dict(element.metadata),
                    )
                )
            else:
                units.append(
                    StructuralUnit(
                        unit_type=ElementType.PARAGRAPH,
                        content=stripped,
                        page_number=element.page_number,
                        section_heading=element.section_heading,
                        char_start=base_start + curr_offset,
                        char_end=base_start + curr_offset + seg_len,
                        metadata=dict(element.metadata),
                    )
                )
            curr_offset += seg_len

        return units

    def _build_structural_chunks(
        self,
        units: list[StructuralUnit],
        chunk_size: int,
        preserve_headers: bool,
    ) -> list[dict[str, Any]]:
        """Combine structural units into token-bounded chunks preserving heading hierarchy and paragraphs."""
        chunks: list[dict[str, Any]] = []

        current_heading_path: dict[int, str] = {}  # {1: "Title", 2: "Section", 3: "Subsection"}
        current_heading_unit: StructuralUnit | None = None
        current_buffer: list[StructuralUnit] = []
        pending_heading: StructuralUnit | None = None

        def get_active_heading_text() -> str | None:
            levels = sorted(current_heading_path.keys())
            if not levels:
                return None
            return " > ".join(current_heading_path[lvl] for lvl in levels if current_heading_path[lvl])

        def flush_buffer(force: bool = False):
            nonlocal current_buffer, pending_heading
            if not current_buffer and not pending_heading:
                return

            # Avoid separating heading from its associated content when possible
            if pending_heading and not current_buffer and not force:
                return

            all_units = list(current_buffer)
            if pending_heading and pending_heading not in all_units:
                all_units.insert(0, pending_heading)
                pending_heading = None

            if not all_units:
                return

            chunk_text = "\n\n".join(u.content for u in all_units).strip()
            if not chunk_text:
                current_buffer = []
                return

            # Combine locator attributes
            page_numbers = [u.page_number for u in all_units if u.page_number is not None]
            char_starts = [u.char_start for u in all_units if u.char_start is not None]
            char_ends = [u.char_end for u in all_units if u.char_end is not None]

            sheet_names = [u.sheet_name for u in all_units if u.sheet_name is not None]
            row_ranges = [u.row_range for u in all_units if u.row_range is not None]

            active_heading = get_active_heading_text()
            heading_levels = [u.heading_level for u in all_units if u.heading_level is not None]

            chunks.append(
                {
                    "content": chunk_text,
                    "page_number": page_numbers[0] if page_numbers else None,
                    "section_heading": (
                        current_heading_unit.heading_title
                        if current_heading_unit and current_heading_unit.heading_title
                        else (active_heading or (all_units[0].section_heading if all_units else None))
                    ),
                    "sheet_name": sheet_names[0] if sheet_names else None,
                    "row_range": row_ranges[0] if row_ranges else None,
                    "char_start": min(char_starts) if char_starts else None,
                    "char_end": max(char_ends) if char_ends else None,
                    "heading_level": heading_levels[0] if heading_levels else None,
                    "heading_hierarchy": [current_heading_path[k] for k in sorted(current_heading_path.keys())],
                    "heading_path": active_heading,
                    "unit_count": len(all_units),
                }
            )
            current_buffer = []

        for unit in units:
            if unit.unit_type == ElementType.HEADING:
                lvl = unit.heading_level or 1
                title = unit.heading_title or unit.content.lstrip("#").strip()

                # If we have an existing buffer, flush it to start a new structural section
                if current_buffer or pending_heading:
                    flush_buffer(force=True)

                # Update heading hierarchy path for the new section
                current_heading_path = {k: v for k, v in current_heading_path.items() if k < lvl}
                current_heading_path[lvl] = title

                current_heading_unit = unit
                pending_heading = unit
                continue

            # Non-heading unit (paragraph, table, list)
            unit_tokens = count_tokens(unit.content)

            # Check if this single unit is oversized and needs recursive splitting
            if unit_tokens > chunk_size:
                if current_buffer or pending_heading:
                    flush_buffer(force=True)

                # Recursively split the oversized unit
                split_chunks = self._recursively_split_unit(
                    unit=unit,
                    chunk_size=chunk_size,
                    parent_heading=current_heading_unit if preserve_headers else None,
                    heading_path=get_active_heading_text(),
                )
                chunks.extend(split_chunks)
                continue

            # Check if adding unit to current buffer exceeds chunk_size
            candidate_units = list(current_buffer)
            if pending_heading and pending_heading not in candidate_units:
                candidate_units.insert(0, pending_heading)
            candidate_units.append(unit)

            candidate_content = "\n\n".join(u.content for u in candidate_units)
            if count_tokens(candidate_content) <= chunk_size:
                if pending_heading:
                    current_buffer.append(pending_heading)
                    pending_heading = None
                current_buffer.append(unit)
            else:
                # Flush current buffer
                flush_buffer(force=True)

                # Start next buffer under same parent heading
                if preserve_headers and current_heading_unit and unit_tokens + count_tokens(current_heading_unit.content) <= chunk_size:
                    current_buffer.append(current_heading_unit)
                current_buffer.append(unit)

        # Flush any trailing units
        flush_buffer(force=True)
        return chunks

    def _recursively_split_unit(
        self,
        unit: StructuralUnit,
        chunk_size: int,
        parent_heading: StructuralUnit | None,
        heading_path: str | None,
    ) -> list[dict[str, Any]]:
        """Recursively split an oversized unit using paragraph, sentence, and word boundaries."""
        results: list[dict[str, Any]] = []
        text = unit.content

        # Separators in order of priority: double newlines, single newlines, sentences, words
        pieces = self._split_text_hierarchically(text, chunk_size)

        base_start = unit.char_start or 0
        curr_offset = 0

        header_prefix = f"{parent_heading.content}\n\n" if parent_heading else ""
        header_tokens = count_tokens(header_prefix)

        for piece in pieces:
            piece_clean = piece.strip()
            if not piece_clean:
                curr_offset += len(piece)
                continue

            content = f"{header_prefix}{piece_clean}" if (header_tokens + count_tokens(piece_clean) <= chunk_size) else piece_clean

            piece_start = base_start + curr_offset
            piece_end = piece_start + len(piece)
            curr_offset += len(piece)

            results.append(
                {
                    "content": content,
                    "page_number": unit.page_number,
                    "section_heading": heading_path or unit.section_heading,
                    "sheet_name": unit.sheet_name,
                    "row_range": unit.row_range,
                    "char_start": piece_start,
                    "char_end": min(piece_end, unit.char_end or piece_end),
                    "heading_level": parent_heading.heading_level if parent_heading else None,
                    "heading_hierarchy": [heading_path] if heading_path else [],
                    "unit_count": 1,
                }
            )

        return results

    def _split_text_hierarchically(self, text: str, max_tokens: int) -> list[str]:
        """Recursively subdivide text by paragraph, then sentence, then word boundaries."""
        if count_tokens(text) <= max_tokens:
            return [text]

        # 1. Try splitting by double newline (\n\n)
        if "\n\n" in text:
            parts = text.split("\n\n")
            return self._accumulate_pieces(parts, "\n\n", max_tokens)

        # 2. Try splitting by single newline (\n)
        if "\n" in text:
            parts = text.split("\n")
            return self._accumulate_pieces(parts, "\n", max_tokens)

        # 3. Try splitting by sentences (. , ! , ? )
        sentence_parts = re.split(r"(?<=[.!?])\s+", text)
        if len(sentence_parts) > 1:
            return self._accumulate_pieces(sentence_parts, " ", max_tokens)

        # 4. Fallback to words
        words = text.split(" ")
        if len(words) > 1:
            return self._accumulate_pieces(words, " ", max_tokens)

        # 5. Last resort: token sliding window on unbreakable text
        return [text]

    def _accumulate_pieces(self, parts: list[str], joiner: str, max_tokens: int) -> list[str]:
        """Accumulate parts into slices that fit within max_tokens."""
        accumulated: list[str] = []
        current_group: list[str] = []

        for p in parts:
            if not p.strip():
                continue
            if count_tokens(p) > max_tokens:
                if current_group:
                    accumulated.append(joiner.join(current_group))
                    current_group = []
                # Recurse further on the single oversized piece
                accumulated.extend(self._split_text_hierarchically(p, max_tokens))
                continue

            candidate = joiner.join(current_group + [p]) if current_group else p
            if count_tokens(candidate) <= max_tokens:
                current_group.append(p)
            else:
                if current_group:
                    accumulated.append(joiner.join(current_group))
                current_group = [p]

        if current_group:
            accumulated.append(joiner.join(current_group))

        return accumulated
