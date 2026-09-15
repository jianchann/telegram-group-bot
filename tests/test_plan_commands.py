"""Plan commands are DB-only and render compact current-chat state."""

from datetime import date
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.repositories.bot import AcceptedMessage
from app.services.plan_renderer import (
    render_assistant_response,
    render_plan_candidates,
    render_plan_detail,
    render_plan_page,
    render_task_page,
)
from app.services.telegram_service import TelegramService


def plan(name="Seoul Trip", *, items=None, members=None, artifacts=None, total_item_count=None):
    entries = items or []
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        description="Group trip",
        plan_type="trip",
        status="active",
        start_date=date(2027, 1, 15),
        end_date=date(2027, 1, 19),
        members=members or [],
        items=entries,
        artifacts=artifacts or [],
        total_artifact_count=len(artifacts or []),
        total_item_count=len(entries) if total_item_count is None else total_item_count,
    )


def item(kind, title, *, status="open", assigned_name=None):
    return SimpleNamespace(
        id=uuid4(),
        item_type=kind,
        title=title,
        status=status,
        assigned_name=assigned_name,
        due_at=None,
        metadata={},
    )


def update(text):
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "date": 1,
            "chat": {"id": -1001, "type": "supergroup"},
            "from": {"id": 99, "first_name": "Jian"},
            "text": text,
        },
    }


@pytest.fixture
def command_bot(settings):
    accepted = AcceptedMessage(uuid4(), uuid4(), uuid4(), 5)
    repository = SimpleNamespace(
        accept_message=AsyncMock(return_value=accepted),
        reserve_ai_request=AsyncMock(side_effect=AssertionError("Plan commands must not use AI")),
        record_ai_run=AsyncMock(),
        save_outgoing=AsyncMock(),
        finish_update=AsyncMock(),
    )
    context = SimpleNamespace(
        build_context=AsyncMock(
            side_effect=AssertionError("Plan commands must not build AI context")
        )
    )
    orchestrator = SimpleNamespace(
        respond=AsyncMock(side_effect=AssertionError("Plan commands must not call Gemini"))
    )
    telegram = SimpleNamespace(
        send_message=AsyncMock(return_value={"message_id": 6, "date": 1, "chat": {"id": -1001}})
    )
    summary = plan()
    detail = plan(items=[item("constraint", "Hotel under PHP 8,000")])
    resolution = SimpleNamespace(plan=summary, candidates=[], reason="exact")
    plans = SimpleNamespace(
        list_page=AsyncMock(return_value=([summary], 21)),
        list_tasks=AsyncMock(
            return_value=(
                [
                    SimpleNamespace(
                        id=uuid4(),
                        plan_name="Seoul Trip",
                        title="Book KTX",
                        status="open",
                        assigned_name="Jian",
                        due_at=None,
                    )
                ],
                21,
            )
        ),
        resolve=AsyncMock(return_value=resolution),
        get=AsyncMock(return_value=detail),
    )
    service = TelegramService(
        settings,
        repository,
        context,
        orchestrator,
        telegram,
        42,
        "groupbot",
        plan_service=plans,
    )
    return SimpleNamespace(
        service=service,
        repository=repository,
        context=context,
        orchestrator=orchestrator,
        telegram=telegram,
        plans=plans,
        accepted=accepted,
        summary=summary,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command,page", [("/plans", 1), ("/plans 2", 2), ("/plans@GroupBot 2", 2)])
async def test_plans_lists_active_current_chat_without_ai(command_bot, command, page):
    bot = command_bot
    await bot.service.process(update(command))
    bot.plans.list_page.assert_awaited_once_with(
        bot.accepted.chat_id, status="active", page=page, page_size=20
    )
    bot.context.build_context.assert_not_awaited()
    bot.orchestrator.respond.assert_not_awaited()
    bot.repository.reserve_ai_request.assert_not_awaited()
    bot.repository.record_ai_run.assert_not_awaited()
    bot.repository.save_outgoing.assert_awaited_once()
    text = unescape(bot.telegram.send_message.await_args.args[1])
    assert "Seoul Trip" in text
    assert f"Page {page}/2" in text
    assert str(bot.summary.id) not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["0", "-1", "2.5", "next", "1 2", "１２", "123456789"])
async def test_invalid_plans_page_does_not_query_or_call_ai(command_bot, page):
    await command_bot.service.process(update("/plans " + page))
    command_bot.plans.list_page.assert_not_awaited()
    command_bot.orchestrator.respond.assert_not_awaited()
    assert "positive page number" in command_bot.telegram.send_message.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,expected_request", [("/plan", ""), ("/plan Seoul Trip", "Seoul Trip")]
)
async def test_plan_resolves_then_loads_capped_detail_without_ai(
    command_bot, command, expected_request
):
    bot = command_bot
    await bot.service.process(update(command))
    bot.plans.resolve.assert_awaited_once_with(
        bot.accepted.chat_id, request=expected_request, reply_text=None, recent_texts=[]
    )
    bot.plans.get.assert_awaited_once_with(bot.accepted.chat_id, bot.summary.id, item_limit=50)
    bot.orchestrator.respond.assert_not_awaited()
    bot.repository.reserve_ai_request.assert_not_awaited()
    text = unescape(bot.telegram.send_message.await_args.args[1])
    assert "Hotel under PHP 8,000" in text
    assert str(bot.summary.id) not in text


@pytest.mark.asyncio
async def test_ambiguous_plan_lists_candidate_names_without_loading_detail(command_bot):
    bot = command_bot
    bot.plans.resolve.return_value = SimpleNamespace(
        plan=None, candidates=[plan("Seoul Trip"), plan("Seoul Work Trip")], reason="ambiguous"
    )
    await bot.service.process(update("/plan Seoul"))
    bot.plans.get.assert_not_awaited()
    bot.orchestrator.respond.assert_not_awaited()
    text = unescape(bot.telegram.send_message.await_args.args[1])
    assert "Which active plan" in text
    assert "Seoul Trip" in text and "Seoul Work Trip" in text


@pytest.mark.asyncio
async def test_plan_command_addressed_to_other_bot_stays_silent(command_bot):
    await command_bot.service.process(update("/plans@otherbot"))
    command_bot.plans.list_page.assert_not_awaited()
    command_bot.plans.resolve.assert_not_awaited()
    command_bot.telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,mine,page",
    [
        ("/tasks", False, 1),
        ("/tasks 2", False, 2),
        ("/tasks mine", True, 1),
        ("/tasks mine 2", True, 2),
    ],
)
async def test_tasks_lists_group_or_personal_tasks_without_ai(command_bot, command, mine, page):
    bot = command_bot
    await bot.service.process(update(command))
    bot.plans.list_tasks.assert_awaited_once_with(
        bot.accepted.chat_id,
        page=page,
        page_size=20,
        assigned_user_id=bot.accepted.user_id if mine else None,
    )
    bot.orchestrator.respond.assert_not_awaited()
    bot.repository.reserve_ai_request.assert_not_awaited()
    text = unescape(bot.telegram.send_message.await_args.args[1])
    assert "Book KTX" in text and "Seoul Trip" in text
    assert f"Page {page}/2" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["mine 0", "all", "mine two", "1 2"])
async def test_invalid_tasks_request_does_not_query(command_bot, arguments):
    await command_bot.service.process(update("/tasks " + arguments))
    command_bot.plans.list_tasks.assert_not_awaited()
    assert "Use /tasks" in command_bot.telegram.send_message.await_args.args[1]


def test_plan_page_and_not_found_results_hide_internal_ids():
    entry = plan()
    text = render_plan_page([entry], 21, 1)
    assert "Next: /plans 2" in text
    assert str(entry.id) not in text
    assert "Use /plans" in render_plan_candidates([])


def test_task_page_renders_compact_rows_without_ids():
    task = SimpleNamespace(
        id=uuid4(),
        plan_name="Seoul Trip",
        title="Book KTX",
        status="in_progress",
        assigned_name="Anna",
        due_at=date(2027, 1, 2),
    )
    text = render_task_page([task], 21, 1, False)
    assert "[Seoul Trip] Book KTX — in progress · Anna · 2027-01-02" in text
    assert "Next: /tasks 2" in text
    assert str(task.id) not in text


def test_detail_groups_items_caps_at_fifty_and_reports_omitted_count():
    items = [
        item("decision", "Stay in Hongdae", status="confirmed"),
        item("constraint", "One hotel", status="active"),
        item("task", "Book KTX", assigned_name="Anna"),
        item("activity", "Palace visit", status="shortlisted"),
        item("open_question", "Airport transfer?"),
        item("note", "Bring adapters"),
        *(item("task", f"Extra {index}") for index in range(46)),
    ]
    entry = plan(items=items)
    text = render_plan_detail(entry)
    for heading in ("Decisions", "Constraints", "Tasks", "Activities", "Open questions", "Notes"):
        assert heading in text
    assert "Book KTX — open · Anna" in text
    assert "2 more items omitted." in text
    assert "Extra 45" not in text
    assert str(entry.id) not in text
    assert all(str(value.id) not in text for value in items)


def test_detail_groups_saved_links_without_exposing_artifact_ids():
    artifact = SimpleNamespace(
        id=uuid4(), title="Preferred hotel", url="https://example.com/hotel", category="Stay"
    )
    text = render_plan_detail(plan(artifacts=[artifact]))
    assert "Saved links\nStay\n• Preferred hotel — https://example.com/hotel" in text
    assert str(artifact.id) not in text


def test_detail_shows_status_active_participants_and_decision_rationale():
    decision = item("decision", "Choose Zhongshan", status="confirmed")
    decision.metadata = {
        "selected": "Zhongshan",
        "reason": "Quieter with easy MRT access",
        "alternatives": ["Ximending"],
    }
    members = [
        SimpleNamespace(display_name="Anna", role="traveler", status="active"),
        SimpleNamespace(display_name="Mike", role=None, status="removed"),
    ]
    text = render_plan_detail(plan(items=[decision], members=members))
    assert "trip · active" in text
    assert "Participants\n• Anna — traveler" in text
    assert "Mike" not in text
    assert "Selected: Zhongshan" in text
    assert "Reason: Quieter with easy MRT access" in text
    assert "Alternatives: Ximending" in text


def test_confirmed_plan_actions_are_deduplicated_and_survive_provider_failure():
    action = {
        "kind": "plan_item",
        "action": "added",
        "entity": {"id": str(uuid4()), "title": "Book KTX"},
    }
    response = SimpleNamespace(
        text="Provider failed",
        error_code="provider_error",
        memory_actions=[],
        plan_actions=[action, action],
    )
    text = render_assistant_response(response)
    assert text.count("Added: Book KTX") == 1
    assert "changes above are confirmed" in text
    assert action["entity"]["id"] not in text
    assert "Provider failed" not in text


@pytest.mark.parametrize(
    "kind,action,entity,expected",
    [
        ("plan", "created", {"name": "Seoul Trip"}, "Created plan: Seoul Trip"),
        (
            "plan",
            "updated",
            {"name": "Seoul Trip", "status": "completed"},
            "Completed plan: Seoul Trip",
        ),
        ("plan_item", "deleted", {"title": "Book KTX"}, "Removed: Book KTX"),
        (
            "plan_member",
            "updated",
            {"display_name": "Anna"},
            "Updated participant: Anna",
        ),
        (
            "plan_member",
            "unchanged",
            {"display_name": "Anna"},
            "Participant already set: Anna",
        ),
    ],
)
def test_plan_confirmation_label_depends_on_entity_kind(kind, action, entity, expected):
    response = SimpleNamespace(
        text="Done",
        error_code=None,
        memory_actions=[],
        plan_actions=[{"kind": kind, "action": action, "entity": entity}],
    )
    assert expected in render_assistant_response(response)


def test_memory_confirmation_behavior_is_preserved_with_plan_renderer():
    response = SimpleNamespace(
        text="Done",
        error_code=None,
        plan_actions=[],
        memory_actions=[
            {
                "action": "saved",
                "memory": {"id": str(uuid4()), "scope": "group", "content": "Morning flights"},
            }
        ],
    )
    text = render_assistant_response(response)
    assert "Remembered (Group): Morning flights" in text
    assert text.endswith("Done")


def test_confirmed_poll_action_is_rendered_without_internal_ids():
    response = SimpleNamespace(
        text="",
        error_code=None,
        plan_actions=[],
        memory_actions=[],
        telegram_actions=[
            {
                "kind": "telegram_poll",
                "action": "created",
                "entity": {"question": "Which hotel?", "poll_id": "hidden"},
            }
        ],
    )
    text = render_assistant_response(response)
    assert text == "Created poll: Which hotel?"
    assert "hidden" not in text


def test_mixed_memory_and_plan_confirmations_survive_provider_failure():
    response = SimpleNamespace(
        text="Provider failed",
        error_code="provider_error",
        memory_actions=[
            {
                "action": "saved",
                "memory": {"id": str(uuid4()), "scope": "group", "content": "Morning flights"},
            }
        ],
        plan_actions=[{"kind": "plan_item", "action": "created", "entity": {"title": "Book KTX"}}],
    )
    text = render_assistant_response(response)
    assert "Remembered (Group): Morning flights" in text
    assert "Added: Book KTX" in text
    assert text.count("changes above are confirmed") == 1
    assert "Provider failed" not in text
