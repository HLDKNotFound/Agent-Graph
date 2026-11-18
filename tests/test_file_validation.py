"""Unit tests for File Validation Layer and File Signature Detection."""

import asyncio
import io
import zipfile
import pytest

from backend.ingestion.config import IngestionSettings
from backend.ingestion.exceptions import (
    EmptyFileError,
    FileSizeExceededError,
    FileSignatureMismatchError,
    MalformedFileError,
    PathTraversalError,
    UnsupportedDocumentTypeError,
    ValidationError,
)
from backend.ingestion.types.context import IngestionContext
from backend.ingestion.types.enums import DocumentMimeType, DocumentType
from backend.ingestion.validation import (
    DefaultDocumentValidator,
    detect_file_format,
    sanitize_filename,
    validate_safe_path,
)


# Helper fixtures for valid documents
def create_valid_pdf_bytes() -> bytes:
    """Generate a minimal syntactically valid PDF binary."""
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<< /Type /Catalog /Pages 2 0 R >>\n"
        b"endobj\n"
        b"2 0 obj\n"
        b"<< /Type /Pages /Kids [] /Count 0 >>\n"
        b"endobj\n"
        b"xref\n"
        b"0 3\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"trailer\n"
        b"<< /Size 3 /Root 1 0 R >>\n"
        b"startxref\n"
        b"115\n"
        b"%%EOF\n"
    )
    return pdf


def create_valid_docx_bytes() -> bytes:
    """Generate a valid OpenXML DOCX archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Enterprise Employee Handbook</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return buffer.getvalue()


def create_valid_xlsx_bytes() -> bytes:
    """Generate a valid OpenXML XLSX archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Salaries" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
    return buffer.getvalue()


def create_valid_csv_bytes() -> bytes:
    """Generate valid CSV text bytes."""
    return b"department_id,name,headcount,budget\nHR,Human Resources,12,120000\nENG,Engineering,45,650000\n"


def create_context(tenant_id: str = "tenant_enterprise_01") -> IngestionContext:
    """Helper to instantiate multi-tenant IngestionContext."""
    return IngestionContext(
        tenant_id=tenant_id,
        document_id="00000000-0000-0000-0000-000000000001",
        version_id="00000000-0000-0000-0000-000000000002",
        roles=["HR_SPECIALIST"],
    )


# ==========================================
# TEST SUITE: FILENAME SANITIZATION & PATH TRAVERSAL
# ==========================================

class TestFilenameSanitization:
    """Test path traversal prevention, dangerous character stripping, and reserved names."""

    def test_valid_filenames(self) -> None:
        assert sanitize_filename("annual_report_2026.pdf") == "annual_report_2026.pdf"
        assert sanitize_filename("Employee Handbook (Final).docx") == "Employee_Handbook__Final_.docx"
        assert sanitize_filename("salary-q3.xlsx") == "salary-q3.xlsx"
        assert sanitize_filename("metrics.csv") == "metrics.csv"

    def test_path_traversal_relative_blocked(self) -> None:
        with pytest.raises(PathTraversalError, match="Path traversal sequence"):
            sanitize_filename("../../etc/passwd")

        with pytest.raises(PathTraversalError, match="Path traversal sequence"):
            sanitize_filename("..\\..\\windows\\win.ini")

        with pytest.raises(PathTraversalError, match="Path traversal sequence"):
            sanitize_filename("folder/../../secret.pdf")

    def test_path_traversal_absolute_blocked(self) -> None:
        with pytest.raises(PathTraversalError, match="Path traversal sequence"):
            sanitize_filename("/etc/shadow")

        with pytest.raises(PathTraversalError, match="Path traversal sequence"):
            sanitize_filename("\\windows\\system32\\cmd.exe")

    def test_url_encoded_traversal_blocked(self) -> None:
        with pytest.raises(PathTraversalError, match="URL-encoded path traversal"):
            sanitize_filename("%2e%2e%2fetc%2fpasswd")

    def test_null_byte_injection_blocked(self) -> None:
        with pytest.raises(PathTraversalError, match="Null byte"):
            sanitize_filename("legit_doc.pdf\x00.exe")

    def test_windows_reserved_names_blocked(self) -> None:
        with pytest.raises(ValidationError, match="reserved system device name"):
            sanitize_filename("CON.pdf")

        with pytest.raises(ValidationError, match="reserved system device name"):
            sanitize_filename("aux.docx")

        with pytest.raises(ValidationError, match="reserved system device name"):
            sanitize_filename("NUL.xlsx")

        with pytest.raises(ValidationError, match="reserved system device name"):
            sanitize_filename("com1.csv")

    def test_empty_or_whitespace_filename_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot be empty"):
            sanitize_filename("")

        with pytest.raises(ValidationError, match="cannot be blank"):
            sanitize_filename("   ")

    def test_leading_dots_stripped(self) -> None:
        # Avoid creating hidden files
        sanitized = sanitize_filename(".hidden_policy.pdf")
        assert sanitized == "hidden_policy.pdf"

    def test_max_filename_length_enforcement(self) -> None:
        long_name = "a" * 300 + ".pdf"
        sanitized = sanitize_filename(long_name, max_length=50)
        assert len(sanitized) <= 50
        assert sanitized.endswith(".pdf")


# ==========================================
# TEST SUITE: MAGIC BYTES & FORMAT DETECTION
# ==========================================

class TestFormatAndSignatureDetection:
    """Test magic bytes inspection and structure verification across formats."""

    def test_detect_valid_pdf(self) -> None:
        pdf_bytes = create_valid_pdf_bytes()
        doc_type, mime = detect_file_format(pdf_bytes)
        assert doc_type == DocumentType.PDF
        assert mime == DocumentMimeType.PDF.value

    def test_detect_valid_docx(self) -> None:
        docx_bytes = create_valid_docx_bytes()
        doc_type, mime = detect_file_format(docx_bytes)
        assert doc_type == DocumentType.DOCX
        assert mime == DocumentMimeType.DOCX.value

    def test_detect_valid_xlsx(self) -> None:
        xlsx_bytes = create_valid_xlsx_bytes()
        doc_type, mime = detect_file_format(xlsx_bytes)
        assert doc_type == DocumentType.XLSX
        assert mime == DocumentMimeType.XLSX.value

    def test_detect_valid_csv(self) -> None:
        csv_bytes = create_valid_csv_bytes()
        doc_type, mime = detect_file_format(csv_bytes)
        assert doc_type == DocumentType.CSV
        assert mime == DocumentMimeType.CSV.value

    def test_detect_executable_binary_rejected(self) -> None:
        # Windows MZ header
        exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
        doc_type, mime = detect_file_format(exe_bytes)
        assert doc_type is None
        assert "executable" in (mime or "").lower()

    def test_detect_generic_zip_rejected(self) -> None:
        # Zip containing random text, neither DOCX nor XLSX
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("notes.txt", "hello enterprise")
        generic_zip = buf.getvalue()

        doc_type, mime = detect_file_format(generic_zip)
        assert doc_type is None
        assert mime == "application/zip"


# ==========================================
# TEST SUITE: DOCUMENT VALIDATOR
# ==========================================

class TestDefaultDocumentValidator:
    """Test end-to-end document validation including size limits, corrupted data, and mismatches."""

    def test_validate_all_four_supported_formats(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            # 1. PDF
            res_pdf = await validator.validate(create_valid_pdf_bytes(), "handbook.pdf", ctx)
            assert res_pdf.is_valid is True
            assert res_pdf.detected_mime_type == DocumentMimeType.PDF.value
            assert len(res_pdf.errors) == 0

            # 2. DOCX
            res_docx = await validator.validate(create_valid_docx_bytes(), "memo.docx", ctx)
            assert res_docx.is_valid is True
            assert res_docx.detected_mime_type == DocumentMimeType.DOCX.value
            assert len(res_docx.errors) == 0

            # 3. XLSX
            res_xlsx = await validator.validate(create_valid_xlsx_bytes(), "budget.xlsx", ctx)
            assert res_xlsx.is_valid is True
            assert res_xlsx.detected_mime_type == DocumentMimeType.XLSX.value
            assert len(res_xlsx.errors) == 0

            # 4. CSV
            res_csv = await validator.validate(create_valid_csv_bytes(), "salaries.csv", ctx)
            assert res_csv.is_valid is True
            assert res_csv.detected_mime_type == DocumentMimeType.CSV.value
            assert len(res_csv.errors) == 0

        asyncio.run(_run())

    def test_reject_empty_file(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            result = await validator.validate(b"", "empty.pdf", ctx)
            assert result.is_valid is False
            assert any("empty" in e.lower() for e in result.errors)

            with pytest.raises(EmptyFileError, match="empty"):
                await validator.validate_or_raise(b"", "empty.pdf", ctx)

        asyncio.run(_run())

    def test_reject_exceeding_file_size(self) -> None:
        async def _run() -> None:
            custom_settings = IngestionSettings(max_file_size_bytes=100)
            validator = DefaultDocumentValidator(settings=custom_settings)
            ctx = create_context()

            large_pdf = create_valid_pdf_bytes() + (b"0" * 200)
            result = await validator.validate(large_pdf, "large.pdf", ctx)
            assert result.is_valid is False
            assert any("exceeds maximum allowed limit" in e for e in result.errors)

            with pytest.raises(FileSizeExceededError, match="exceeds maximum limit"):
                await validator.validate_or_raise(large_pdf, "large.pdf", ctx)

        asyncio.run(_run())

    def test_reject_unsupported_file_extension(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            result = await validator.validate(b"random content", "malicious.exe", ctx)
            assert result.is_valid is False
            assert any("not supported" in e for e in result.errors)

            with pytest.raises(UnsupportedDocumentTypeError, match="not supported"):
                await validator.validate_or_raise(b"random content", "malicious.exe", ctx)

        asyncio.run(_run())

    def test_reject_signature_mismatch(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            # PDF content disguised as DOCX
            pdf_bytes = create_valid_pdf_bytes()
            result = await validator.validate(pdf_bytes, "fake_document.docx", ctx)
            assert result.is_valid is False
            assert any("signature mismatch" in e.lower() for e in result.errors)

            with pytest.raises(FileSignatureMismatchError, match="Signature mismatch"):
                await validator.validate_or_raise(pdf_bytes, "fake_document.docx", ctx)

        asyncio.run(_run())

    def test_reject_docx_disguised_as_xlsx(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            docx_bytes = create_valid_docx_bytes()
            result = await validator.validate(docx_bytes, "fake_sheet.xlsx", ctx)
            assert result.is_valid is False
            assert any("signature mismatch" in e.lower() or "mismatched document type" in e.lower() for e in result.errors)

            with pytest.raises(FileSignatureMismatchError):
                await validator.validate_or_raise(docx_bytes, "fake_sheet.xlsx", ctx)

        asyncio.run(_run())

    def test_reject_corrupted_pdf_missing_eof(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            # Truncated PDF without EOF trailer
            corrupt_pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            result = await validator.validate(corrupt_pdf, "truncated.pdf", ctx)
            assert result.is_valid is False
            assert any("missing mandatory '%%eof'" in e.lower() for e in result.errors)

            with pytest.raises(MalformedFileError, match="Missing mandatory '%%EOF'"):
                await validator.validate_or_raise(corrupt_pdf, "truncated.pdf", ctx)

        asyncio.run(_run())

    def test_reject_corrupted_docx_truncated_zip(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            corrupted_zip = b"PK\x03\x04\x14\x00\x00\x00\x08\x00garbage_truncated_content"
            result = await validator.validate(corrupted_zip, "corrupt.docx", ctx)
            assert result.is_valid is False
            assert any("corrupted docx" in e.lower() or "zip archive" in e.lower() for e in result.errors)

            with pytest.raises(MalformedFileError):
                await validator.validate_or_raise(corrupted_zip, "corrupt.docx", ctx)

        asyncio.run(_run())

    def test_reject_csv_with_binary_null_bytes(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()

            binary_csv = b"col1,col2,col3\nval1,\x00\x01\x02,val3\n"
            result = await validator.validate(binary_csv, "binary.csv", ctx)
            assert result.is_valid is False
            assert any("null bytes detected" in e.lower() for e in result.errors)

            with pytest.raises(FileSignatureMismatchError, match="null bytes detected"):
                await validator.validate_or_raise(binary_csv, "binary.csv", ctx)

        asyncio.run(_run())

    def test_path_traversal_in_validate_or_raise(self) -> None:
        async def _run() -> None:
            validator = DefaultDocumentValidator()
            ctx = create_context()
            valid_pdf = create_valid_pdf_bytes()

            with pytest.raises(PathTraversalError, match="Path traversal sequence"):
                await validator.validate_or_raise(valid_pdf, "../../etc/passwd.pdf", ctx)

        asyncio.run(_run())
