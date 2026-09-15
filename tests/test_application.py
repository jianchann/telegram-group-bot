import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from app.logging import JSONFormatter
from app.main import create_app
from app.repositories.bot import AcceptedMessage, MessageRecord
from app.schemas.ai import GeminiResult
from app.services.ai_orchestrator import AIOrchestrator
from app.services.context_service import ContextService
from app.services.gemini_gateway import GeminiError
from app.services.telegram_client import TelegramError, TelegramSendUncertain
from app.services.telegram_service import AI_FAILURE_TEXT, TelegramService


class Repository:
    def __init__(self):
        self.accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
        self.updates = set()
        self.messages = []
        self.outgoing = []
        self.statuses = []
        self.runs = []
        self.history = [MessageRecord(1, "Anna is vegetarian", "Anna", False, datetime.now(UTC))]
        self.rate_allowed = True
        self.reservations = 0
        self.failure = False
        self.outgoing_failure = False

    async def accept_message(self, update_id, message, default_timezone):
        if self.failure:
            raise RuntimeError("SECRET DATABASE password URL")
        if update_id in self.updates:
            return None
        self.updates.add(update_id)
        self.messages.append(message)
        return self.accepted

    async def recent_messages(self, chat_id, before_message_id, limit):
        assert chat_id == self.accepted.chat_id
        return self.history

    async def reserve_ai_request(self, chat_id, user_id, user_limit, chat_limit):
        self.reservations += 1
        assert (user_limit, chat_limit) == (5, 20)
        return self.rate_allowed

    async def save_outgoing(self, chat_id, message):
        if self.outgoing_failure:
            raise RuntimeError("SECRET database error after confirmed Telegram send")
        self.outgoing.append(message)

    async def record_ai_run(self, **run):
        self.runs.append(run)

    async def finish_update(self, update_id, status, error_code=None):
        self.statuses.append((update_id, status, error_code))


class Gateway:
    def __init__(self, repository):
        self.repository = repository
        self.calls = []
        self.error = None
        self.delay = 0

    async def run(self, *, prompt, system_prompt):
        assert self.repository.messages  # Persistence completed before AI.
        self.calls.append(json.loads(prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return GeminiResult("Vegetarian options fit Anna's preference in this conversation.", 35, 8)


class Telegram:
    def __init__(self):
        self.calls = []
        self.errors = []

    async def send_message(self, chat_id, text, reply_to_message_id=None, parse_mode="HTML"):
        self.calls.append((chat_id, text, reply_to_message_id, parse_mode))
        if self.errors:
            raise self.errors.pop(0)
        return {
            "message_id": 10 + len(self.calls),
            "date": 1,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": 42, "username": "groupbot", "is_bot": True},
            "text": text,
        }


@pytest.fixture
def bot(settings):
    repository = Repository()
    gateway = Gateway(repository)
    telegram = Telegram()
    service = TelegramService(
        settings,
        repository,
        ContextService(repository, 30, 24000),
        AIOrchestrator(gateway, "No durable memory or tools available."),
        telegram,
        42,
        "groupbot",
    )
    return service, repository, gateway, telegram


def update(text="@groupbot what should we eat?", chat_id=-1001, chat_type="supergroup"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "date": 1,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": 99, "first_name": "Jian"},
            "text": text,
            "entities": [{"type": "mention", "offset": 0, "length": 9}]
            if text.startswith("@groupbot")
            else [],
        },
    }


@pytest.mark.asyncio
async def test_passive_message_is_saved_without_ai_or_response(bot):
    service, repository, gateway, telegram = bot
    await service.process(update("Anna is hungry"))
    assert repository.messages[0]["text"] == "Anna is hungry"
    assert gateway.calls == telegram.calls == []
    assert repository.runs == []
    assert repository.statuses == [(1, "completed", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat_id,chat_type", [(-1002, "supergroup"), (99, "private"), (-1001, "channel")]
)
async def test_unlisted_or_unsupported_chats_do_not_store_or_call_ai(bot, chat_id, chat_type):
    service, repository, gateway, telegram = bot
    await service.process(update(chat_id=chat_id, chat_type=chat_type))
    assert repository.messages == repository.statuses == gateway.calls == telegram.calls == []


@pytest.mark.asyncio
async def test_invocation_sees_context_records_usage_and_stores_reply(bot):
    service, repository, gateway, telegram = bot
    await service.process(update())
    assert gateway.calls[0]["recent_group_context"][0]["text"] == "Anna is vegetarian"
    assert gateway.calls[0]["current_request"]["speaker"] == "Jian"
    assert len(repository.outgoing) == 1
    assert repository.runs[0]["status"] == "success"
    assert (repository.runs[0]["input_tokens"], repository.runs[0]["output_tokens"]) == (35, 8)
    assert telegram.calls[0][2] == 5
    assert repository.statuses[-1][1] == "completed"


@pytest.mark.asyncio
async def test_reply_invokes_and_replay_does_not_repeat_ai_or_reply(bot):
    service, repository, gateway, telegram = bot
    payload = update("Continue")
    payload["message"]["reply_to_message"] = {
        "message_id": 1,
        "from": {"id": 42},
        "text": "Previous answer",
    }
    await service.process(payload)
    await service.process(payload)
    assert len(gateway.calls) == len(telegram.calls) == len(repository.outgoing) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/help", "/start", "/ask"])
async def test_help_start_and_empty_ask_are_deterministic_without_ai(bot, text):
    service, repository, gateway, telegram = bot
    await service.process(update(text))
    assert gateway.calls == []
    assert len(telegram.calls) == len(repository.outgoing) == 1


@pytest.mark.asyncio
async def test_rate_limit_refuses_ai_and_oversized_request_does_not_reserve(bot):
    service, repository, gateway, telegram = bot
    repository.rate_allowed = False
    await service.process(update())
    assert gateway.calls == []
    assert "limit" in telegram.calls[0][1]
    assert repository.statuses[-1][1] == "rate_limited"
    payload = update("/ask " + "x" * 25000)
    payload["update_id"] = 2
    await service.process(payload)
    assert repository.reservations == 1
    assert repository.statuses[-1][1] == "input_limited"


@pytest.mark.asyncio
async def test_gemini_failure_preserves_message_and_sends_retry_response(bot):
    service, repository, gateway, telegram = bot
    gateway.error = GeminiError("provider_error")
    await service.process(update())
    assert len(repository.messages) == 1
    assert telegram.calls[0][1] == AI_FAILURE_TEXT
    assert repository.runs[0]["status"] == "failed"
    assert repository.statuses[-1] == (1, "ai_failed", "provider_error")


@pytest.mark.asyncio
async def test_formatting_fallback_only_after_explicit_format_rejection(bot):
    service, repository, gateway, telegram = bot
    telegram.errors = [TelegramError("formatting")]
    await service.process(update("/help"))
    assert len(telegram.calls) == 2
    assert telegram.calls[0][3] == "HTML"
    assert telegram.calls[1][3] is None
    assert len(repository.outgoing) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status",
    [(TelegramSendUncertain("transport"), "uncertain"), (TelegramError("403"), "delivery_failed")],
)
async def test_failed_delivery_is_not_retried_or_claimed_success(bot, error, status):
    service, repository, gateway, telegram = bot
    telegram.errors = [error]
    await service.process(update())
    await service.process(update())
    assert len(telegram.calls) == 1
    assert repository.outgoing == []
    assert repository.statuses[-1][1] == status


@pytest.mark.asyncio
async def test_edits_are_ignored_without_overwriting_history(bot):
    service, repository, gateway, telegram = bot
    payload = update()
    payload["edited_message"] = payload.pop("message")
    await service.process(payload)
    assert repository.messages == gateway.calls == telegram.calls == []


@pytest.mark.asyncio
async def test_disabled_chat_persists_without_ai_or_reply(bot):
    service, repository, gateway, telegram = bot
    repository.accepted = replace(repository.accepted, bot_enabled=False)
    await service.process(update())
    assert len(repository.messages) == 1
    assert gateway.calls == telegram.calls == []


@pytest.mark.asyncio
async def test_outgoing_persistence_failure_marks_uncertain_without_resending(bot):
    service, repository, gateway, telegram = bot
    repository.outgoing_failure = True
    await service.process(update())
    await service.process(update())
    assert len(telegram.calls) == 1
    assert repository.outgoing == []
    assert repository.statuses[-1] == (1, "uncertain", "outgoing_persistence_failed")


@pytest.mark.asyncio
async def test_webhook_authentication_precedes_body_parsing(settings, bot):
    service, repository, gateway, telegram = bot
    app = create_app(settings, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        response = await client.post("/telegram/webhook", content="not json")
        assert response.status_code == 403
        response = await client.post(
            "/telegram/webhook", json=update(), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        )
        assert response.status_code == 403
        assert repository.messages == gateway.calls == telegram.calls == []


@pytest.mark.asyncio
async def test_valid_webhook_replay_and_malformed_input(settings, bot):
    service, repository, gateway, telegram = bot
    app = create_app(settings, service)
    headers = {
        "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret.get_secret_value()
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post("/telegram/webhook", json=update(), headers=headers)
            assert response.status_code == 200
        response = await client.post(
            "/telegram/webhook", content="INVALID SECRET input", headers=headers
        )
        assert response.status_code == 400
        assert "SECRET" not in response.text
        response = await client.post("/telegram/webhook", content=b"x" * 1_000_001, headers=headers)
        assert response.status_code == 413
    assert len(gateway.calls) == len(telegram.calls) == 1


@pytest.mark.asyncio
async def test_persistence_failure_returns_retryable_without_ai_or_secret_leak(settings, bot):
    service, repository, gateway, telegram = bot
    repository.failure = True
    app = create_app(settings, service)
    headers = {
        "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret.get_secret_value()
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/telegram/webhook", json=update(), headers=headers)
        assert response.status_code == 503
        assert "SECRET" not in response.text
        assert gateway.calls == telegram.calls == []
        repository.failure = False
        response = await client.post("/telegram/webhook", json=update(), headers=headers)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_request_timeout_records_uncertainty_without_replay(settings, bot):
    service, repository, gateway, telegram = bot
    gateway.delay = 1
    config = settings.model_copy(update={"webhook_timeout_seconds": 0.01})
    app = create_app(config, service)
    headers = {
        "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret.get_secret_value()
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post("/telegram/webhook", json=update(), headers=headers)
        ).status_code == 503
        assert (
            await client.post("/telegram/webhook", json=update(), headers=headers)
        ).status_code == 200
    assert len(gateway.calls) == 1
    assert telegram.calls == []
    assert repository.statuses[-1] == (1, "uncertain", "request_cancelled")


def test_structured_formatter_does_not_serialize_exception_details():
    import logging

    record = logging.LogRecord(
        "app",
        logging.ERROR,
        "",
        1,
        "webhook_failed",
        (),
        (RuntimeError, RuntimeError("SECRET database password"), None),
    )
    record.update_id = 1
    record.error_type = "RuntimeError"
    output = JSONFormatter().format(record)
    assert "SECRET" not in output
    assert json.loads(output)["update_id"] == 1


@pytest.mark.asyncio
async def test_startup_and_cleanup_close_all_resources_even_after_close_failure(settings):
    connection = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def connect():
        yield connection

    database = SimpleNamespace(engine=SimpleNamespace(connect=connect), close=AsyncMock())
    telegram = SimpleNamespace(
        get_me=AsyncMock(return_value={"id": 42, "username": "groupbot"}), close=AsyncMock()
    )
    gateway = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("SECRET cleanup details")))
    with (
        patch("app.main.Database", return_value=database),
        patch("app.main.TelegramClient", return_value=telegram),
        patch("app.main.GoogleGeminiGateway", return_value=gateway),
    ):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.service, TelegramService)
    gateway.close.assert_awaited_once()
    telegram.close.assert_awaited_once()
    database.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_error_is_sanitized_and_closes_resources(settings):
    @asynccontextmanager
    async def connect():
        raise RuntimeError("SECRET database password URL")
        yield

    database = SimpleNamespace(engine=SimpleNamespace(connect=connect), close=AsyncMock())
    telegram = SimpleNamespace(get_me=AsyncMock(), close=AsyncMock())
    with (
        patch("app.main.Database", return_value=database),
        patch("app.main.TelegramClient", return_value=telegram),
        patch("app.main.GoogleGeminiGateway") as provider,
    ):
        app = create_app(settings)
        with pytest.raises(RuntimeError) as error:
            async with app.router.lifespan_context(app):
                pass
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__
    telegram.get_me.assert_not_awaited()
    provider.assert_not_called()
    telegram.close.assert_awaited_once()
    database.close.assert_awaited_once()
