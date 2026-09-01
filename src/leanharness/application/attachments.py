"""Build one current-run model message from validated local attachments."""

from __future__ import annotations

from typing import Literal, cast

from leanharness.errors import AttachmentError
from leanharness.models import ImageContent, ModelMessage
from leanharness.storage import AttachmentRecord, LocalStore

MAX_MODEL_ATTACHMENT_TEXT_CHARS = 120_000


def attachment_to_dict(attachment: AttachmentRecord) -> dict[str, object]:
    return {
        "id": attachment.id,
        "session_id": attachment.session_id,
        "message_id": attachment.message_id,
        "filename": attachment.filename,
        "media_type": attachment.media_type,
        "kind": attachment.kind,
        "byte_size": attachment.byte_size,
        "sha256": attachment.sha256,
        "created_at": attachment.created_at,
    }


def message_with_attachments(
    store: LocalStore,
    session_id: str,
    message: str,
    attachment_ids: tuple[str, ...],
) -> ModelMessage:
    """Return the exact current user input; raw attachment data stays in memory."""
    text_parts = [message]
    images: list[ImageContent] = []
    text_budget = MAX_MODEL_ATTACHMENT_TEXT_CHARS
    for attachment_id in attachment_ids:
        attachment = store.get_attachment(attachment_id)
        if attachment.session_id != session_id:
            raise AttachmentError("Attachment does not belong to this session")
        data = store.read_attachment(attachment.id, session_id=session_id)
        if attachment.kind == "text":
            decoded = data.decode("utf-8")
            if len(decoded) > text_budget:
                decoded = (
                    decoded[: max(0, text_budget - 180)]
                    + "\n[attachment content truncated for the current model request; "
                    "the stored attachment remains available by its metadata]"
                )
            text_budget = max(0, text_budget - len(decoded))
            text_parts.extend(
                [
                    "",
                    f"<attachment filename={attachment.filename!r}>",
                    decoded,
                    "</attachment>",
                ]
            )
        else:
            media_type = cast(
                Literal["image/png", "image/jpeg", "image/webp"],
                attachment.media_type,
            )
            images.append(ImageContent(media_type, data))
            text_parts.extend(["", f"[Image attachment: {attachment.filename}]"])
    return ModelMessage(content="\n".join(text_parts), role="user", images=tuple(images))
