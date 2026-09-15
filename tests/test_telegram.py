import json
from html import unescape

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.telegram import TelegramUpdate
from app.services.formatting import plain_chunks, render_chunks
from app.services.invocation import detect_invocation
from app.services.telegram_client import (
    TelegramClient,
    TelegramError,
    TelegramSendUncertain,
)


def test_mentions_use_utf16_offsets_and_identity():
    message = {
        "text": "😀 @GroupBot hi",
        "entities": [{"type": "mention", "offset": 3, "length": 9}],
    }
    assert detect_invocation(message, 42, "groupbot").kind == "ai"
    assert detect_invocation(message, 42, "otherbot") is None
    message = {
        "text": "hey Jane",
        "entities": [{"type": "text_mention", "offset": 4, "length": 4, "user": {"id": 42}}],
    }
    assert detect_invocation(message, 42, "groupbot").kind == "ai"
    assert detect_invocation(message, 99, "groupbot") is None


@pytest.mark.parametrize(
    "text,kind,expected_request",
    [
        ("/ask@GroupBot hello", "ai", "hello"),
        ("/ask\nhello", "ai", "hello"),
        ("/help", "help", ""),
        ("/start", "start", ""),
    ],
)
def test_supported_commands(text, kind, expected_request):
    result = detect_invocation({"text": text}, 42, "groupbot")
    assert (result.kind, result.request) == (kind, expected_request)


def test_passive_commands_replies_and_bots():
    assert (
        detect_invocation(
            {"text": "ordinary", "from": None, "reply_to_message": None}, 42, "groupbot"
        )
        is None
    )
    assert detect_invocation({"text": "plain @groupbot without entity"}, 42, "groupbot") is None
    assert detect_invocation({"text": "/ask@otherbot @groupbot"}, 42, "groupbot") is None
    assert detect_invocation({"text": "/unknown @groupbot"}, 42, "groupbot") is None
    reply = {"text": "continue", "reply_to_message": {"from": {"id": 42}}}
    assert detect_invocation(reply, 42, "groupbot").kind == "ai"
    assert detect_invocation(reply, 43, "groupbot") is None
    reply["from"] = {"is_bot": True}
    assert detect_invocation(reply, 42, "groupbot") is None


def test_escape_and_chunk_boundaries():
    text = "<tag>& 😀" * 1200
    chunks = render_chunks(text)
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert "".join(unescape(chunk) for chunk in chunks) == text
    assert all("<" not in chunk and ">" not in chunk for chunk in chunks)
    assert "".join(plain_chunks(text, 13)) == text
    assert render_chunks("") == []


def test_astral_characters_respect_utf16_chunk_limit():
    text = "😀" * 4100
    for splitter in (render_chunks, plain_chunks):
        chunks = splitter(text)
        assert "".join(chunks) == text
        assert all(len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks)


def test_captions_support_entity_mentions_without_media_reasoning():
    result = detect_invocation(
        {
            "caption": "@groupbot explain",
            "caption_entities": [{"type": "mention", "offset": 0, "length": 9}],
            "photo": [{"file_id": "opaque"}],
        },
        42,
        "groupbot",
    )
    assert result is not None
    assert result.request == "@groupbot explain"
    assert detect_invocation({"photo": [{"file_id": "opaque"}]}, 42, "groupbot") is None


def test_schema_preserves_unknown_fields_and_sender_alias():
    payload = {
        "update_id": 2,
        "message": {
            "message_id": 4,
            "date": 1,
            "chat": {"id": -1, "type": "group"},
            "from": {"id": 3},
            "photo": [{"file_id": "abc"}],
        },
    }
    parsed = TelegramUpdate.model_validate(payload)
    assert parsed.model_dump(by_alias=True)["message"]["from"] == {"id": 3}
    assert parsed.model_dump(by_alias=True)["message"]["photo"] == [{"file_id": "abc"}]


@pytest.mark.parametrize("bad_id", [True, "123"])
def test_boundary_rejects_coerced_ids(bad_id):
    with pytest.raises(ValidationError):
        TelegramUpdate.model_validate({"update_id": bad_id})
    with pytest.raises(ValidationError):
        TelegramUpdate.model_validate(
            {
                "update_id": 1,
                "message": {"message_id": 2, "date": 3, "chat": {"id": bad_id, "type": "group"}},
            }
        )


def test_null_mention_identity_is_ignored():
    assert (
        detect_invocation(
            {"text": "Jane", "entities": [None, {"type": "text_mention", "user": None}]},
            42,
            "groupbot",
        )
        is None
    )


@pytest.mark.asyncio
async def test_client_methods_and_safe_setup_payloads():
    seen = []

    def handler(request):
        method = request.url.path.split("/")[-1]
        payload = json.loads(request.content)
        seen.append((method, payload))
        result = [] if method == "getUpdates" else {"id": 42, "username": "groupbot"}
        if method == "sendMessage":
            result = {"message_id": 8, "date": 1, "chat": {"id": -1, "type": "group"}}
        elif method == "sendPoll":
            result = {
                "message_id": 9,
                "date": 1,
                "chat": {"id": -1, "type": "group"},
                "poll": {"id": "poll-1"},
            }
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient("SECRET", client=http)
        assert (await client.get_me())["id"] == 42
        await client.send_message(-1, "hello", reply_to_message_id=7)
        await client.send_poll(-1, "Where?", ["Seoul", "Busan"])
        await client.set_webhook("https://example.test/hook", "hooksecret")
        await client.get_webhook_info()
        await client.delete_webhook()
        assert await client.get_updates() == []
    assert seen[1][1]["reply_parameters"] == {"message_id": 7}
    assert seen[1][1]["parse_mode"] == "HTML"
    assert seen[2] == (
        "sendPoll",
        {
            "chat_id": -1,
            "question": "Where?",
            "options": [{"text": "Seoul"}, {"text": "Busan"}],
            "is_anonymous": False,
            "allows_multiple_answers": False,
        },
    )
    assert all("drop_pending_updates" not in payload for _, payload in seen)
    assert "offset" not in seen[-1][1]


@pytest.mark.asyncio
async def test_formatting_error_and_token_sanitization():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "ok": False,
                "description": "Bad Request: can't parse entities SECRET",
                "error_code": 400,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TelegramError) as caught:
            await TelegramClient("SECRET", client=http).send_message(-1, "bad")
    assert caught.value.code == "formatting"
    assert "SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_transport_send_is_uncertain_and_not_retried():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout(f"SECRET {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TelegramSendUncertain) as caught:
            await TelegramClient("SECRET", client=http).send_message(-1, "hello")
    assert count == 1
    assert "SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.asyncio
async def test_poll_transport_failure_is_uncertain_and_not_retried():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TelegramSendUncertain):
            await TelegramClient("SECRET", client=http).send_poll(-1, "Where?", ["A", "B"])
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"ok": True},
        {"ok": True, "result": None},
        {"ok": True, "result": {"message_id": True, "date": 1, "chat": {"id": -1}}},
        {"ok": True, "result": {"message_id": 2, "date": 1, "chat": {"id": -2}}},
    ],
)
async def test_malformed_send_response_is_uncertain(body):
    def handler(request):
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TelegramSendUncertain):
            await TelegramClient("SECRET", client=http).send_message(-1, "hello")
