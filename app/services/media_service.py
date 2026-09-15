"""Select and validate one supported attachment for an explicit invocation."""

from typing import Any, Protocol

from app.schemas.media import MediaAttachment, MediaError

SUPPORTED = {"image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain"}


class MediaClient(Protocol):
    async def get_file(self, file_id: str) -> dict[str, Any]: ...
    async def download_file(self, file_path: str, max_bytes: int) -> bytes: ...


class MediaService:
    def __init__(self, client: MediaClient, max_bytes: int) -> None:
        self.client = client
        self.max_bytes = max_bytes

    async def load(self, message: dict[str, Any]) -> MediaAttachment | None:
        selected = self._select(message)
        if selected is None:
            reply = message.get("reply_to_message")
            selected = self._select(reply) if isinstance(reply, dict) else None
        if selected is None:
            return None
        descriptor, mime_type, source_id, file_name = selected
        declared_size = descriptor.get("file_size")
        if type(declared_size) is int and declared_size > self.max_bytes:
            raise MediaError(
                "media_too_large", "That attachment is too large. Please send one under 10 MB."
            )
        file_id = descriptor.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise MediaError("invalid_media", "I couldn't read that attachment.")
        try:
            remote = await self.client.get_file(file_id)
            remote_size = remote.get("file_size")
            if type(remote_size) is int and remote_size > self.max_bytes:
                raise MediaError(
                    "media_too_large", "That attachment is too large. Please send one under 10 MB."
                )
            file_path = remote.get("file_path")
            if not isinstance(file_path, str) or not file_path:
                raise MediaError("invalid_media", "I couldn't read that attachment.")
            data = await self.client.download_file(file_path, self.max_bytes)
        except MediaError:
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == "file_too_large":
                raise MediaError(
                    "media_too_large", "That attachment is too large. Please send one under 10 MB."
                ) from None
            raise MediaError(
                "media_download_failed", "I couldn't download that attachment. Please try again."
            ) from None
        self._validate(data, mime_type)
        return MediaAttachment(data, mime_type, source_id, file_name)

    def _select(
        self, message: dict[str, Any]
    ) -> tuple[dict[str, Any], str, int, str | None] | None:
        source_id = message.get("message_id")
        if type(source_id) is not int:
            return None
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            valid = [item for item in photos if isinstance(item, dict)]
            if valid:
                item = max(
                    valid,
                    key=lambda p: (
                        p.get("file_size") or 0,
                        (p.get("width") or 0) * (p.get("height") or 0),
                    ),
                )
                return item, "image/jpeg", source_id, None
        document = message.get("document")
        if isinstance(document, dict):
            mime = document.get("mime_type")
            if mime not in SUPPORTED:
                raise MediaError(
                    "unsupported_media",
                    "I can currently read JPEG, PNG, WebP, PDF, or plain-text attachments.",
                )
            name = document.get("file_name")
            return document, mime, source_id, name if isinstance(name, str) else None
        return None

    @staticmethod
    def _validate(data: bytes, mime_type: str) -> None:
        valid = bool(data)
        if mime_type == "image/jpeg":
            valid = data.startswith(b"\xff\xd8\xff")
        elif mime_type == "image/png":
            valid = data.startswith(b"\x89PNG\r\n\x1a\n")
        elif mime_type == "image/webp":
            valid = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        elif mime_type == "application/pdf":
            valid = data.startswith(b"%PDF-")
        elif mime_type == "text/plain":
            try:
                data.decode("utf-8", errors="strict")
                valid = bool(data) and b"\x00" not in data
            except UnicodeDecodeError:
                valid = False
        if not valid:
            raise MediaError(
                "invalid_media", "That attachment doesn't match its declared file type."
            )
