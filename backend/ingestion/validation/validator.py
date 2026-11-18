"""Default enterprise document validator enforcing format signatures, size limits, and security."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from backend.ingestion.config import IngestionSettings, get_ingestion_settings
from backend.ingestion.exceptions import (
    CorruptDocumentError,
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
from backend.ingestion.validation.interfaces import DocumentValidator, ValidationResult
from backend.ingestion.validation.sanitizer import sanitize_filename
from backend.ingestion.validation.signatures import (
    ALL_ZIP_MAGICS,
    PDF_MAGIC,
    detect_file_format,
    validate_csv_integrity,
    validate_docx_integrity,
    validate_pdf_integrity,
    validate_xlsx_integrity,
)


class DefaultDocumentValidator(DocumentValidator):
    """Production-grade document validator enforcing file signatures, size constraints, and sanitization."""

    def __init__(self, settings: IngestionSettings | None = None) -> None:
        self.settings = settings or get_ingestion_settings()
        self._allowed_types: set[DocumentType] = {
            DocumentType.PDF,
            DocumentType.DOCX,
            DocumentType.XLSX,
            DocumentType.CSV,
        }

    @property
    def name(self) -> str:
        """Validator identifier name."""
        return "default_document_validator"

    def _resolve_bytes(self, content: bytes | BinaryIO | Path) -> bytes:
        """Read content input into contiguous byte stream."""
        if isinstance(content, bytes):
            return content
        if isinstance(content, Path):
            if not content.exists() or not content.is_file():
                raise ValidationError(f"File path does not exist or is not a regular file: {content}")
            return content.read_bytes()
        if hasattr(content, "read"):
            data = content.read()
            if isinstance(data, str):
                return data.encode("utf-8")
            return data
        raise ValidationError(f"Unsupported content input type: {type(content)}")

    async def validate(
        self,
        content: bytes | BinaryIO | Path,
        filename: str,
        context: IngestionContext,
    ) -> ValidationResult:
        """Validate raw binary stream or file path against security and integrity rules.

        Returns:
            ValidationResult containing status, detected MIME, size, errors, and metadata.
        """
        errors: list[str] = []
        warnings: list[str] = []
        details: dict = {}
        detected_mime: str | None = None
        file_size: int = 0
        sanitized_name: str | None = None

        # 1. Filename sanitization & path traversal check
        try:
            sanitized_name = sanitize_filename(filename, max_length=self.settings.max_filename_length)
            details["sanitized_filename"] = sanitized_name
            details["original_filename"] = filename
        except (PathTraversalError, ValidationError) as e:
            errors.append(str(e))
            details["filename_error"] = str(e)
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=None,
                file_size_bytes=0,
                details=details,
            )

        # 2. Resolve content to bytes
        try:
            raw_bytes = self._resolve_bytes(content)
            file_size = len(raw_bytes)
        except Exception as e:
            errors.append(f"Failed to read file content: {e}")
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=None,
                file_size_bytes=0,
                details=details,
            )

        # 3. File size constraints
        if file_size < self.settings.min_file_size_bytes:
            errors.append(f"Uploaded file is empty (size is {file_size} bytes)")
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=None,
                file_size_bytes=file_size,
                details=details,
            )

        if file_size > self.settings.max_file_size_bytes:
            errors.append(
                f"File size ({file_size} bytes) exceeds maximum allowed limit "
                f"({self.settings.max_file_size_bytes} bytes)"
            )
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=None,
                file_size_bytes=file_size,
                details=details,
            )

        # 4. Check extension against allowed extensions
        file_ext = Path(sanitized_name).suffix.lower()
        if file_ext not in self.settings.allowed_extensions:
            errors.append(
                f"File extension '{file_ext}' is not supported. "
                f"Allowed extensions: {self.settings.allowed_extensions}"
            )
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=None,
                file_size_bytes=file_size,
                details=details,
            )

        # 5. Compute SHA-256 content hash
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        details["content_hash"] = content_hash
        details["tenant_id"] = context.tenant_id
        details["validation_time"] = datetime.now(timezone.utc).isoformat()

        # Resolve expected DocumentType from filename
        try:
            expected_type = DocumentType.from_filename(sanitized_name)
        except ValueError:
            expected_type = None

        # Check format-specific structural integrity early to detect corrupted or malformed files
        if expected_type == DocumentType.CSV:
            try:
                validate_csv_integrity(raw_bytes)
            except (FileSignatureMismatchError, MalformedFileError, CorruptDocumentError) as e:
                errors.append(str(e))
                details["integrity_error"] = str(e)
                return ValidationResult(
                    is_valid=False,
                    errors=errors,
                    warnings=warnings,
                    detected_mime_type=None,
                    file_size_bytes=file_size,
                    details=details,
                )

        if expected_type in (DocumentType.DOCX, DocumentType.XLSX):
            if any(raw_bytes.startswith(m) for m in ALL_ZIP_MAGICS):
                try:
                    if expected_type == DocumentType.DOCX:
                        validate_docx_integrity(raw_bytes)
                    else:
                        validate_xlsx_integrity(raw_bytes)
                except (FileSignatureMismatchError, MalformedFileError, CorruptDocumentError) as e:
                    errors.append(str(e))
                    details["integrity_error"] = str(e)
                    return ValidationResult(
                        is_valid=False,
                        errors=errors,
                        warnings=warnings,
                        detected_mime_type="application/x-corrupt-zip",
                        file_size_bytes=file_size,
                        details=details,
                    )

        if expected_type == DocumentType.PDF:
            if raw_bytes.startswith(PDF_MAGIC):
                try:
                    validate_pdf_integrity(raw_bytes)
                except (FileSignatureMismatchError, MalformedFileError, CorruptDocumentError) as e:
                    errors.append(str(e))
                    details["integrity_error"] = str(e)
                    return ValidationResult(
                        is_valid=False,
                        errors=errors,
                        warnings=warnings,
                        detected_mime_type=DocumentMimeType.PDF.value,
                        file_size_bytes=file_size,
                        details=details,
                    )

        # 6. Magic bytes inspection
        detected_type, detected_mime = detect_file_format(raw_bytes)
        details["detected_mime_type"] = detected_mime
        details["detected_document_type"] = detected_type.value if detected_type else None

        if detected_type is None or detected_type not in self._allowed_types:
            errors.append(
                f"File signature not recognized as a supported document format. "
                f"Detected MIME: '{detected_mime or 'unknown'}'"
            )
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=detected_mime,
                file_size_bytes=file_size,
                details=details,
            )

        # 7. Match declared extension with detected magic byte format
        if expected_type != detected_type:
            errors.append(
                f"File signature mismatch: File '{sanitized_name}' has extension for "
                f"'{expected_type.value if expected_type else 'unknown'}', but actual binary signature "
                f"matches '{detected_type.value}' ({detected_mime})"
            )
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=detected_mime,
                file_size_bytes=file_size,
                details=details,
            )

        # 8. Structural integrity verification for valid types
        try:
            if detected_type == DocumentType.PDF:
                validate_pdf_integrity(raw_bytes)
            elif detected_type == DocumentType.DOCX:
                validate_docx_integrity(raw_bytes)
            elif detected_type == DocumentType.XLSX:
                validate_xlsx_integrity(raw_bytes)
            elif detected_type == DocumentType.CSV:
                validate_csv_integrity(raw_bytes)
        except (FileSignatureMismatchError, MalformedFileError, CorruptDocumentError) as e:
            errors.append(str(e))
            details["integrity_error"] = str(e)
            return ValidationResult(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                detected_mime_type=detected_mime,
                file_size_bytes=file_size,
                details=details,
            )

        # Check for non-fatal warnings
        if file_size > 20 * 1024 * 1024:
            warnings.append(f"Large document ({file_size / (1024 * 1024):.1f} MB); extraction may take longer")

        return ValidationResult(
            is_valid=True,
            errors=[],
            warnings=warnings,
            detected_mime_type=detected_mime,
            file_size_bytes=file_size,
            details=details,
        )

    async def validate_or_raise(
        self,
        content: bytes | BinaryIO | Path,
        filename: str,
        context: IngestionContext,
    ) -> ValidationResult:
        """Validate and raise specific domain exception on any failure.

        Raises:
            PathTraversalError: If filename attempts path traversal or unsafe characters.
            EmptyFileError: If file is 0 bytes.
            FileSizeExceededError: If file exceeds maximum allowed size.
            UnsupportedDocumentTypeError: If extension or MIME type is unsupported.
            FileSignatureMismatchError: If magic bytes do not match expected format.
            MalformedFileError / CorruptDocumentError: If internal structure is corrupted.
            ValidationError: For general validation failures.
        """
        # 1. Pre-check filename for path traversal to throw exact error type
        try:
            clean_name = sanitize_filename(filename, max_length=self.settings.max_filename_length)
        except PathTraversalError:
            raise
        except ValidationError:
            raise

        # 2. Pre-check file size
        raw_bytes = self._resolve_bytes(content)
        if len(raw_bytes) < self.settings.min_file_size_bytes:
            raise EmptyFileError(
                f"Uploaded file '{filename}' is empty ({len(raw_bytes)} bytes)",
                details={"file_size_bytes": len(raw_bytes)},
            )
        if len(raw_bytes) > self.settings.max_file_size_bytes:
            raise FileSizeExceededError(
                f"File size ({len(raw_bytes)} bytes) exceeds maximum limit ({self.settings.max_file_size_bytes} bytes)",
                details={"file_size_bytes": len(raw_bytes), "max_allowed": self.settings.max_file_size_bytes},
            )

        # 3. Check extension
        file_ext = Path(clean_name).suffix.lower()
        if file_ext not in self.settings.allowed_extensions:
            raise UnsupportedDocumentTypeError(
                f"File extension '{file_ext}' is not supported. Allowed: {self.settings.allowed_extensions}",
                details={"extension": file_ext},
            )

        expected_type = DocumentType.from_filename(clean_name)

        # 4. Check integrity of expected type (catches corrupted/truncated files before signature mismatch)
        if expected_type == DocumentType.CSV:
            validate_csv_integrity(raw_bytes)
        elif expected_type == DocumentType.DOCX and any(raw_bytes.startswith(m) for m in ALL_ZIP_MAGICS):
            validate_docx_integrity(raw_bytes)
        elif expected_type == DocumentType.XLSX and any(raw_bytes.startswith(m) for m in ALL_ZIP_MAGICS):
            validate_xlsx_integrity(raw_bytes)
        elif expected_type == DocumentType.PDF and raw_bytes.startswith(PDF_MAGIC):
            validate_pdf_integrity(raw_bytes)

        # 5. Check magic bytes
        detected_type, detected_mime = detect_file_format(raw_bytes)
        if detected_type is None or detected_type not in self._allowed_types:
            raise FileSignatureMismatchError(
                f"File signature not recognized as a supported document format (detected: {detected_mime})",
                details={"detected_mime": detected_mime},
            )

        if expected_type != detected_type:
            raise FileSignatureMismatchError(
                f"Signature mismatch: Declared '{expected_type.value}', detected '{detected_type.value}' ({detected_mime})",
                details={"expected_type": expected_type.value, "detected_type": detected_type.value},
            )

        # 6. Final integrity check
        if detected_type == DocumentType.PDF:
            validate_pdf_integrity(raw_bytes)
        elif detected_type == DocumentType.DOCX:
            validate_docx_integrity(raw_bytes)
        elif detected_type == DocumentType.XLSX:
            validate_xlsx_integrity(raw_bytes)
        elif detected_type == DocumentType.CSV:
            validate_csv_integrity(raw_bytes)

        return await self.validate(raw_bytes, filename, context)
