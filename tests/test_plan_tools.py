from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.ai.tools.composite import CompositeToolExecutor
from app.ai.tools.plan import PlanToolExecutor
from app.schemas.ai import AssistantContext, ContextMessage, ToolCall, ToolDeclaration, ToolResult
from app.schemas.memory import MemberEntry, SourceMessage
from app.schemas.plan import (
    PlanArtifactEntry,
    PlanEntry,
    PlanItemEntry,
    PlanMemberEntry,
    PlanMutation,
)


def plan(name="Seoul Trip", items=None):
    return PlanEntry(
        id=uuid4(),
        chat_id=uuid4(),
        name=name,
        description=None,
        plan_type="trip",
        status="active",
        start_date=None,
        end_date=None,
        timezone=None,
        metadata={},
        created_by_user_id=None,
        members=[],
        items=items or [],
    )


def item(plan_id, title="Hotel under 8k", item_type="constraint"):
    return PlanItemEntry(
        id=uuid4(),
        plan_id=plan_id,
        item_type=item_type,
        title=title,
        description=None,
        status="active",
        assigned_user_id=None,
        assigned_name=None,
        due_at=None,
        category=None,
        position=None,
        metadata={},
        source_message_id=None,
    )


def setup(text="Show plans", active=None):
    actor, source_id = uuid4(), uuid4()
    context = AssistantContext(
        uuid4(),
        "Jian",
        text,
        current_user_id=actor,
        current_message_id=source_id,
        active_plan=active,
        members=[MemberEntry(id=actor, display_name="Jian", username="jian")],
        sources=[
            SourceMessage(
                id=source_id, telegram_message_id=1, text=text, is_bot_message=False, user_id=actor
            )
        ],
    )
    service = SimpleNamespace(
        list_page=AsyncMock(return_value=([], 0)),
        get=AsyncMock(),
        create=AsyncMock(),
        update=AsyncMock(),
        set_member=AsyncMock(),
        add_item=AsyncMock(),
        update_item=AsyncMock(),
        delete_item=AsyncMock(),
        save_artifact=AsyncMock(),
        list_artifacts=AsyncMock(return_value=[]),
    )
    return context, service, PlanToolExecutor(context, service)


@pytest.mark.asyncio
async def test_list_discovers_plan_before_get_and_preserves_chat_scope():
    context, service, executor = setup()
    target = plan()
    service.list_page.return_value = ([target], 1)
    service.get.return_value = target
    unknown = await executor.execute(ToolCall("get_plan", {"plan_id": str(target.id)}))
    assert unknown.result["error"] == "unknown_plan"
    listed = await executor.execute(ToolCall("list_plans", {}))
    assert listed.result["total"] == 1
    got = await executor.execute(ToolCall("get_plan", {"plan_id": str(target.id)}))
    assert got.result["plan"]["id"] == str(target.id)
    service.get.assert_awaited_once_with(context.chat_id, target.id)


@pytest.mark.asyncio
async def test_create_requires_direct_unquoted_intent_and_observed_participants():
    context, service, executor = setup('Example: "create a Seoul trip plan"')
    created = plan()
    service.create.return_value = PlanMutation(action="created", plan=created)
    call = ToolCall(
        "create_plan",
        {
            "name": "Seoul",
            "participant_ids": [str(context.current_user_id)],
            "source_quote": context.current_message,
        },
    )
    assert (await executor.execute(call)).result["error"] == "explicit_create_intent_required"
    service.create.assert_not_awaited()
    context.current_message = "Create a Seoul trip plan"
    context.sources[0] = context.sources[0].model_copy(update={"text": context.current_message})
    call.arguments["source_quote"] = context.current_message
    result = await executor.execute(call)
    assert result.result["kind"] == "plan"
    assert executor.actions[0]["entity"]["id"] == str(created.id)


@pytest.mark.asyncio
async def test_auto_capture_only_constraint_or_confirmed_decision_with_exact_evidence():
    target = plan()
    context, service, executor = setup("Hotel budget is 8000", target)
    new_item = item(target.id)
    service.add_item.return_value = PlanMutation(action="created", item=new_item)
    base = {
        "plan_id": str(target.id),
        "title": "Hotel under 8k",
        "source_quote": "Hotel budget is 8000",
    }
    note = await executor.execute(ToolCall("create_plan_item", {**base, "item_type": "note"}))
    assert note.result["error"] == "explicit_item_intent_required"
    constraint = await executor.execute(
        ToolCall("create_plan_item", {**base, "item_type": "constraint"})
    )
    assert constraint.result["ok"] is True
    assert service.add_item.await_args.args[:4] == (
        context.chat_id,
        context.current_user_id,
        context.current_message_id,
        target.id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Maybe we chose Seoul", 'The example says "we decided Seoul"'])
async def test_tentative_or_quoted_decision_is_not_auto_captured(text):
    target = plan()
    context, service, executor = setup(text, target)
    result = await executor.execute(
        ToolCall(
            "create_plan_item",
            {
                "plan_id": str(target.id),
                "item_type": "decision",
                "title": "Seoul",
                "source_quote": text,
            },
        )
    )
    assert result.result["error"] in {"unconfirmed_plan_fact", "invalid_conversational_evidence"}
    service.add_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_assignment_requires_direct_intent_and_known_member():
    target = plan()
    context, service, executor = setup("Jian likes hotels", target)
    call = ToolCall(
        "create_plan_item",
        {
            "plan_id": str(target.id),
            "item_type": "constraint",
            "title": "Book hotel",
            "source_quote": context.current_message,
            "assigned_user_id": str(context.current_user_id),
        },
    )
    assert (await executor.execute(call)).result["error"] == "explicit_assignment_intent_required"
    context.current_message = "Assign Jian to book the hotel"
    context.sources[0] = context.sources[0].model_copy(update={"text": context.current_message})
    service.add_item.return_value = PlanMutation(
        action="created", item=item(target.id, "Book hotel", "task")
    )
    call.arguments["source_quote"] = context.current_message
    assert (await executor.execute(call)).result["ok"] is True


@pytest.mark.asyncio
async def test_update_delete_require_known_item_and_direct_destructive_intent():
    target = plan()
    existing = item(target.id)
    target = target.model_copy(update={"items": [existing]})
    context, service, executor = setup("Do not delete the hotel constraint", target)
    call = ToolCall(
        "delete_plan_item", {"item_id": str(existing.id), "source_quote": context.current_message}
    )
    assert (await executor.execute(call)).result["error"] == "explicit_delete_intent_required"
    service.delete_item.assert_not_awaited()
    context.current_message = "Delete the hotel constraint"
    context.sources[0] = context.sources[0].model_copy(update={"text": context.current_message})
    call.arguments["source_quote"] = context.current_message
    service.delete_item.return_value = PlanMutation(action="deleted", item=existing)
    assert (await executor.execute(call)).result["ok"] is True
    assert executor.actions[-1]["kind"] == "plan_item"


@pytest.mark.asyncio
async def test_set_member_checks_current_intent_and_observed_identity():
    target = plan()
    context, service, executor = setup("Add Jian as a participant to the plan", target)
    service.set_member.return_value = PlanMutation(
        action="updated",
        plan=target,
        member=PlanMemberEntry(
            user_id=context.current_user_id, display_name="Jian", role="traveler", status="active"
        ),
    )
    result = await executor.execute(
        ToolCall(
            "set_plan_member",
            {
                "plan_id": str(target.id),
                "user_id": str(context.current_user_id),
                "role": "traveler",
                "status": "active",
                "source_quote": context.current_message,
            },
        )
    )
    assert result.result["kind"] == "plan_member"
    assert executor.actions[-1]["entity"]["user_id"] == str(context.current_user_id)
    service.set_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_strict_arguments_unknown_tools_and_dates():
    context, service, executor = setup("Create a Seoul trip plan")
    assert (await executor.execute(ToolCall("unknown", {}))).result["error"] == "unknown_tool"
    invalid = await executor.execute(
        ToolCall(
            "create_plan", {"name": "Seoul", "extra": True, "source_quote": context.current_message}
        )
    )
    assert invalid.result["error"] == "invalid_tool_arguments"
    created = plan().model_copy(update={"start_date": date(2027, 1, 2)})
    service.create.return_value = PlanMutation(action="created", plan=created)
    valid = await executor.execute(
        ToolCall(
            "create_plan",
            {"name": "Seoul", "start_date": "2027-01-02", "source_quote": context.current_message},
        )
    )
    assert valid.result["ok"] is True
    assert service.create.await_args.kwargs["start_date"] == date(2027, 1, 2)


@pytest.mark.asyncio
async def test_composite_routes_tools_combines_actions_and_rejects_duplicates():
    first = SimpleNamespace(
        declarations=[ToolDeclaration("one", "", {})],
        actions=[{"a": 1}],
        execute=AsyncMock(return_value=ToolResult("one", {"ok": True}, "id")),
    )
    second = SimpleNamespace(
        declarations=[ToolDeclaration("two", "", {})],
        actions=[{"b": 2}],
        execute=AsyncMock(return_value=ToolResult("two", {"ok": True})),
    )
    composite = CompositeToolExecutor(first, second)
    assert [tool.name for tool in composite.declarations] == ["one", "two"]
    assert composite.actions == [{"a": 1}, {"b": 2}]
    assert (await composite.execute(ToolCall("one", {}, "id"))).call_id == "id"
    assert (await composite.execute(ToolCall("missing", {}, "x"))).result["error"] == "unknown_tool"
    with pytest.raises(ValueError, match="duplicate"):
        CompositeToolExecutor(first, first)


@pytest.mark.asyncio
async def test_ambiguous_candidate_can_be_read_but_not_mutated():
    target = plan()
    context, service, executor = setup("Cancel the Seoul plan")
    context.plan_candidates = [target]
    executor = PlanToolExecutor(context, service)
    service.get.return_value = target
    assert (await executor.execute(ToolCall("get_plan", {"plan_id": str(target.id)}))).result[
        "ok"
    ] is True
    result = await executor.execute(
        ToolCall(
            "update_plan",
            {
                "plan_id": str(target.id),
                "status": "cancelled",
                "source_quote": context.current_message,
            },
        )
    )
    assert result.result["error"] == "ambiguous_plan"
    service.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_decision_accepts_only_reply_linked_human_confirmation():
    target = plan()
    context, service, executor = setup("Seoul or Busan?", target)
    context.sources.append(
        SourceMessage(
            id=uuid4(),
            telegram_message_id=2,
            reply_to_telegram_message_id=1,
            text="We decided Seoul",
            is_bot_message=False,
            user_id=uuid4(),
        )
    )
    service.add_item.return_value = PlanMutation(
        action="created", item=item(target.id, "Seoul", "decision")
    )
    result = await executor.execute(
        ToolCall(
            "create_plan_item",
            {
                "plan_id": str(target.id),
                "item_type": "decision",
                "title": "Seoul",
                "status": "confirmed",
                "source_quote": "Seoul or Busan?",
            },
        )
    )
    assert result.result["ok"] is True


@pytest.mark.asyncio
async def test_multiple_items_require_a_unique_textual_target():
    target = plan()
    hotel = item(target.id, "Book hotel", "task")
    flights = item(target.id, "Book flights", "task")
    target = target.model_copy(update={"items": [hotel, flights]})
    context, service, executor = setup("Mark that done", target)
    result = await executor.execute(
        ToolCall(
            "update_plan_item",
            {
                "item_id": str(hotel.id),
                "status": "done",
                "source_quote": context.current_message,
            },
        )
    )
    assert result.result["error"] == "ambiguous_plan_item"
    service.update_item.assert_not_awaited()

    context.current_message = "Mark the hotel task done"
    context.sources[0] = context.sources[0].model_copy(update={"text": context.current_message})
    service.update_item.return_value = PlanMutation(action="updated", item=hotel)
    call = ToolCall(
        "update_plan_item",
        {
            "item_id": str(hotel.id),
            "status": "done",
            "source_quote": context.current_message,
        },
    )
    assert (await executor.execute(call)).result["ok"] is True


@pytest.mark.asyncio
async def test_direct_mutation_cannot_use_older_source_as_authority():
    target = plan()
    context, service, executor = setup("Add a hotel task", target)
    older = SourceMessage(
        id=uuid4(),
        telegram_message_id=0,
        text="Hotel task",
        is_bot_message=False,
        user_id=context.current_user_id,
    )
    context.sources.append(older)
    result = await executor.execute(
        ToolCall(
            "create_plan_item",
            {
                "plan_id": str(target.id),
                "item_type": "task",
                "title": "Hotel",
                "source_message_id": str(older.id),
                "source_quote": older.text,
            },
        )
    )
    assert result.result["error"] == "explicit_intent_requires_current_evidence"
    service.add_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_save_requires_direct_intent_and_current_or_replied_url():
    target = plan()
    context, service, executor = setup("Save that link to the plan", target)
    linked_source = SourceMessage(
        id=uuid4(),
        telegram_message_id=3,
        text="Hotel https://Example.com:443/stay#rooms",
        is_bot_message=False,
        user_id=context.current_user_id,
    )
    context.sources.append(linked_source)
    context.reply_message = ContextMessage(
        message_id=3,
        speaker="Jian",
        text=linked_source.text,
        source_id=linked_source.id,
        user_id=context.current_user_id,
    )
    executor = PlanToolExecutor(context, service)
    artifact = PlanArtifactEntry(
        id=uuid4(),
        plan_id=target.id,
        title="Hotel",
        url="https://Example.com:443/stay#rooms",
        normalized_url="https://example.com/stay",
        category="stay",
        metadata={},
        source_message_id=linked_source.id,
        created_by_user_id=context.current_user_id,
        latest_actor_user_id=context.current_user_id,
    )
    service.save_artifact.return_value = PlanMutation(action="created", artifact=artifact)
    result = await executor.execute(
        ToolCall(
            "save_plan_artifact",
            {
                "plan_id": str(target.id),
                "url": artifact.url,
                "title": "Hotel",
                "category": "stay",
                "source_message_id": str(linked_source.id),
                "source_quote": linked_source.text,
            },
        )
    )
    assert result.result["kind"] == "plan_artifact"
    assert service.save_artifact.await_args.args[2:5] == (
        context.current_message_id,
        linked_source.id,
        target.id,
    )

    context.reply_message = None
    context.sources.append(
        SourceMessage(
            id=uuid4(),
            telegram_message_id=4,
            text="Newer link https://example.com/other",
            is_bot_message=False,
            user_id=context.current_user_id,
        )
    )
    executor = PlanToolExecutor(context, service)
    rejected = await executor.execute(
        ToolCall(
            "save_plan_artifact",
            {
                "plan_id": str(target.id),
                "url": artifact.url,
                "source_message_id": str(linked_source.id),
                "source_quote": linked_source.text,
            },
        )
    )
    assert rejected.result["error"] == "artifact_source_must_be_current_or_referenced"


@pytest.mark.asyncio
async def test_explicit_save_can_use_a_verified_current_research_url():
    target = plan()
    context, service, _ = setup("Find a hotel and save the best result", target)
    url = "https://example.com/researched-hotel"
    context.verified_research_urls = (url,)
    artifact = PlanArtifactEntry(
        id=uuid4(),
        plan_id=target.id,
        title="Researched hotel",
        url=url,
        normalized_url=url,
        category="stay",
        metadata={},
        source_message_id=context.current_message_id,
        created_by_user_id=context.current_user_id,
        latest_actor_user_id=context.current_user_id,
    )
    service.save_artifact.return_value = PlanMutation(action="created", artifact=artifact)
    result = await PlanToolExecutor(context, service).execute(
        ToolCall(
            "save_plan_artifact",
            {
                "plan_id": str(target.id),
                "url": url,
                "title": artifact.title,
                "category": artifact.category,
                "source_quote": context.current_message,
            },
        )
    )
    assert result.result["kind"] == "plan_artifact"
    assert service.save_artifact.await_args.args[3] == context.current_message_id


@pytest.mark.asyncio
async def test_artifact_save_rejects_negation_and_accepts_one_adjacent_link():
    target = plan()
    context, service, executor = setup("Don't save this link", target)
    url = "https://example.com/hotel"
    context.sources[0] = context.sources[0].model_copy(
        update={"text": f"Don't save this link {url}"}
    )
    rejected = await executor.execute(
        ToolCall(
            "save_plan_artifact",
            {
                "plan_id": str(target.id),
                "url": url,
                "source_quote": context.sources[0].text,
            },
        )
    )
    assert rejected.result["error"] == "explicit_artifact_save_intent_required"

    context.current_message = "Save that under Taiwan hotels"
    context.sources[0] = context.sources[0].model_copy(update={"text": context.current_message})
    adjacent = SourceMessage(
        id=uuid4(),
        telegram_message_id=2,
        text=url,
        is_bot_message=False,
        user_id=context.current_user_id,
    )
    context.sources.append(adjacent)
    artifact = PlanArtifactEntry(
        id=uuid4(),
        plan_id=target.id,
        title="Hotel",
        url=url,
        normalized_url=url,
        category="hotels",
        metadata={},
        source_message_id=adjacent.id,
        created_by_user_id=context.current_user_id,
        latest_actor_user_id=context.current_user_id,
    )
    service.save_artifact.return_value = PlanMutation(action="created", artifact=artifact)
    saved = await PlanToolExecutor(context, service).execute(
        ToolCall(
            "save_plan_artifact",
            {
                "plan_id": str(target.id),
                "url": url,
                "source_message_id": str(adjacent.id),
                "source_quote": url,
            },
        )
    )
    assert saved.result["kind"] == "plan_artifact"
