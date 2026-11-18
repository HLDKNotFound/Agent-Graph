"""File signature (magic byte) detection and structural integrity verification for enterprise documents."""

import csv
import io
import zipfile
from typing import BinaryIO

from backend.ingestion.exceptions import (
    CorruptDocumentError,
    FileSignatureMismatchError,
    MalformedFileError,
    UnsupportedDocumentTypeError,
)
from backend.ingestion.types.enums import DocumentMimeType, DocumentType

# Magic byte signatures
PDF_MAGIC = b"%PDF-"
ZIP_MAGIC_LOCAL = b"PK\x03\x04"
ZIP_MAGIC_EMPTY = b"PK\x05\x06"
ZIP_MAGIC_SPANNED = b"PK\x07\x08"
ALL_ZIP_MAGICS = (ZIP_MAGIC_LOCAL, ZIP_MAGIC_EMPTY, ZIP_MAGIC_SPANNED)

# Executable and binary headers to explicitly reject
EXECUTABLE_MAGICS = {
    b"MZ": "Windows PE Executable / DLL",
    b"\x7fELF": "Linux ELF Executable",
    b"\xca\xfe\xba\xbe": "Java / Mach-O Binary",
    b"\xce\xfa\xed\xfe": "Mach-O 32-bit",
    b"\xcf\xfa\xed\xfe": "Mach-O 64-bit",
    b"#!/bin": "Shell Script Executable",
}


def detect_file_format(
    content: bytes,
) -> tuple[DocumentType | None, str | None]:
    """Inspect magic bytes and internal structure to identify document type and confirmed MIME type.

    Args:
        content: Raw binary document bytes.

    Returns:
        Tuple of (detected DocumentType, confirmed MIME string) or (None, None).
    """
    if not content:
        return None, None

    # Check for PDF signature
    if content.startswith(PDF_MAGIC):
        return DocumentType.PDF, DocumentMimeType.PDF.value

    # Check for OpenXML ZIP-based formats (DOCX / XLSX)
    if any(content.startswith(magic) for magic in ALL_ZIP_MAGICS):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                namelist = set(zf.namelist())

                # Check for DOCX internal structure
                is_docx = (
                    "word/document.xml" in namelist
                    or any(name.startswith("word/") for name in namelist)
                )
                if is_docx:
                    return DocumentType.DOCX, DocumentMimeType.DOCX.value

                # Check for XLSX internal structure
                is_xlsx = (
                    "xl/workbook.xml" in namelist
                    or any(name.startswith("xl/") for name in namelist)
                )
                if is_xlsx:
                    return DocumentType.XLSX, DocumentMimeType.XLSX.value

                # Inspect [Content_Types].xml for explicit MIME declaration
                if "[Content_Types].xml" in namelist:
                    try:
                        ct_content = zf.read("[Content_Types].xml").decode("utf-8", errors="ignore")
                        if "wordprocessingml.document" in ct_content:
                            return DocumentType.DOCX, DocumentMimeType.DOCX.value
                        if "spreadsheetml.sheet" in ct_content:
                            return DocumentType.XLSX, DocumentMimeType.XLSX.value
                    except Exception:
                        pass

                # Generic or unsupported ZIP archive
                return None, "application/zip"
        except zipfile.BadZipFile:
            # Corrupted ZIP header/data
            return None, "application/x-corrupt-zip"

    # Check for Executable binaries disguised as documents
    for magic_prefix, desc in EXECUTABLE_MAGICS.items():
        if content.startswith(magic_prefix):
            return None, f"application/x-executable ({desc})"

    # Check for CSV (plain text formatted data)
    # CSV must NOT contain binary null bytes
    if b"\x00" not in content[:8192]:
        try:
            # Attempt to decode as text
            text_sample: str | None = None
            for encoding in ("utf-8", "utf-8-sig", "latin-1", "ascii"):
                try:
                    text_sample = content[:4096].decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

            if text_sample and text_sample.strip():
                # Attempt CSV parsing
                sample_io = io.StringIO(text_sample)
                try:
                    dialect = csv.Sniffer().sniff(text_sample[:1024])
                    delimiter = dialect.delimiter
                except Exception:
                    # Sniffer failed, check for common delimiters manually
                    common_delimiters = [",", "\t", ";", "|"]
                    delimiter = ","
                    for d in common_delimiters:
                        if d in text_sample:
                            delimiter = d
                            break

                sample_io.seek(0)
                reader = csv.reader(sample_io, delimiter=delimiter)
                rows = [row for row in reader if row]
                if rows:
                    return DocumentType.CSV, DocumentMimeType.CSV.value
        except Exception:
            pass

    return None, None


def validate_pdf_integrity(content: bytes) -> None:
    """Validate structural integrity of PDF binary content."""
    if not content.startswith(PDF_MAGIC):
        actual_prefix = content[:8].hex()
        raise FileSignatureMismatchError(
            f"Invalid PDF signature: Expected '%PDF-', got hex '{actual_prefix}'",
            details={"expected": "%PDF-", "actual_prefix_hex": actual_prefix},
        )

    # PDF must have minimal length for header, catalog/page, and EOF
    if len(content) < 32:
        raise MalformedFileError(
            "Corrupted PDF document: File size is too small to contain valid PDF structure",
            details={"file_size_bytes": len(content)},
        )

    # Standard PDFs must contain %%EOF marker near the end
    tail = content[-2048:] if len(content) > 2048 else content
    if b"%%EOF" not in tail:
        raise MalformedFileError(
            "Corrupted PDF document: Missing mandatory '%%EOF' end-of-file trailer",
            details={"file_size_bytes": len(content)},
        )

    # Check for essential PDF structural markers: obj, xref, Root, or Type
    has_pdf_tokens = (
        b"obj" in content
        or b"xref" in content
        or b"/Root" in content
        or b"/Type" in content
        or b"startxref" in content
    )
    if not has_pdf_tokens:
        raise MalformedFileError(
            "Corrupted PDF document: File lacks valid PDF objects, xref tables, or trailer dictionaries",
            details={"file_size_bytes": len(content)},
        )


def validate_docx_integrity(content: bytes) -> None:
    """Validate structural integrity and OpenXML layout of DOCX binary content."""
    if not any(content.startswith(m) for m in ALL_ZIP_MAGICS):
        raise FileSignatureMismatchError(
            "Invalid DOCX signature: Missing ZIP archive magic bytes 'PK\\x03\\x04'",
            details={"actual_prefix_hex": content[:8].hex()},
        )

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            namelist = set(zf.namelist())

            if "[Content_Types].xml" not in namelist:
                raise MalformedFileError(
                    "Corrupted DOCX: Missing OpenXML '[Content_Types].xml' manifest",
                    details={"namelist": list(namelist)[:10]},
                )

            # Check if an XLSX file was renamed to DOCX
            if "xl/workbook.xml" in namelist or any(n.startswith("xl/") for n in namelist):
                raise FileSignatureMismatchError(
                    "Mismatched document type: File contains XLSX spreadsheet structure, not DOCX",
                    details={"detected_format": "XLSX", "expected_format": "DOCX"},
                )

            # Check for word/document.xml
            has_word_doc = (
                "word/document.xml" in namelist
                or any(n.startswith("word/") for n in namelist)
            )
            if not has_word_doc:
                # Check [Content_Types].xml content
                try:
                    ct = zf.read("[Content_Types].xml").decode("utf-8", errors="ignore")
                    if "wordprocessingml" not in ct:
                        raise FileSignatureMismatchError(
                            "Generic ZIP archive uploaded: Missing DOCX OpenXML structure ('word/document.xml')",
                            details={"namelist": list(namelist)[:10]},
                        )
                except Exception:
                    raise FileSignatureMismatchError(
                        "ZIP archive missing DOCX OpenXML structure ('word/document.xml')",
                        details={"namelist": list(namelist)[:10]},
                    )

            # Verify integrity of internal XML entry
            try:
                zf.read("[Content_Types].xml")
            except Exception as e:
                raise MalformedFileError(
                    f"Corrupted DOCX: Failed reading OpenXML internal archive entry: {e}",
                    details={"error": str(e)},
                )
    except zipfile.BadZipFile as e:
        raise MalformedFileError(
            f"Corrupted DOCX document: Malformed or truncated ZIP archive ({e})",
            details={"error": str(e)},
        )


def validate_xlsx_integrity(content: bytes) -> None:
    """Validate structural integrity and OpenXML layout of XLSX binary content."""
    if not any(content.startswith(m) for m in ALL_ZIP_MAGICS):
        raise FileSignatureMismatchError(
            "Invalid XLSX signature: Missing ZIP archive magic bytes 'PK\\x03\\x04'",
            details={"actual_prefix_hex": content[:8].hex()},
        )

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            namelist = set(zf.namelist())

            if "[Content_Types].xml" not in namelist:
                raise MalformedFileError(
                    "Corrupted XLSX: Missing OpenXML '[Content_Types].xml' manifest",
                    details={"namelist": list(namelist)[:10]},
                )

            # Check if a DOCX file was renamed to XLSX
            if "word/document.xml" in namelist or any(n.startswith("word/") for n in namelist):
                raise FileSignatureMismatchError(
                    "Mismatched document type: File contains DOCX document structure, not XLSX",
                    details={"detected_format": "DOCX", "expected_format": "XLSX"},
                )

            # Check for xl/workbook.xml or xl/
            has_xl = (
                "xl/workbook.xml" in namelist
                or any(n.startswith("xl/") for n in namelist)
            )
            if not has_xl:
                try:
                    ct = zf.read("[Content_Types].xml").decode("utf-8", errors="ignore")
                    if "spreadsheetml" not in ct:
                        raise FileSignatureMismatchError(
                            "Generic ZIP archive uploaded: Missing XLSX OpenXML structure ('xl/workbook.xml')",
                            details={"namelist": list(namelist)[:10]},
                        )
                except Exception:
                    raise FileSignatureMismatchError(
                        "ZIP archive missing XLSX OpenXML structure ('xl/workbook.xml')",
                        details={"namelist": list(namelist)[:10]},
                    )

            try:
                zf.read("[Content_Types].xml")
            except Exception as e:
                raise MalformedFileError(
                    f"Corrupted XLSX: Failed reading OpenXML internal archive entry: {e}",
                    details={"error": str(e)},
                )
    except zipfile.BadZipFile as e:
        raise MalformedFileError(
            f"Corrupted XLSX document: Malformed or truncated ZIP archive ({e})",
            details={"error": str(e)},
        )


def validate_csv_integrity(content: bytes) -> None:
    """Validate structural integrity and plain-text tabular format of CSV content."""
    # CSV must never contain null bytes
    if b"\x00" in content:
        raise FileSignatureMismatchError(
            "Invalid CSV content: Binary null bytes detected (possible executable or binary file disguised as CSV)",
            details={"file_size_bytes": len(content)},
        )

    # Disallow executable headers
    for magic_prefix, desc in EXECUTABLE_MAGICS.items():
        if content.startswith(magic_prefix):
            raise FileSignatureMismatchError(
                f"Executable binary header detected ({desc}); rejected as CSV",
                details={"header_desc": desc},
            )

    # Attempt text decoding
    decoded_text: str | None = None
    for enc in ("utf-8", "utf-8-sig", "latin-1", "ascii"):
        try:
            decoded_text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if decoded_text is None:
        raise MalformedFileError(
            "Corrupted CSV: File content could not be decoded as text using UTF-8 or Latin-1",
            details={"file_size_bytes": len(content)},
        )

    # Must contain at least one non-whitespace line
    lines = [line.strip() for line in decoded_text.splitlines() if line.strip()]
    if not lines:
        raise MalformedFileError(
            "Empty or whitespace-only CSV file content",
            details={"line_count": 0},
        )

    # Try parsing sample with csv.reader
    try:
        sample_reader = csv.reader(io.StringIO(decoded_text[:8192]))
        rows = list(sample_reader)
        if not rows:
            raise MalformedFileError(
                "CSV parser could not extract any tabular rows from document",
                details={"line_count": len(lines)},
            )
    except Exception as e:
        raise MalformedFileError(
            f"Corrupted CSV document parsing error: {e}",
            details={"error": str(e)},
        )
