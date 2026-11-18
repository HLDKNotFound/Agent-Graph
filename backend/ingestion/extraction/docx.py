"""Microsoft Word DOCX document extractor adapter leveraging python-docx.

Provides structured DOCX extraction preserving:
- Paragraphs and headings in exact document body order.
- Heading hierarchy (Heading 1-6, Title, Subtitle, and outline levels).
- Association of paragraphs, lists, and tables with nearest section heading and outline hierarchy.
- Tables with row and column structure preserved and Markdown representations.
- Bulleted and numbered lists.
- Exact character offsets relative to canonical text stream.
- Detection of empty, malformed, and password-protected / encrypted documents.
- Normalized ExtractionResult production with rich structural metadata for recursive chunking.
"""

import hashlib
import io
import logging
import re
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from backend.ingestion.config import get_ingestion_settings
from backend.ingestion.exceptions import (
    CorruptDocumentError,
    EmptyFileError,
    EncryptedDocumentError,
    ExtractionError,
    UnsupportedDocumentTypeError,
)
from backend.ingestion.extraction.base import DocumentExtractor
from backend.ingestion.extraction.registry import register_extractor
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.coordinates import ChunkLocator
from backend.ingestion.types.elements import (
    ExtractedElement,
    HeadingElement,
    ListElement,
    ParagraphElement,
    TableElement,
)
from backend.ingestion.types.enums import DocumentMimeType, DocumentType
from backend.ingestion.types.extraction import ExtractionResult, ExtractionStats
from backend.ingestion.types.metadata import DocumentMetadata

logger = logging.getLogger(__name__)

# Bullet and numbering patterns for text-based list item detection
BULLET_PATTERN = re.compile(r"^\s*[\u2022\u2023\u25E6\u2043\u2219\u25AA\u25CF\*\-]\s+(.+)$")
NUMBERED_LIST_PATTERN = re.compile(r"^\s*(\(?\d+[\.\)]|\(?[a-zA-Z][\.\)])\s+(.+)$")
HEADING_STYLE_PATTERN = re.compile(r"^heading\s*(\d+)$", re.IGNORECASE)
MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$")

# Standard OLE Compound Document signature (Word 97-2004 or encrypted OpenXML container)
OLE_MAGIC_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@register_extractor()
class DocxExtractor(DocumentExtractor):
    """Production-grade Microsoft Word DOCX document extractor using python-docx.

    Extracts paragraphs, headings, tables, and lists in document order with full
    source traceability coordinates, section hierarchy tracking, and canonical character offsets.
    """

    def __init__(self) -> None:
        """Initialize DocxExtractor."""
        self._settings = get_ingestion_settings()

    @property
    def name(self) -> str:
        return "DocxExtractor"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def supported_document_types(self) -> list[DocumentType]:
        return [DocumentType.DOCX]

    @property
    def supported_mime_types(self) -> list[str]:
        return [
            DocumentMimeType.DOCX.value,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
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
        """Asynchronously extract structured elements from DOCX content or path."""
        start_time = time.perf_counter()

        # 1. Resolve raw bytes and source filename
        raw_bytes, source_filename = self._resolve_content_bytes(content)
        if len(raw_bytes) == 0:
            raise EmptyFileError("Input DOCX document is empty (0 bytes).")

        # 2. Check for password-protection or corrupted archive before opening
        self._check_file_integrity(raw_bytes, source_filename)

        # 3. Open DOCX document with python-docx
        doc: docx.Document | None = None
        try:
            stream = io.BytesIO(raw_bytes)
            doc = docx.Document(stream)
        except (docx.opc.exceptions.PackageNotFoundError, zipfile.BadZipFile) as e:
            raise CorruptDocumentError(f"Corrupted or invalid DOCX document '{source_filename}': {e}") from e
        except Exception as e:
            err_str = str(e).lower()
            if "encrypted" in err_str or "password" in err_str:
                raise EncryptedDocumentError(
                    f"DOCX document '{source_filename}' is encrypted or password-protected: {e}"
                ) from e
            raise CorruptDocumentError(f"Failed to open DOCX document '{source_filename}': {e}") from e

        # 4. Traverse document body elements in document order
        elements: list[ExtractedElement] = []
        warnings: list[str] = []

        current_section_heading: str | None = None
        heading_stack: dict[int, str] = {}
        pending_list_items: list[str] = []
        pending_list_ordered: bool = False

        def flush_pending_list() -> None:
            """Flush aggregated list items into a ListElement."""
            nonlocal pending_list_items, pending_list_ordered
            if not pending_list_items:
                return

            sec_hierarchy = [heading_stack[k] for k in sorted(heading_stack.keys())]
            sec_path = " > ".join(sec_hierarchy) if sec_hierarchy else None

            list_elem = ListElement(
                items=list(pending_list_items),
                ordered=pending_list_ordered,
                start_index=1,
                section_heading=current_section_heading,
                locator=ChunkLocator(section_heading=current_section_heading),
                metadata={
                    "section_hierarchy": sec_hierarchy,
                    "section_path": sec_path,
                    "item_count": len(pending_list_items),
                },
            )
            elements.append(list_elem)
            pending_list_items = []
            pending_list_ordered = False

        # Iterate over child elements in document body preserving order
        for child in doc.element.body:
            tag = child.tag
            # 4a. Process paragraph XML elements (<w:p>)
            if tag.endswith("p"):
                p = Paragraph(child, doc)
                text = p.text.strip()
                if not text:
                    continue

                # Check if paragraph is a heading
                heading_level = self._detect_heading_level(p)
                if heading_level is not None:
                    flush_pending_list()
                    # Clean heading text
                    clean_heading_text = self._clean_heading_text(text)
                    if not clean_heading_text:
                        continue

                    # Update section hierarchy outline stack
                    heading_stack = {k: v for k, v in heading_stack.items() if k < heading_level}
                    heading_stack[heading_level] = clean_heading_text
                    current_section_heading = clean_heading_text
                    sec_hierarchy = [heading_stack[k] for k in sorted(heading_stack.keys())]
                    sec_path = " > ".join(sec_hierarchy)

                    heading_elem = HeadingElement(
                        text=clean_heading_text,
                        level=heading_level,
                        section_heading=clean_heading_text,
                        locator=ChunkLocator(section_heading=clean_heading_text),
                        metadata={
                            "style": p.style.name if p.style else None,
                            "level": heading_level,
                            "section_hierarchy": sec_hierarchy,
                            "section_path": sec_path,
                        },
                    )
                    elements.append(heading_elem)
                    continue

                # Check if paragraph is a list item
                is_list, is_ordered, item_text = self._detect_list_item(p)
                if is_list:
                    # If list type changed (ordered vs unordered), flush previous list
                    if pending_list_items and pending_list_ordered != is_ordered:
                        flush_pending_list()

                    pending_list_ordered = is_ordered
                    pending_list_items.append(item_text)
                    continue

                # Standard body prose paragraph
                flush_pending_list()
                sec_hierarchy = [heading_stack[k] for k in sorted(heading_stack.keys())]
                sec_path = " > ".join(sec_hierarchy) if sec_hierarchy else None

                is_bold = all(r.bold for r in p.runs) if p.runs else False
                para_elem = ParagraphElement(
                    text=text,
                    section_heading=current_section_heading,
                    locator=ChunkLocator(section_heading=current_section_heading),
                    metadata={
                        "style": p.style.name if p.style else None,
                        "section_hierarchy": sec_hierarchy,
                        "section_path": sec_path,
                        "is_bold": is_bold,
                    },
                )
                elements.append(para_elem)

            # 4b. Process table XML elements (<w:tbl>)
            elif tag.endswith("tbl"):
                flush_pending_list()
                tbl = Table(child, doc)
                table_elem = self._extract_table(
                    tbl=tbl,
                    current_heading=current_section_heading,
                    heading_stack=heading_stack,
                )
                if table_elem is not None:
                    elements.append(table_elem)

        # Flush any trailing list items
        flush_pending_list()

        # 5. Detect empty document
        if not elements:
            warning_msg = f"DOCX document '{source_filename}' contains no extractable text, headings, or tables."
            warnings.append(warning_msg)
            logger.warning(warning_msg)

        # 6. Build enriched DocumentMetadata
        doc_metadata = self._build_metadata(
            doc=doc,
            context=context,
            raw_bytes=raw_bytes,
            metadata=metadata,
            source_filename=source_filename,
            total_elements=len(elements),
        )

        # 7. Compute execution stats
        duration = time.perf_counter() - start_time
        stats = ExtractionStats(
            extractor_name=self.name,
            extractor_version=self.version,
            duration_seconds=round(duration, 4),
            total_pages=None,
            total_elements=len(elements),
            total_characters=sum(len(e.text_content) for e in elements),
            ocr_applied=False,
        )

        # 8. Construct ExtractionResult
        result = ExtractionResult(
            document_id=context.document_id,
            document_type=DocumentType.DOCX,
            mime_type=DocumentMimeType.DOCX.value,
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

    def _resolve_content_bytes(self, content: bytes | BinaryIO | Path) -> tuple[bytes, str]:
        """Normalize content input into raw bytes and determine source filename."""
        if isinstance(content, Path):
            if not content.exists() or not content.is_file():
                raise CorruptDocumentError(f"Target DOCX file does not exist: {content}")
            return content.read_bytes(), content.name

        if isinstance(content, bytes):
            return content, "document.docx"

        if hasattr(content, "read"):
            data = content.read()
            if isinstance(data, str):
                data = data.encode("utf-8")
            name = getattr(content, "name", "document.docx")
            return data, Path(str(name)).name

        raise UnsupportedDocumentTypeError(f"Unsupported content type for DOCX extraction: {type(content)}")

    def _check_file_integrity(self, raw_bytes: bytes, filename: str) -> None:
        """Verify binary signature and check for encrypted or password-protected archives."""
        # 1. Check for OLE Compound Document (often encrypted Word document or legacy .doc)
        if raw_bytes.startswith(OLE_MAGIC_BYTES):
            # Check for EncryptedPackage stream in OLE
            if b"EncryptedPackage" in raw_bytes or b"EncryptionInfo" in raw_bytes:
                raise EncryptedDocumentError(
                    f"DOCX document '{filename}' is password-protected or encrypted and cannot be parsed without credentials."
                )
            raise UnsupportedDocumentTypeError(
                f"Binary legacy Word format (.doc) is not supported for '{filename}'. Please convert to modern DOCX format."
            )

        # 2. Check for standard ZIP magic bytes (PK\x03\x04 or PK\x05\x06)
        if not (raw_bytes.startswith(b"PK\x03\x04") or raw_bytes.startswith(b"PK\x05\x06")):
            raise CorruptDocumentError(
                f"File '{filename}' does not have a valid OpenXML/ZIP header signature."
            )

        # 3. Test opening zip archive to detect truncated or corrupted files early
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                namelist = zf.namelist()
                # A valid DOCX OpenXML archive must contain word/document.xml or [Content_Types].xml
                if "word/document.xml" not in namelist and "[Content_Types].xml" not in namelist:
                    raise CorruptDocumentError(
                        f"Archive '{filename}' is missing essential DOCX parts ('word/document.xml' or '[Content_Types].xml')."
                    )
        except zipfile.BadZipFile as e:
            raise CorruptDocumentError(f"Corrupted ZIP archive for DOCX document '{filename}': {e}") from e

    def _detect_heading_level(self, p: Paragraph) -> int | None:
        """Detect heading level (1 to 6) from style, OpenXML outline level, or structural markers."""
        # Check 1: Paragraph style name
        style_name = (p.style.name or "").strip() if p.style else ""
        if style_name:
            match = HEADING_STYLE_PATTERN.match(style_name)
            if match:
                return min(max(int(match.group(1)), 1), 6)

            low_style = style_name.lower()
            if low_style in ("title", "document title"):
                return 1
            if low_style in ("subtitle", "sub-title"):
                return 2

        # Check 2: Paragraph outline level in OpenXML properties (<w:outlineLvl w:val="0"/>)
        if p._p.pPr is not None:
            outline = p._p.pPr.find(qn("w:outlineLvl"))
            if outline is not None:
                val = outline.get(qn("w:val"))
                if val is not None and val.isdigit():
                    return min(max(int(val) + 1, 1), 6)

        # Check 3: Markdown-like heading syntax in text (e.g. # Title, ## Section)
        text = p.text.strip()
        md_match = MARKDOWN_HEADING_PATTERN.match(text)
        if md_match:
            return min(max(len(md_match.group(1)), 1), 6)

        return None

    def _clean_heading_text(self, text: str) -> str:
        """Clean markdown markers or leading formatting from heading text."""
        md_match = MARKDOWN_HEADING_PATTERN.match(text)
        if md_match:
            return md_match.group(2).strip()
        return text.strip()

    def _detect_list_item(self, p: Paragraph) -> tuple[bool, bool, str]:
        """Detect if paragraph is a list item, whether it is ordered, and return cleaned text.

        Returns:
            Tuple of (is_list, is_ordered, cleaned_text).
        """
        text = p.text.strip()
        style_name = (p.style.name or "").lower() if p.style else ""

        # Check OpenXML numbering property (<w:numPr>)
        has_num_pr = False
        if p._p.pPr is not None and p._p.pPr.find(qn("w:numPr")) is not None:
            has_num_pr = True

        # Check style naming conventions
        if "bullet" in style_name:
            clean_item = self._clean_list_text(text)
            return True, False, clean_item

        if "number" in style_name:
            clean_item = self._clean_list_text(text)
            return True, True, clean_item

        if has_num_pr or "list" in style_name:
            # Determine ordering from text or default to bullet
            num_match = NUMBERED_LIST_PATTERN.match(text)
            if num_match:
                return True, True, num_match.group(2).strip()
            bullet_match = BULLET_PATTERN.match(text)
            if bullet_match:
                return True, False, bullet_match.group(1).strip()
            return True, False, text

        # Check text patterns for explicit bullets or numbering
        bullet_match = BULLET_PATTERN.match(text)
        if bullet_match:
            return True, False, bullet_match.group(1).strip()

        num_match = NUMBERED_LIST_PATTERN.match(text)
        if num_match:
            return True, True, num_match.group(2).strip()

        return False, False, text

    def _clean_list_text(self, text: str) -> str:
        """Strip redundant bullet or numbering prefixes from list item text."""
        bullet_match = BULLET_PATTERN.match(text)
        if bullet_match:
            return bullet_match.group(1).strip()
        num_match = NUMBERED_LIST_PATTERN.match(text)
        if num_match:
            return num_match.group(2).strip()
        return text

    def _extract_table(
        self,
        tbl: Table,
        current_heading: str | None,
        heading_stack: dict[int, str],
    ) -> TableElement | None:
        """Extract table structure into TableElement preserving row and column matrices."""
        if not tbl.rows:
            return None

        # Extract rows
        raw_rows: list[list[str]] = []
        for row in tbl.rows:
            # Strip cell text and normalize internal newlines to spaces
            row_cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            raw_rows.append(row_cells)

        if not raw_rows:
            return None

        # Check if table has any non-empty content
        has_content = any(any(c for c in r) for r in raw_rows)
        if not has_content:
            return None

        # First row as headers, remaining as rows
        headers = raw_rows[0]
        body_rows = raw_rows[1:]

        # Handle column count safely without triggering python-docx merged cell exceptions
        try:
            col_count = len(tbl.columns)
        except Exception:
            col_count = len(headers) if headers else 0

        sec_hierarchy = [heading_stack[k] for k in sorted(heading_stack.keys())]
        sec_path = " > ".join(sec_hierarchy) if sec_hierarchy else None

        table_elem = TableElement(
            headers=headers,
            rows=body_rows,
            section_heading=current_heading,
            locator=ChunkLocator(section_heading=current_heading),
            metadata={
                "row_count": len(tbl.rows),
                "col_count": col_count,
                "section_hierarchy": sec_hierarchy,
                "section_path": sec_path,
            },
        )
        return table_elem

    def _build_metadata(
        self,
        doc: docx.Document,
        context: IngestionContext,
        raw_bytes: bytes,
        metadata: DocumentMetadata | None,
        source_filename: str,
        total_elements: int,
    ) -> DocumentMetadata:
        """Construct normalized DocumentMetadata enriching input metadata with DOCX properties."""
        core_props = doc.core_properties if hasattr(doc, "core_properties") else None
        docx_title = (core_props.title or "").strip() if core_props else ""
        docx_author = (core_props.author or "").strip() if core_props else ""

        effective_title = (
            (metadata.title if metadata and metadata.title else None)
            or (docx_title if docx_title else None)
            or Path(source_filename).stem
        )

        custom_attrs: dict[str, Any] = {}
        if metadata and getattr(metadata, "custom_metadata", None):
            custom_attrs.update(metadata.custom_metadata)
        if metadata and getattr(metadata, "custom_attributes", None):
            custom_attrs.update(metadata.custom_attributes)

        if core_props:
            custom_attrs.update(
                {
                    "author": docx_author or custom_attrs.get("author"),
                    "creator": core_props.author,
                    "created": str(core_props.created) if core_props.created else None,
                    "modified": str(core_props.modified) if core_props.modified else None,
                    "last_modified_by": core_props.last_modified_by,
                    "revision": core_props.revision,
                    "subject": core_props.subject,
                    "category": core_props.category,
                    "keywords": core_props.keywords,
                }
            )

        custom_attrs["total_elements"] = total_elements
        custom_attrs["is_empty"] = total_elements == 0

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
            mime_type=DocumentMimeType.DOCX.value,
            document_type=DocumentType.DOCX,
            classification=context.classification,
            department_id=context.department_id,
            uploaded_by_id=context.user_id,
            author=docx_author or (metadata.author if metadata else None),
            title=effective_title,
            total_pages=None,
            custom_metadata=custom_attrs,
            custom_attributes=custom_attrs,
        )


# Backward-compatible and architectural alias
OpenXMLDocxExtractor = DocxExtractor
