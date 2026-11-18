"""Document validation contracts and interfaces."""

import abc
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.ingestion.types.context import IngestionContext


class ValidationResult(BaseModel):
    """Validation outcome report verifying document safety, format, and size constraints."""

    model_config = ConfigDict(frozen=True)

    is_valid: bool = Field(..., description="Whether document passed all validation checks")
    errors: list[str] = Field(default_factory=list, description="Validation failure messages if invalid")
    warnings: list[str] = Field(default_factory=list, description="Non-fatal warnings (e.g. large file, low OCR DPI)")
    detected_mime_type: str | None = Field(default=None, description="MIME type confirmed by magic byte inspection")
    file_size_bytes: int = Field(default=0, ge=0, description="Measured file size in bytes")
    details: dict[str, Any] = Field(default_factory=dict, description="Diagnostic validation metadata")


class DocumentValidator(abc.ABC):
    """Abstract contract for pre-ingestion document format and integrity verification."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Validator identifier name."""
        raise NotImplementedError

    @abc.abstractmethod
    async def validate(
        self,
        content: bytes | Path,
        filename: str,
        context: IngestionContext,
    ) -> ValidationResult:
        """Validate raw binary stream or file path before parsing begins."""
        raise NotImplementedError
