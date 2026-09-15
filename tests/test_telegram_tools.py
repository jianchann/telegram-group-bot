"""Native Telegram actions require direct intent and confirmed persistence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.ai.tools.telegram import TelegramToolExecutor
from app.schemas.ai import AssistantContext, ToolCall
from app.services.telegram_action_service import TelegramActionError, TelegramActionService
from app.services.telegram_client import TelegramSendUncertain


def executor(message="@groupbot let's vote on a hotel"):
    context = AssistantContext(
        chat_id=uuid4(),
        current_user="Jian",
        current_message=message,
        current_user_id=uuid4(),
        current_message_id=uuid4(),
    )
    service = SimpleNamespace(
        create_poll=AsyncMock(return_value={"message_id": 9, "poll": {"id": "poll-1"}})
    )
    return context, service, TelegramToolExecutor(context, service)


@pytest.mark.asyncio
async def test_create_poll_uses_bound_chat_and_records_only_confirmed_action():
    context, service, tool = executor()
    result = await tool.execute(
        ToolCall(
            "create_poll",
            {"question": "Which hotel?", "options": ["A", "B"]},
            "call-1",
        )
    )
    assert result.result["ok"] is True
    service.create_poll.assert_awaited_once_with(
        context.chat_id,
        context.current_user_id,
        context.current_message_id,
        "Which hotel?",
        ["A", "B"],
        False,
        False,
    )
    assert tool.actions == [
        {
            "kind": "telegram_poll",
            "action": "created",
            "entity": {"question": "Which hotel?"},
        }
    ]
    repeated = await tool.execute(
        ToolCall("create_poll", {"question": "Which hotel?", "options": ["A", "B"]})
    )
    assert repeated.result == {"ok": False, "error": "poll_already_attempted"}
    assert service.create_poll.await_count == 1


@pytest.mark.asyncio
async def test_poll_rejects_missing_direct_intent_and_duplicate_options():
    _, service, tool = executor("Here is an example: create a poll")
    quoted = await tool.execute(
        ToolCall("create_poll", {"question": "Where?", "options": ["A", "B"]})
    )
    duplicate = await tool.execute(
        ToolCall("create_poll", {"question": "Where?", "options": ["Hotel", "hotel"]})
    )
    assert quoted.result == {"ok": False, "error": "explicit_poll_intent_required"}
    assert duplicate.result == {"ok": False, "error": "invalid_tool_arguments"}
    service.create_poll.assert_not_awaited()
    assert not tool.actions


@pytest.mark.asyncio
async def test_uncertain_poll_delivery_is_not_confirmed():
    _, service, tool = executor()
    service.create_poll.side_effect = TelegramSendUncertain("transport")
    result = await tool.execute(
        ToolCall("create_poll", {"question": "Where?", "options": ["A", "B"]})
    )
    assert result.result == {
        "ok": False,
        "error": "transport",
        "delivery_uncertain": True,
    }
    assert not tool.actions


@pytest.mark.asyncio
async def test_action_service_resolves_chat_and_persists_confirmed_poll():
    chat_id = uuid4()
    actor_id = uuid4()
    source_id = uuid4()
    outgoing = {"message_id": 9, "poll": {"id": "poll-1"}}
    repository = SimpleNamespace(
        telegram_chat_id=AsyncMock(return_value=-1001),
        save_outgoing=AsyncMock(),
    )
    telegram = SimpleNamespace(send_poll=AsyncMock(return_value=outgoing))
    result = await TelegramActionService(repository, telegram).create_poll(
        chat_id, actor_id, source_id, "Where?", ["A", "B"]
    )
    assert result is outgoing
    repository.telegram_chat_id.assert_awaited_once_with(chat_id, actor_id, source_id)
    telegram.send_poll.assert_awaited_once_with(-1001, "Where?", ["A", "B"], False, False)
    repository.save_outgoing.assert_awaited_once_with(chat_id, outgoing)


@pytest.mark.asyncio
async def test_action_service_never_sends_for_unknown_chat():
    repository = SimpleNamespace(
        telegram_chat_id=AsyncMock(return_value=None),
        save_outgoing=AsyncMock(),
    )
    telegram = SimpleNamespace(send_poll=AsyncMock())
    with pytest.raises(TelegramActionError, match="chat_not_found"):
        await TelegramActionService(repository, telegram).create_poll(
            uuid4(), uuid4(), uuid4(), "Where?", ["A", "B"]
        )
    telegram.send_poll.assert_not_awaited()
