"""Memory commands stay database-only and confirmations use committed outcomes."""

from dataclasses import replace
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.repositories.bot import AcceptedMessage
from app.schemas.ai import AssistantContext, AssistantResponse, GeminiTurn, ToolCall, ToolResult
from app.schemas.memory import MemoryEntry
from app.services.ai_orchestrator import AIOrchestrator
from app.services.gemini_gateway import GeminiError
from app.services.memory_renderer import render_assistant_response, render_memory_page
from app.services.telegram_service import TelegramService


def memory(content="Morning flights", *, user_id=None, status="active"):
    return MemoryEntry(
        id=uuid4(),
        scope="user" if user_id else "group",
        user_id=user_id,
        member_name="Anna" if user_id else None,
        category="preference",
        content=content,
        normalized_key="flight_time",
        importance=5,
        status=status,
        source_message_id=uuid4(),
    )


def update(text, *, anonymous=False):
    message = {
        "message_id": 5,
        "date": 1,
        "chat": {"id": -1001, "type": "supergroup"},
        "from": {"id": 99, "first_name": "Jian"},
        "text": text,
    }
    if anonymous:
        message["sender_chat"] = message["chat"]
    return {"update_id": 1, "message": message}


@pytest.fixture
def command_bot(settings):
    accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
    repository = SimpleNamespace(
        accept_message=AsyncMock(return_value=accepted),
        reserve_ai_request=AsyncMock(side_effect=AssertionError("Commands must not reserve AI")),
        record_ai_run=AsyncMock(),
        save_outgoing=AsyncMock(),
        finish_update=AsyncMock(),
    )
    context = SimpleNamespace(
        build_context=AsyncMock(side_effect=AssertionError("Commands must not build AI context"))
    )
    orchestrator = SimpleNamespace(
        respond=AsyncMock(side_effect=AssertionError("Commands must not call Gemini"))
    )
    telegram = SimpleNamespace(
        send_message=AsyncMock(return_value={"message_id": 6, "date": 1, "chat": {"id": -1001}})
    )
    memories = SimpleNamespace(list_page=AsyncMock(return_value=([memory()], 21)))
    service = TelegramService(
        settings, repository, context, orchestrator, telegram, 42, "groupbot", memories
    )
    return SimpleNamespace(
        service=service,
        repository=repository,
        context=context,
        orchestrator=orchestrator,
        telegram=telegram,
        memories=memories,
        accepted=accepted,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,personal,page",
    [("/memory", False, 1), ("/memory 2", False, 2), ("/memory_me@GroupBot 2", True, 2)],
)
async def test_memory_commands_use_current_chat_and_personal_identity(
    command_bot, command, personal, page
):
    bot = command_bot
    await bot.service.process(update(command))
    bot.memories.list_page.assert_awaited_once_with(
        bot.accepted.chat_id, bot.accepted.user_id if personal else None, page
    )
    bot.context.build_context.assert_not_awaited()
    bot.orchestrator.respond.assert_not_awaited()
    bot.repository.reserve_ai_request.assert_not_awaited()
    bot.repository.record_ai_run.assert_not_awaited()
    bot.repository.save_outgoing.assert_awaited_once()
    bot.repository.finish_update.assert_awaited_once_with(1, "completed", None)
    sent = unescape(bot.telegram.send_message.await_args.args[1])
    assert "Morning flights" in sent
    assert f"Page {page}/2" in sent
    assert str(bot.accepted.chat_id) not in sent
    assert str(bot.accepted.user_id) not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["0", "-1", "2.5", "next", "1 2", "１２", "123456789"])
async def test_invalid_page_never_queries_memory_or_calls_ai(command_bot, page):
    await command_bot.service.process(update("/memory " + page))
    command_bot.memories.list_page.assert_not_awaited()
    command_bot.orchestrator.respond.assert_not_awaited()
    assert "positive page number" in command_bot.telegram.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_anonymous_personal_command_requires_identity_but_group_command_is_shared(
    command_bot,
):
    bot = command_bot
    bot.repository.accept_message.return_value = replace(bot.accepted, user_id=None)
    await bot.service.process(update("/memory_me", anonymous=True))
    bot.memories.list_page.assert_not_awaited()
    assert "personal Telegram identity" in bot.telegram.send_message.await_args.args[1]
    await bot.service.process(update("/memory", anonymous=True))
    bot.memories.list_page.assert_awaited_once_with(bot.accepted.chat_id, None, 1)
    bot.orchestrator.respond.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_command_addressed_to_other_bot_stays_silent(command_bot):
    await command_bot.service.process(update("/memory@otherbot"))
    command_bot.repository.accept_message.assert_awaited_once()
    command_bot.memories.list_page.assert_not_awaited()
    command_bot.telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_out_of_range_page_reports_available_range(command_bot):
    command_bot.memories.list_page.return_value = ([], 21)
    await command_bot.service.process(update("/memory 3"))
    assert "between 1 and 2" in command_bot.telegram.send_message.await_args.args[1]
    command_bot.orchestrator.respond.assert_not_awaited()


def test_memory_page_distinguishes_shared_personal_facts_and_hides_ids():
    group = memory()
    personal = memory("Vegetarian", user_id=uuid4())
    text = render_memory_page([group, personal], 21, 1, False)
    assert "Group memory" in text
    assert "Personal memory (shared in this group)" in text
    assert "Anna: Vegetarian" in text
    assert "Next: /memory 2" in text
    assert str(group.id) not in text
    assert str(personal.user_id) not in text


@pytest.mark.parametrize("personal", [False, True])
def test_empty_memory_page_uses_appropriate_group_or_personal_message(personal):
    text = render_memory_page([], 0, 1, personal)
    assert "No active memories" in text
    assert ("about you" in text) is personal


@pytest.mark.parametrize(
    "action,status,label",
    [
        ("saved", "active", "Remembered"),
        ("updated", "active", "Updated"),
        ("deleted", "deleted", "Forgot"),
        ("unchanged", "active", "Already remembered"),
        ("unchanged", "deleted", "Already forgotten"),
    ],
)
def test_confirmations_reflect_memory_outcome_without_internal_ids(action, status, label):
    entry = memory(status=status)
    outcome = {"action": action, "memory": entry.as_dict()}
    text = render_assistant_response(AssistantResponse("", memory_actions=[outcome, outcome]))
    assert text.count(f"{label} (Group): Morning flights") == 1
    assert str(entry.id) not in text
    if status == "deleted":
        assert "Already remembered" not in text


@pytest.mark.asyncio
async def test_provider_failure_still_delivers_confirmed_mutation(command_bot):
    bot = command_bot
    outcome = {"action": "saved", "memory": memory().as_dict()}

    class Executor:
        declarations = []

        def __init__(self):
            self.actions = []

        async def execute(self, call):
            self.actions.append(outcome)
            return ToolResult(call.name, {"ok": True, **outcome}, call.call_id)

    executor = Executor()
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(None, [ToolCall("save_memory", {}, "saved-call")], 10, 2),
                GeminiError("provider_error"),
            ]
        )
    )
    bot.service.orchestrator = AIOrchestrator(gateway, "rules", lambda _: executor)
    bot.repository.reserve_ai_request = AsyncMock(return_value=True)
    bot.context.build_context = AsyncMock(
        return_value=AssistantContext(bot.accepted.chat_id, "Jian", "Remember morning flights")
    )
    await bot.service.process(update("/ask Remember morning flights"))
    text = unescape(bot.telegram.send_message.await_args.args[1])
    assert "Remembered (Group): Morning flights" in text
    assert "memory changes above are confirmed" in text
    assert "couldn't complete the remaining answer" in text
    bot.repository.save_outgoing.assert_awaited_once()
    assert bot.repository.record_ai_run.await_args.kwargs["status"] == "failed"
    assert bot.repository.record_ai_run.await_args.kwargs["used_custom_functions"] is True
    bot.repository.finish_update.assert_awaited_once_with(1, "ai_failed", "provider_error")
