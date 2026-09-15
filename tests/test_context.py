import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.repositories.bot import AcceptedMessage, MessageRecord
from app.schemas.plan import PlanEntry, PlanResolution
from app.services.context_service import ContextService, ContextTooLarge


class History:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def recent_messages(self, chat_id, before_message_id, limit):
        self.calls.append((chat_id, before_message_id, limit))
        return self.rows[-limit:]


def row(message_id, text, speaker="Anna", bot=False):
    return MessageRecord(message_id, text, speaker, bot, datetime.now(UTC))


@pytest.mark.asyncio
async def test_history_order_speakers_current_request_and_chat_scope():
    history = History([row(1, "Seoul in November"), row(2, "Keep it quiet"), row(3, None)])
    accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
    context = await ContextService(history, 30, 24000).build_context(
        accepted, {"from": {"first_name": "Jian"}}, "Where should we stay?"
    )
    prompt = json.loads(context.prompt())
    assert [item["text"] for item in prompt["recent_group_context"]] == [
        "Seoul in November",
        "Keep it quiet",
    ]
    assert prompt["current_request"] == {"speaker": "Jian", "text": "Where should we stay?"}
    assert prompt["memories"] == []
    assert prompt["active_plan"] is None
    assert history.calls == [(accepted.chat_id, 5, 30)]


@pytest.mark.asyncio
async def test_bounded_context_drops_oldest_first_and_preserves_request():
    history = History([row(1, "x" * 500), row(2, "Latest idea")])
    accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
    context = await ContextService(history, 30, 500).build_context(accepted, {}, "Which idea?")
    assert len(context.prompt()) <= 500
    assert [item.message_id for item in context.recent_messages] == [2]
    assert context.current_message == "Which idea?"


@pytest.mark.asyncio
async def test_older_explicit_reply_is_available_but_not_duplicated():
    history = History([row(4, "Unrelated topic")])
    accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
    message = {
        "reply_to_message": {"message_id": 1, "text": "Hotel A", "from": {"first_name": "Anna"}}
    }
    context = await ContextService(history, 30, 24000).build_context(
        accepted, message, "Compare it"
    )
    assert context.reply_message.text == "Hotel A"
    history.rows = [row(1, "Hotel A")]
    context = await ContextService(history, 30, 24000).build_context(
        accepted, message, "Compare it"
    )
    assert context.reply_message is None


@pytest.mark.asyncio
async def test_oversized_reply_is_dropped_and_oversized_request_rejected():
    history = History([])
    accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
    message = {"reply_to_message": {"message_id": 1, "text": "x" * 10000}}
    builder = ContextService(history, 30, 500)
    context = await builder.build_context(accepted, message, "Question")
    assert context.reply_message is None
    assert len(context.prompt()) <= 500
    with pytest.raises(ContextTooLarge):
        await builder.build_context(accepted, {}, '"' * 500)
    assert len(history.calls) == 1


@pytest.mark.asyncio
async def test_resolved_plan_and_chat_clock_are_added_to_context():
    history = History([row(1, "The Seoul Trip hotel should be quiet")])
    accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
    summary = PlanEntry(
        id=uuid4(),
        chat_id=accepted.chat_id,
        name="Seoul Trip",
        description=None,
        plan_type="trip",
        status="active",
        start_date=None,
        end_date=None,
        timezone="Asia/Singapore",
        metadata={},
        created_by_user_id=accepted.user_id,
    )
    detail = summary.model_copy(update={"total_item_count": 2})
    plans = SimpleNamespace(
        resolve=AsyncMock(
            return_value=PlanResolution(plan=summary, candidates=[summary], reason="recent")
        ),
        get=AsyncMock(return_value=detail),
        chat_timezone=AsyncMock(return_value="Asia/Singapore"),
    )
    context = await ContextService(history, 30, 24000, plan_service=plans).build_context(
        accepted, {}, "What is next?"
    )
    prompt = json.loads(context.prompt())
    assert prompt["active_plan"]["name"] == "Seoul Trip"
    assert "available_plans" not in prompt
    assert prompt["chat_timezone"] == "Asia/Singapore"
    assert date.fromisoformat(prompt["current_local_date"])
    plans.resolve.assert_awaited_once_with(
        accepted.chat_id,
        request="What is next?",
        reply_text=None,
        recent_texts=["The Seoul Trip hotel should be quiet"],
    )
    plans.get.assert_awaited_once_with(accepted.chat_id, summary.id, item_limit=50)
