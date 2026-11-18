"""Filename sanitization and path traversal prevention utilities."""

import os
import re
from pathlib import Path

from backend.ingestion.exceptions import PathTraversalError, ValidationError

# Windows reserved device names that cannot be used as filenames or stems
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# Dangerous characters disallowed in cross-platform filenames:
# Control characters, slashes, backslashes, colons, quotes, pipes, wildcards, angle brackets
DANGEROUS_CHARS_REGEX = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """Sanitize uploaded filename to prevent directory traversal and filesystem attacks.

    Args:
        filename: Raw user-provided or extracted filename.
        max_length: Maximum allowed length for the sanitized filename (default 255).

    Returns:
        Clean, safe, normalized filename string.

    Raises:
        PathTraversalError: If path traversal sequences, null bytes, or absolute paths are detected.
        ValidationError: If filename is empty, malformed, or references system reserved names.
    """
    if not filename or not isinstance(filename, str):
        raise ValidationError("Filename cannot be empty or non-string")

    stripped = filename.strip()
    if not stripped:
        raise ValidationError("Filename cannot be blank or whitespace-only")

    # 1. Reject null bytes immediately
    if "\x00" in stripped:
        raise PathTraversalError(
            "Null byte injection detected in filename",
            details={"raw_filename": filename},
        )

    # 2. Reject explicit directory traversal patterns
    if ".." in stripped or stripped.startswith(("/", "\\")):
        raise PathTraversalError(
            f"Path traversal sequence detected in filename: '{filename}'",
            details={"raw_filename": filename},
        )

    # Check for encoded traversal patterns
    lower_raw = stripped.lower()
    if "%2e%2e" in lower_raw or "%2f" in lower_raw or "%5c" in lower_raw:
        raise PathTraversalError(
            f"URL-encoded path traversal sequence detected in filename: '{filename}'",
            details={"raw_filename": filename},
        )

    # 3. Strip directory path components to keep only the basename
    base_name = os.path.basename(stripped.replace("\\", "/"))
    if not base_name:
        raise ValidationError("Filename resolves to an empty basename")

    # 4. Remove dangerous/illegal characters
    cleaned = DANGEROUS_CHARS_REGEX.sub("_", base_name)

    # 5. Prevent leading dot to avoid hidden files on POSIX
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        raise ValidationError(
            f"Filename '{filename}' is empty after removing leading dots",
            details={"raw_filename": filename},
        )

    # 6. Extract stem and extension
    path_obj = Path(cleaned)
    extension = path_obj.suffix.lower()
    stem = path_obj.stem.strip()

    if not stem:
        raise ValidationError(
            f"Filename '{filename}' lacks a valid name stem before extension",
            details={"raw_filename": filename},
        )

    # 7. Check against Windows reserved device names
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        raise ValidationError(
            f"Filename stem '{stem}' is a reserved system device name",
            details={"raw_filename": filename, "reserved_stem": stem},
        )

    # 8. Collapse consecutive spaces and replace with underscores
    cleaned_stem = re.sub(r"\s+", "_", stem)
    # Remove any characters outside standard alphanumeric, hyphen, underscore, and dot
    cleaned_stem = re.sub(r"[^\w\.-]", "_", cleaned_stem)

    final_name = f"{cleaned_stem}{extension}"

    # 9. Enforce maximum length limit while preserving extension
    if len(final_name) > max_length:
        max_stem_len = max(1, max_length - len(extension))
        final_name = f"{cleaned_stem[:max_stem_len]}{extension}"

    return final_name


def validate_safe_path(target_path: Path | str, base_dir: Path | str) -> Path:
    """Validate that target_path resolves strictly within base_dir to block traversal attacks.

    Args:
        target_path: Path to validate.
        base_dir: Enclosing boundary root directory.

    Returns:
        Resolved Path guaranteed to reside inside base_dir.

    Raises:
        PathTraversalError: If target_path resolves outside base_dir.
    """
    resolved_base = Path(base_dir).resolve()
    resolved_target = Path(target_path).resolve()

    try:
        # Python 3.9+ is_relative_to
        if not resolved_target.is_relative_to(resolved_base):
            raise PathTraversalError(
                f"Path traversal detected: '{target_path}' resolves outside allowed base directory",
                details={
                    "target_path": str(target_path),
                    "base_dir": str(base_dir),
                    "resolved_target": str(resolved_target),
                    "resolved_base": str(resolved_base),
                },
            )
    except AttributeError:
        # Fallback for Python versions without is_relative_to
        try:
            resolved_target.relative_to(resolved_base)
        except ValueError:
            raise PathTraversalError(
                f"Path traversal detected: '{target_path}' resolves outside allowed base directory",
                details={"target_path": str(target_path), "base_dir": str(base_dir)},
            )

    return resolved_target
