"""Validation rules for user-provided image and text attachments."""

from __future__ import annotations

import io
import re
from pathlib import PurePath

from PIL import Image, UnidentifiedImageError

from leanharness.errors import AttachmentError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 8
MAX_ATTACHMENTS_TOTAL_BYTES = 20 * 1024 * 1024

_TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
        ".md", ".css", ".html", ".htm", ".sql", ".java", ".go", ".rs", ".toml",
        ".xml", ".sh", ".bat", ".ps1", ".c", ".h", ".cpp", ".hpp", ".cs",
    }
)
_IMAGE_MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_SAFE_NAME = re.compile(r"[^\w.()\- ]", re.UNICODE)


def validate_attachment(
    filename: str,
    media_type: str | None,
    data: bytes,
) -> tuple[str, str, str]:
    """Return ``(safe_filename, kind, canonical_media_type)`` or fail closed."""
    if not isinstance(filename, str) or not filename.strip():
        raise AttachmentError("Attachment filename is required")
    name = PurePath(filename.replace("\\", "/")).name
    name = _SAFE_NAME.sub("_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise AttachmentError("Attachment filename is invalid")
    suffix = PurePath(name).suffix.casefold()
    if suffix in _TEXT_EXTENSIONS:
        if len(data) > MAX_TEXT_BYTES:
            raise AttachmentError("Text attachment exceeds 512 KiB")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttachmentError("Text attachment must be valid UTF-8") from exc
        if media_type and not (
            media_type.startswith("text/")
            or media_type in {"application/json", "application/octet-stream"}
        ):
            raise AttachmentError("Attachment MIME type does not match text content")
        return name, "text", "text/plain"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise AttachmentError("Attachment type is not supported")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError("Image attachment exceeds 10 MiB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            image_format = image.format
    except (UnidentifiedImageError, OSError) as exc:
        raise AttachmentError("Image attachment is not a valid PNG, JPEG, or WebP") from exc
    canonical = _IMAGE_MEDIA.get(str(image_format))
    if canonical is None:
        raise AttachmentError("Image format is not supported")
    if media_type and media_type not in {canonical, "application/octet-stream"}:
        raise AttachmentError("Attachment MIME type does not match image content")
    return name, "image", canonical
