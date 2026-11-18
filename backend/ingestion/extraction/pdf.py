"""Native PDF document extractor adapter leveraging PyMuPDF.

Provides high-performance digital PDF extraction preserving:
- 1-based page numbers and spatial bounding boxes.
- Multi-column and complex reading order layout reconstruction.
- Paragraph boundaries and list structures.
- Heading hierarchy (H1-H6) derived from font metrics and structural patterns.
- Table detection and extraction into TableElement representations.
- Scanned / image-only page detection with OCR requirement warnings.
- Graceful handling of encrypted, corrupted, and empty documents with resource cleanup.
"""

import hashlib
import io
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, BinaryIO

import pymupdf

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.exceptions import (
    CorruptDocumentError,
    EmptyFileError,
    EncryptedDocumentError,
    ExtractionError,
    UnsupportedDocumentTypeError,
)
from backend.ingestion.extraction.base import DocumentExtractor
from backend.ingestion.extraction.ocr import (
    ScannedPageDetector,
    get_ocr_engine,
    render_pdf_page_to_image,
    sort_and_classify_ocr_blocks,
)
from backend.ingestion.extraction.registry import register_extractor
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.coordinates import BoundingBox, ChunkLocator
from backend.ingestion.types.elements import (
    ExtractedElement,
    HeadingElement,
    ListElement,
    PageElement,
    ParagraphElement,
    TableElement,
    TextBlockElement,
)
from backend.ingestion.types.enums import DocumentClassification, DocumentType
from backend.ingestion.types.extraction import ExtractionResult, ExtractionStats
from backend.ingestion.types.metadata import AccessControlPayload, DocumentMetadata

logger = logging.getLogger(__name__)

# Bullet and numbering patterns
BULLET_PATTERN = re.compile(r"^\s*[\u2022\u2023\u25E6\u2043\u2219\u25AA\u25CF\*\-]\s+(.+)$")
NUMBERED_LIST_PATTERN = re.compile(r"^\s*(\(?\d+[\.\)]|\(?[a-zA-Z][\.\)])\s+(.+)$")
SECTION_HEADING_PATTERN = re.compile(
    r"^(?:(?:\d+\.)+\d*|[A-Z]\.|\b(?:Section|Chapter|Article|Appendix)\s+\d+)\s+[A-Z]",
    re.IGNORECASE,
)


@register_extractor()
class NativePDFExtractor(DocumentExtractor):
    """Production-grade native PDF extractor using PyMuPDF (fitz).

    Extracts text, headings, tables, and lists page by page with full source
    traceability coordinates, intelligent multi-column layout sorting, and scanned-page detection.
    """

    def __init__(
        self,
        scanned_threshold_chars: int | None = None,
        heading_size_ratio: float = 1.15,
        enable_ocr: bool = False,
        ocr_dpi: int | None = None,
        ocr_engine: str | None = None,
        ocr_languages: str | None = None,
    ) -> None:
        """Initialize NativePDFExtractor.

        Args:
            scanned_threshold_chars: Minimum character count below which a page is flagged as potential scanned.
            heading_size_ratio: Ratio above body font size required to identify a heading.
            enable_ocr: Whether to execute OCR on detected scanned pages (default: False).
            ocr_dpi: DPI resolution for PDF page image rasterization (default from settings: 300).
            ocr_engine: Specific OCR engine name ('rapidocr' or 'tesseract').
            ocr_languages: OCR language identifier (e.g. 'eng+vie').
        """
        settings = get_ingestion_settings()
        self.scanned_threshold_chars = (
            scanned_threshold_chars
            if scanned_threshold_chars is not None
            else settings.ocr_scanned_char_threshold
        )
        self.heading_size_ratio = heading_size_ratio
        self.enable_ocr = enable_ocr
        self.ocr_dpi = ocr_dpi if ocr_dpi is not None else settings.ocr_dpi
        self.ocr_engine = ocr_engine or settings.ocr_engine
        self.ocr_languages = ocr_languages or settings.ocr_languages
        self._detector = ScannedPageDetector(
            scanned_threshold_chars=self.scanned_threshold_chars,
            image_coverage_threshold=settings.ocr_image_coverage_threshold,
        )

    @property
    def name(self) -> str:
        return "NativePDFExtractor"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def supported_document_types(self) -> list[DocumentType]:
        return [DocumentType.PDF]

    @property
    def supported_mime_types(self) -> list[str]:
        return ["application/pdf"]

    @property
    def supports_ocr(self) -> bool:
        return False

    async def extract(
        self,
        content: bytes | BinaryIO | Path,
        context: IngestionContext,
        metadata: DocumentMetadata | None = None,
    ) -> ExtractionResult:
        """Asynchronously extract structured elements from PDF content or path."""
        start_time = time.perf_counter()

        # Input bytes resolution and early validation
        raw_bytes, source_filename = self._resolve_content_bytes(content)
        if len(raw_bytes) == 0:
            raise EmptyFileError("Input PDF document is empty (0 bytes).")

        # Open PDF with PyMuPDF and ensure resources are cleaned up deterministically
        doc: pymupdf.Document | None = None
        try:
            try:
                doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
            except pymupdf.FileDataError as e:
                raise CorruptDocumentError(f"Corrupted or invalid PDF document: {e}") from e
            except Exception as e:
                raise CorruptDocumentError(f"Failed to open PDF document: {e}") from e

            # Security: Password-protected or encrypted check
            if doc.is_encrypted and doc.needs_pass:
                raise EncryptedDocumentError(
                    f"PDF document '{source_filename}' is encrypted or password-protected and cannot be parsed without credentials."
                )

            # Integrity: Empty document with 0 pages
            if doc.page_count == 0:
                raise CorruptDocumentError(f"PDF document '{source_filename}' contains zero pages or is malformed.")

            # Compute dominant body font size across document
            body_font_size = self._compute_dominant_font_size(doc)

            elements: list[ExtractedElement] = []
            warnings: list[str] = []
            potential_scanned_pages: list[int] = []
            ocr_pages: list[int] = []
            current_section_heading: str | None = None
            effective_enable_ocr = self.enable_ocr or context.options.enable_ocr

            # Process document page by page
            for page_idx in range(doc.page_count):
                page_num = page_idx + 1  # 1-based page numbering
                page = doc[page_idx]

                page_elements, next_heading, is_scanned_page, char_count = self._extract_page(
                    page=page,
                    page_num=page_num,
                    current_heading=current_section_heading,
                    body_font_size=body_font_size,
                )

                if is_scanned_page:
                    potential_scanned_pages.append(page_num)

                    if effective_enable_ocr:
                        # Apply OCR only to pages that require it
                        try:
                            engine = get_ocr_engine(
                                engine_name=self.ocr_engine,
                                languages=self.ocr_languages,
                            )
                            # Render PDF page to image with configurable DPI
                            img_array, scale_x, scale_y = render_pdf_page_to_image(
                                page=page,
                                dpi=self.ocr_dpi,
                            )
                            # Run offline OCR inference
                            ocr_blocks = engine.extract_text(
                                image=img_array,
                                page_num=page_num,
                                scale_x=scale_x,
                                scale_y=scale_y,
                            )
                            if ocr_blocks:
                                ocr_elements, next_ocr_heading = sort_and_classify_ocr_blocks(
                                    blocks=ocr_blocks,
                                    page_num=page_num,
                                    current_heading=current_section_heading,
                                    page_width=page.rect.width,
                                    page_height=page.rect.height,
                                    engine_name=engine.name,
                                )
                                if next_ocr_heading:
                                    current_section_heading = next_ocr_heading
                                page_elements = ocr_elements
                                ocr_pages.append(page_num)
                        except Exception as ocr_err:
                            # Handle OCR failures on individual pages without failing entire document
                            warning_msg = f"Page {page_num}: OCR extraction failed: {ocr_err}"
                            warnings.append(warning_msg)
                            logger.warning(warning_msg, exc_info=True)
                            fallback_elem = PageElement(
                                page_number=page_num,
                                width=page.rect.width,
                                height=page.rect.height,
                                is_scanned=True,
                                content=f"[Scanned Page {page_num}: OCR extraction error: {ocr_err}]",
                                metadata={
                                    "potential_scanned": True,
                                    "ocr_failed": True,
                                    "error": str(ocr_err),
                                },
                                locator=ChunkLocator(
                                    page_number=page_num,
                                    section_heading=current_section_heading,
                                    bounding_box=BoundingBox(
                                        x0=0.0,
                                        y0=0.0,
                                        x1=page.rect.width,
                                        y1=page.rect.height,
                                        page_number=page_num,
                                    ),
                                ),
                            )
                            page_elements = [fallback_elem]
                    else:
                        warning_msg = (
                            f"Page {page_num}: Little or no extractable text detected ({char_count} chars). "
                            "Flagged as potential scanned or image-only page requiring OCR."
                        )
                        warnings.append(warning_msg)
                        logger.warning(warning_msg)

                if next_heading and not (is_scanned_page and effective_enable_ocr and ocr_pages and ocr_pages[-1] == page_num):
                    current_section_heading = next_heading

                elements.extend(page_elements)

            # Build enriched DocumentMetadata
            doc_metadata = self._build_metadata(
                doc=doc,
                context=context,
                raw_bytes=raw_bytes,
                metadata=metadata,
                source_filename=source_filename,
                potential_scanned_pages=potential_scanned_pages,
                ocr_pages=ocr_pages,
            )

            # Compute execution telemetry
            duration = time.perf_counter() - start_time
            stats = ExtractionStats(
                extractor_name=self.name,
                extractor_version=self.version,
                duration_seconds=round(duration, 4),
                total_pages=doc.page_count,
                total_elements=len(elements),
                total_characters=sum(len(e.text_content) for e in elements),
                ocr_applied=len(ocr_pages) > 0,
            )

            result = ExtractionResult(
                document_id=context.document_id,
                document_type=DocumentType.PDF,
                mime_type="application/pdf",
                elements=elements,
                metadata=doc_metadata,
                warnings=warnings,
                stats=stats,
            )

            # Preserve sequential reading order and populate exact char offsets
            result.compute_canonical_offsets(separator="\n\n")

            # Validate source traceability coordinates
            result.validate_traceability(strict=True)

            return result

        finally:
            # Deterministic resource release
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    def _resolve_content_bytes(self, content: bytes | BinaryIO | Path) -> tuple[bytes, str]:
        """Normalize content input into raw bytes and determine source filename."""
        if isinstance(content, Path):
            if not content.exists() or not content.is_file():
                raise CorruptDocumentError(f"Target PDF file does not exist: {content}")
            return content.read_bytes(), content.name

        if isinstance(content, bytes):
            return content, "document.pdf"

        if hasattr(content, "read"):
            data = content.read()
            if isinstance(data, str):
                data = data.encode("utf-8")
            name = getattr(content, "name", "document.pdf")
            return data, Path(str(name)).name

        raise UnsupportedDocumentTypeError(f"Unsupported content type for PDF extraction: {type(content)}")

    def _compute_dominant_font_size(self, doc: pymupdf.Document) -> float:
        """Compute the dominant body font size across the document based on character frequency."""
        font_size_counts: Counter[float] = Counter()
        # Sample up to first 10 pages for font size distribution
        sample_pages = min(doc.page_count, 10)
        for idx in range(sample_pages):
            try:
                page_dict = doc[idx].get_text("dict")
                for block in page_dict.get("blocks", []):
                    if block.get("type") == 0:  # Text block
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                text = span.get("text", "").strip()
                                size = round(span.get("size", 10.0), 1)
                                if text:
                                    font_size_counts[size] += len(text)
            except Exception:
                continue

        if font_size_counts:
            # Most common font size weighted by character length
            dominant_size, _ = font_size_counts.most_common(1)[0]
            return dominant_size
        return 10.0

    def _extract_page(
        self,
        page: pymupdf.Page,
        page_num: int,
        current_heading: str | None,
        body_font_size: float,
    ) -> tuple[list[ExtractedElement], str | None, bool, int]:
        """Extract all structured elements from a single page in reading order."""
        page_rect = page.rect
        page_width = page_rect.width
        page_height = page_rect.height

        # 1. Extract and detect tables on page first
        tables, table_bboxes = self._detect_tables(page, page_num, current_heading)

        # 2. Extract raw text dict from PyMuPDF
        try:
            page_dict = page.get_text("dict")
            raw_blocks = page_dict.get("blocks", [])
        except Exception as e:
            logger.warning("Failed to extract text dict on page %d: %s", page_num, e)
            raw_blocks = []

        # 3. Filter text blocks and exclude blocks fully inside table bounding boxes
        text_blocks: list[dict[str, Any]] = []
        total_page_chars = 0
        has_images = False

        for b in raw_blocks:
            b_type = b.get("type", 0)
            if b_type == 1:
                has_images = True
                continue
            if b_type == 0:
                bbox = b.get("bbox", (0, 0, 0, 0))
                # Check if block falls inside an extracted table
                if self._is_inside_table(bbox, table_bboxes):
                    continue

                # Reconstruct block text
                block_text_parts: list[str] = []
                for line in b.get("lines", []):
                    line_spans = [s.get("text", "") for s in line.get("spans", [])]
                    line_text = "".join(line_spans).strip()
                    if line_text:
                        block_text_parts.append(line_text)

                block_text = "\n".join(block_text_parts).strip()
                if block_text:
                    total_page_chars += len(block_text)
                    b["_clean_text"] = block_text
                    b["_clean_lines"] = block_text_parts
                    text_blocks.append(b)

        # 4. Check for scanned / image-only page using ScannedPageDetector
        is_scanned, scan_reason, scan_metrics = self._detector.detect(
            page=page,
            digital_char_count=total_page_chars,
            has_tables=bool(tables),
        )

        # If page has virtually no text, emit PageElement so page is recorded
        if total_page_chars == 0 and not tables:
            empty_page_elem = PageElement(
                page_number=page_num,
                width=page_width,
                height=page_height,
                is_scanned=True,
                content=f"[Scanned/Image Page {page_num}: No extractable digital text detected]",
                metadata={
                    "potential_scanned": True,
                    "char_count": 0,
                    "has_images": has_images,
                },
                locator=ChunkLocator(
                    page_number=page_num,
                    section_heading=current_heading,
                    bounding_box=BoundingBox(
                        x0=0.0,
                        y0=0.0,
                        x1=page_width,
                        y1=page_height,
                        page_number=page_num,
                    ),
                ),
            )
            return [empty_page_elem], current_heading, True, 0

        # 5. Multi-column intelligent layout sorting
        sorted_blocks = self._sort_blocks_reading_order(text_blocks, page_width)

        # 6. Merge tables and text blocks in vertical/reading order
        # Combine items with their vertical top coordinate (y0)
        page_items: list[tuple[float, str, Any]] = []

        for tb in tables:
            bbox = tb.locator.bounding_box
            y0 = bbox.y0 if bbox else 0.0
            page_items.append((y0, "table", tb))

        for b in sorted_blocks:
            bbox = b.get("bbox", (0, 0, 0, 0))
            page_items.append((bbox[1], "block", b))

        # Stable sort preserving multi-column block ordering when interleaving tables
        # Sort items by vertical position while preserving block relative order within bands
        sorted_page_items = self._merge_tables_and_blocks(tables, sorted_blocks)

        elements: list[ExtractedElement] = []
        heading_tracker = current_heading

        for item_type, item in sorted_page_items:
            if item_type == "table":
                # Assign latest heading to table if not already set
                if heading_tracker and not item.section_heading:
                    object.__setattr__(item, "section_heading", heading_tracker)
                elements.append(item)
            elif item_type == "block":
                elem, new_heading = self._classify_block(
                    block=item,
                    page_num=page_num,
                    current_heading=heading_tracker,
                    body_font_size=body_font_size,
                    is_scanned=is_scanned,
                )
                if new_heading:
                    heading_tracker = new_heading
                if elem is not None:
                    elements.append(elem)

        return elements, heading_tracker, is_scanned, total_page_chars

    def _sort_blocks_reading_order(
        self,
        blocks: list[dict[str, Any]],
        page_width: float,
    ) -> list[dict[str, Any]]:
        """Intelligently sort blocks on a page to reconstruct true multi-column reading order.

        Preserves:
        - Full-width title / header banners at the top spanning columns.
        - True left-to-right column reading order (Column 1 top-to-bottom, then Column 2 top-to-bottom).
        - Full-width footnotes / footers at the bottom.
        - Single-column flow where parallel columns are not present.
        """
        if len(blocks) <= 1:
            return blocks

        # 1. Detect pairs of blocks running side-by-side (vertical overlap + horizontal separation)
        side_by_side: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                b1, b2 = blocks[i], blocks[j]
                box1, box2 = b1.get("bbox", (0, 0, 0, 0)), b2.get("bbox", (0, 0, 0, 0))
                # Vertical overlap
                v_overlap = min(box1[3], box2[3]) - max(box1[1], box2[1])
                # Horizontal separation with gutter
                h_separated = (box1[2] <= box2[0] + 15) or (box2[2] <= box1[0] + 15)
                if v_overlap > 5 and h_separated:
                    side_by_side.append((b1, b2))

        # If no side-by-side blocks exist on the page, layout is standard single-column
        if not side_by_side:
            return sorted(blocks, key=lambda b: (round(b.get("bbox", (0, 0, 0, 0))[1], 1), b.get("bbox", (0, 0, 0, 0))[0]))

        # 2. Compute vertical bounds of the multi-column zone
        mc_y0 = min(min(b1.get("bbox", (0, 0, 0, 0))[1], b2.get("bbox", (0, 0, 0, 0))[1]) for b1, b2 in side_by_side)
        mc_y1 = max(max(b1.get("bbox", (0, 0, 0, 0))[3], b2.get("bbox", (0, 0, 0, 0))[3]) for b1, b2 in side_by_side)

        top_blocks = [b for b in blocks if b.get("bbox", (0, 0, 0, 0))[3] <= mc_y0 + 10]
        bottom_blocks = [b for b in blocks if b.get("bbox", (0, 0, 0, 0))[1] >= mc_y1 - 10]
        mc_blocks = [b for b in blocks if b not in top_blocks and b not in bottom_blocks]

        # 3. Partition multi-column blocks into left and right columns
        split_x = page_width / 2.0
        best_gap = -1.0
        for ratio in [0.5, 0.45, 0.55, 0.4, 0.6, 0.35, 0.65, 0.33, 0.67]:
            cand_x = page_width * ratio
            left = [b for b in mc_blocks if b.get("bbox", (0, 0, 0, 0))[2] <= cand_x + 10]
            right = [b for b in mc_blocks if b.get("bbox", (0, 0, 0, 0))[0] >= cand_x - 10]
            if len(left) > 0 and len(right) > 0 and (len(left) + len(right) == len(mc_blocks)):
                gap = min(b.get("bbox", (0, 0, 0, 0))[0] for b in right) - max(b.get("bbox", (0, 0, 0, 0))[2] for b in left)
                if gap > best_gap:
                    best_gap = gap
                    split_x = cand_x

        left_col = sorted([b for b in mc_blocks if b.get("bbox", (0, 0, 0, 0))[0] < split_x], key=lambda b: (round(b.get("bbox", (0, 0, 0, 0))[1], 1), b.get("bbox", (0, 0, 0, 0))[0]))
        right_col = sorted([b for b in mc_blocks if b.get("bbox", (0, 0, 0, 0))[0] >= split_x], key=lambda b: (round(b.get("bbox", (0, 0, 0, 0))[1], 1), b.get("bbox", (0, 0, 0, 0))[0]))

        sorted_top = sorted(top_blocks, key=lambda b: (round(b.get("bbox", (0, 0, 0, 0))[1], 1), b.get("bbox", (0, 0, 0, 0))[0]))
        sorted_bottom = sorted(bottom_blocks, key=lambda b: (round(b.get("bbox", (0, 0, 0, 0))[1], 1), b.get("bbox", (0, 0, 0, 0))[0]))

        return sorted_top + left_col + right_col + sorted_bottom

    def _merge_tables_and_blocks(
        self,
        tables: list[TableElement],
        sorted_blocks: list[dict[str, Any]],
    ) -> list[tuple[str, Any]]:
        """Merge tables and sorted text blocks preserving reading order."""
        if not tables:
            return [("block", b) for b in sorted_blocks]
        if not sorted_blocks:
            return [("table", t) for t in tables]

        merged: list[tuple[str, Any]] = []
        table_idx = 0
        sorted_tables = sorted(
            tables,
            key=lambda t: t.locator.bounding_box.y0 if t.locator and t.locator.bounding_box else 0.0,
        )

        for b in sorted_blocks:
            b_y0 = b.get("bbox", (0, 0, 0, 0))[1]
            while table_idx < len(sorted_tables):
                t = sorted_tables[table_idx]
                t_y0 = t.locator.bounding_box.y0 if t.locator and t.locator.bounding_box else 0.0
                if t_y0 <= b_y0:
                    merged.append(("table", t))
                    table_idx += 1
                else:
                    break
            merged.append(("block", b))

        while table_idx < len(sorted_tables):
            merged.append(("table", sorted_tables[table_idx]))
            table_idx += 1

        return merged

    def _classify_block(
        self,
        block: dict[str, Any],
        page_num: int,
        current_heading: str | None,
        body_font_size: float,
        is_scanned: bool,
    ) -> tuple[ExtractedElement | None, str | None]:
        """Classify a text block into HeadingElement, ListElement, ParagraphElement, or TextBlockElement."""
        text = block.get("_clean_text", "").strip()
        lines = block.get("_clean_lines", [])
        if not text:
            return None, None

        bbox_coords = block.get("bbox", (0.0, 0.0, 0.0, 0.0))
        bbox = BoundingBox(
            x0=round(bbox_coords[0], 2),
            y0=round(bbox_coords[1], 2),
            x1=round(bbox_coords[2], 2),
            y1=round(bbox_coords[3], 2),
            page_number=page_num,
        )

        # Compute block font characteristics
        char_weights = 0
        size_sum = 0.0
        is_bold = False

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = span.get("text", "")
                if span_text:
                    char_count = len(span_text)
                    char_weights += char_count
                    size_sum += span.get("size", 10.0) * char_count
                    flags = span.get("flags", 0)
                    font_name = span.get("font", "").lower()
                    if (flags & 2 != 0) or ("bold" in font_name) or ("black" in font_name):
                        is_bold = True

        avg_font_size = round(size_sum / max(char_weights, 1), 1)
        font_ratio = avg_font_size / max(body_font_size, 1.0)

        element_metadata: dict[str, Any] = {
            "font_size": avg_font_size,
            "is_bold": is_bold,
            "potential_scanned": is_scanned,
        }

        # Check 1: Section Heading
        # A block is a heading if font size is significantly larger, or short bold title, or section number
        is_short = len(text) < 180 and len(lines) <= 2
        is_heading_size = font_ratio >= self.heading_size_ratio
        matches_section_regex = bool(SECTION_HEADING_PATTERN.match(text))

        if is_short and (is_heading_size or (is_bold and font_ratio >= 1.0) or matches_section_regex):
            level = self._determine_heading_level(font_ratio, text)
            heading_text = text.lstrip("#").strip()
            heading_elem = HeadingElement(
                text=heading_text,
                level=level,
                page_number=page_num,
                section_heading=heading_text,
                locator=ChunkLocator(
                    page_number=page_num,
                    section_heading=heading_text,
                    bounding_box=bbox,
                ),
                metadata=element_metadata,
            )
            return heading_elem, heading_text

        # Check 2: List Element
        # Check if lines in block match bullet or numbered list patterns
        list_items: list[str] = []
        is_numbered = False
        first_num = 1

        for line in lines:
            bullet_match = BULLET_PATTERN.match(line)
            if bullet_match:
                list_items.append(bullet_match.group(1).strip())
                continue

            num_match = NUMBERED_LIST_PATTERN.match(line)
            if num_match:
                is_numbered = True
                list_items.append(num_match.group(2).strip())
                continue

        # If majority of lines are bullet/numbered items
        if len(list_items) >= 2 and len(list_items) >= len(lines) * 0.7:
            list_elem = ListElement(
                items=list_items,
                ordered=is_numbered,
                start_index=first_num,
                page_number=page_num,
                section_heading=current_heading,
                locator=ChunkLocator(
                    page_number=page_num,
                    section_heading=current_heading,
                    bounding_box=bbox,
                ),
                metadata=element_metadata,
            )
            return list_elem, None

        # Check 3: Narrative Paragraph (default for body prose)
        para_elem = ParagraphElement(
            text=text,
            page_number=page_num,
            section_heading=current_heading,
            locator=ChunkLocator(
                page_number=page_num,
                section_heading=current_heading,
                bounding_box=bbox,
            ),
            metadata=element_metadata,
        )
        return para_elem, None

    def _determine_heading_level(self, font_ratio: float, text: str) -> int:
        """Determine heading hierarchical depth level (1 to 6)."""
        # Section numbers like "1.2.3" indicate level
        num_match = re.match(r"^(\d+(?:\.\d+)*)", text)
        if num_match:
            depth = len(num_match.group(1).split("."))
            return min(max(depth, 1), 6)

        # Fallback to font size ratio hierarchy
        if font_ratio >= 1.6:
            return 1
        if font_ratio >= 1.35:
            return 2
        if font_ratio >= 1.2:
            return 3
        return 4

    def _detect_tables(
        self,
        page: pymupdf.Page,
        page_num: int,
        current_heading: str | None,
    ) -> tuple[list[TableElement], list[tuple[float, float, float, float]]]:
        """Detect and extract structured tables from page using PyMuPDF's TableFinder."""
        table_elements: list[TableElement] = []
        table_bboxes: list[tuple[float, float, float, float]] = []

        try:
            tab_finder = page.find_tables()
            if not tab_finder or not hasattr(tab_finder, "tables"):
                return [], []

            for tab in tab_finder.tables:
                extracted = tab.extract()
                if not extracted or len(extracted) < 1:
                    continue

                bbox_coords = tab.bbox
                bbox = BoundingBox(
                    x0=round(bbox_coords[0], 2),
                    y0=round(bbox_coords[1], 2),
                    x1=round(bbox_coords[2], 2),
                    y1=round(bbox_coords[3], 2),
                    page_number=page_num,
                )
                table_bboxes.append(bbox_coords)

                # First row as headers, remaining rows as body
                headers = [str(col).strip() if col is not None else "" for col in extracted[0]]
                # Clean body cells
                rows = [
                    [str(cell).strip() if cell is not None else "" for cell in r]
                    for r in extracted[1:]
                ]

                # Only include valid tables with content
                if any(h for h in headers) or any(any(c for c in r) for r in rows):
                    table_elem = TableElement(
                        headers=headers,
                        rows=rows,
                        page_number=page_num,
                        section_heading=current_heading,
                        locator=ChunkLocator(
                            page_number=page_num,
                            section_heading=current_heading,
                            bounding_box=bbox,
                        ),
                        metadata={"row_count": len(rows), "col_count": len(headers)},
                    )
                    table_elements.append(table_elem)

        except Exception as e:
            logger.debug("Table detection skipped on page %d: %s", page_num, e)

        return table_elements, table_bboxes

    def _is_inside_table(
        self,
        bbox: tuple[float, float, float, float],
        table_bboxes: list[tuple[float, float, float, float]],
    ) -> bool:
        """Check if a text block's bounding box falls entirely or predominantly inside any table."""
        bx0, by0, bx1, by1 = bbox
        for tx0, ty0, tx1, ty1 in table_bboxes:
            # If block center is inside table bbox
            cx = (bx0 + bx1) / 2.0
            cy = (by0 + by1) / 2.0
            if tx0 <= cx <= tx1 and ty0 <= cy <= ty1:
                return True
        return False

    def _build_metadata(
        self,
        doc: pymupdf.Document,
        context: IngestionContext,
        raw_bytes: bytes,
        metadata: DocumentMetadata | None,
        source_filename: str,
        potential_scanned_pages: list[int],
        ocr_pages: list[int] | None = None,
    ) -> DocumentMetadata:
        """Construct normalized DocumentMetadata enriching input metadata with PDF document attributes."""
        raw_meta = doc.metadata or {}
        pdf_title = (raw_meta.get("title") or "").strip()
        pdf_author = (raw_meta.get("author") or "").strip()

        effective_title = (
            (metadata.title if metadata and metadata.title else None)
            or (pdf_title if pdf_title else None)
            or Path(source_filename).stem
        )

        custom_attrs: dict[str, Any] = {}
        if metadata and getattr(metadata, "custom_metadata", None):
            custom_attrs.update(metadata.custom_metadata)
        if metadata and getattr(metadata, "custom_attributes", None):
            custom_attrs.update(metadata.custom_attributes)

        custom_attrs.update(
            {
                "page_count": doc.page_count,
                "author": pdf_author or custom_attrs.get("author"),
                "creator": raw_meta.get("creator"),
                "producer": raw_meta.get("producer"),
                "creation_date": raw_meta.get("creationDate"),
                "mod_date": raw_meta.get("modDate"),
                "potential_scanned_pages": potential_scanned_pages,
                "has_scanned_pages": len(potential_scanned_pages) > 0,
                "ocr_pages": ocr_pages or [],
                "ocr_applied": bool(ocr_pages),
            }
        )

        file_hash = (
            metadata.file_hash_sha256
            if (metadata and getattr(metadata, "file_hash_sha256", None))
            else hashlib.sha256(raw_bytes).hexdigest()
        )
        file_size = (
            metadata.file_size_bytes
            if (metadata and getattr(metadata, "file_size_bytes", None) is not None)
            else len(raw_bytes)
        )
        fname = (
            metadata.filename
            if (metadata and getattr(metadata, "filename", None))
            else source_filename
        )

        return DocumentMetadata(
            document_id=context.document_id,
            tenant_id=context.tenant_id,
            filename=fname,
            file_hash_sha256=file_hash,
            file_size_bytes=file_size,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
            classification=context.classification,
            department_id=context.department_id,
            uploaded_by_id=context.user_id,
            author=pdf_author or (metadata.author if metadata else None),
            title=effective_title,
            total_pages=doc.page_count,
            custom_metadata=custom_attrs,
            custom_attributes=custom_attrs,
        )


@register_extractor()
class ScannedPDFExtractor(NativePDFExtractor):
    """Adaptive and OCR-capable PDF extractor supporting both native digital and scanned PDF pages.

    Reuses the PDF extraction interface from Task 04. Automatically detects scanned and
    image-only pages, rasterizes them at configurable DPI, runs offline OCR (RapidOCR or Tesseract),
    and preserves line-level provenance coordinates, confidence metrics, and reading order.
    """

    def __init__(
        self,
        scanned_threshold_chars: int | None = None,
        heading_size_ratio: float = 1.15,
        ocr_dpi: int | None = None,
        ocr_engine: str | None = None,
        ocr_languages: str | None = None,
    ) -> None:
        """Initialize ScannedPDFExtractor with OCR enabled by default."""
        super().__init__(
            scanned_threshold_chars=scanned_threshold_chars,
            heading_size_ratio=heading_size_ratio,
            enable_ocr=True,
            ocr_dpi=ocr_dpi,
            ocr_engine=ocr_engine,
            ocr_languages=ocr_languages,
        )

    @property
    def name(self) -> str:
        return "ScannedPDFExtractor"

    @property
    def supports_ocr(self) -> bool:
        return True

