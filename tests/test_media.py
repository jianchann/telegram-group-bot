from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from app.schemas.media import MediaError
from app.services.gemini_gateway import GoogleGeminiGateway
from app.services.media_service import MediaService


class Client:
    def __init__(self, data: bytes = b"\xff\xd8\xffimage") -> None:
        self.data = data
        self.requested: list[str] = []

    async def get_file(self, file_id: str):
        self.requested.append(file_id)
        return {"file_path": "photos/a.jpg", "file_size": len(self.data)}

    async def download_file(self, file_path: str, max_bytes: int):
        assert file_path == "photos/a.jpg"
        assert len(self.data) <= max_bytes
        return self.data


@pytest.mark.asyncio
async def test_media_selects_largest_current_photo_before_replied_document():
    client = Client()
    media = await MediaService(client, 100).load(
        {
            "message_id": 9,
            "photo": [
                {"file_id": "small", "width": 10, "height": 10},
                {"file_id": "large", "width": 20, "height": 20},
            ],
            "reply_to_message": {
                "message_id": 8,
                "document": {"file_id": "reply", "mime_type": "application/pdf"},
            },
        }
    )
    assert media and media.mime_type == "image/jpeg"
    assert media.source_telegram_message_id == 9
    assert client.requested == ["large"]


@pytest.mark.asyncio
async def test_media_rejects_unsupported_and_mismatched_documents():
    service = MediaService(Client(b"not a pdf"), 100)
    with pytest.raises(MediaError, match="unsupported_media"):
        await service.load(
            {"message_id": 1, "document": {"file_id": "x", "mime_type": "video/mp4"}}
        )
    with pytest.raises(MediaError, match="invalid_media"):
        await service.load(
            {
                "message_id": 1,
                "document": {"file_id": "x", "mime_type": "application/pdf"},
            }
        )


@pytest.mark.asyncio
async def test_gateway_sends_inline_bytes_and_preserves_them_during_url_rewrite(settings):
    from app.schemas.media import MediaAttachment
    from app.schemas.research import ResearchToolBundle

    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=[types.Part(text="ok")]))
        ]
    )
    generate = AsyncMock(return_value=response)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    gateway = GoogleGeminiGateway(settings, client=client)
    attachment = MediaAttachment(b"%PDF-x", "application/pdf", 1, "a.pdf")
    await gateway.turn(
        prompt="read this",
        system_prompt="rules",
        tools=[],
        media=attachment,
        research_tools=ResearchToolBundle(use_url_context=True, urls=("https://example.com",)),
    )
    parts = generate.call_args.kwargs["contents"][0].parts
    assert sum(part.inline_data is not None for part in parts) == 1
    assert "Only retrieve" in parts[-1].text
