import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.ai.tools.memory import MemoryToolExecutor
from app.schemas.ai import AssistantContext, ToolCall
from app.schemas.memory import (
    MemberEntry,
    MemoryEntry,
    MemoryInfrastructureError,
    MemoryMutation,
    SourceMessage,
)


def memory(content="Morning flights", **kwargs):
    return MemoryEntry(
        id=kwargs.pop("id", uuid4()),
        scope=kwargs.pop("scope", "group"),
        user_id=kwargs.pop("user_id", None),
        category="preference",
        content=content,
        normalized_key=kwargs.pop("normalized_key", "flight.time"),
        importance=5,
        status="active",
        source_message_id=None,
        **kwargs,
    )


def setup(text="I prefer morning flights", *, memories=None):
    actor, source_id = uuid4(), uuid4()
    context = AssistantContext(
        uuid4(),
        "Jian",
        text,
        current_user_id=actor,
        current_message_id=source_id,
        members=[MemberEntry(id=actor, display_name="Jian", username="jian")],
        sources=[
            SourceMessage(
                id=source_id, telegram_message_id=10, text=text, is_bot_message=False, user_id=actor
            )
        ],
        memories=memories or [],
    )
    mutation = MemoryMutation(action="saved", memory=memory())
    service = SimpleNamespace(
        save=AsyncMock(return_value=mutation),
        update=AsyncMock(return_value=mutation),
        delete=AsyncMock(return_value=MemoryMutation(action="deleted", memory=mutation.memory)),
        search=AsyncMock(return_value=[]),
    )
    return context, service, MemoryToolExecutor(context, service)


def save_call(quote, **kwargs):
    return ToolCall(
        "save_memory",
        {
            "scope": "group",
            "content": "Morning flights",
            "source_quote": quote,
            "durability": "preference",
            **kwargs,
        },
        "call-id",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,quote",
    [
        ("I prefer morning flights, but maybe this is temporary", "I prefer morning flights"),
        ('The example says "I prefer morning flights"', "I prefer morning flights"),
        ("> I prefer morning flights", "I prefer morning flights"),
        ("`I prefer morning flights`", "I prefer morning flights"),
    ],
)
async def test_automatic_save_checks_entire_source_and_unquoted_evidence(text, quote):
    _, service, executor = setup(text)
    result = await executor.execute(save_call(quote))
    assert result.result == {"ok": False, "error": "not_a_durable_fact"}
    service.save.assert_not_awaited()
    assert executor.actions == []


@pytest.mark.asyncio
async def test_named_subject_takes_priority_over_first_person_prefix():
    context, service, executor = setup("I know Anna prefers aisle seats")
    anna = MemberEntry(id=uuid4(), display_name="Anna", username="anna")
    context.members.append(anna)
    wrong = await executor.execute(
        save_call(context.current_message, scope="user", user_id=str(context.current_user_id))
    )
    assert wrong.result["error"] == "ambiguous_member"
    service.save.assert_not_awaited()
    right = await executor.execute(
        save_call(context.current_message, scope="user", user_id=str(anna.id))
    )
    assert right.result["ok"] is True
    assert service.save.await_args.kwargs["user_id"] == anna.id


@pytest.mark.asyncio
async def test_personal_self_fact_is_bound_to_source_speaker():
    context, service, executor = setup()
    result = await executor.execute(
        save_call(context.current_message, scope="user", user_id=str(context.current_user_id))
    )
    assert result.result["ok"] is True
    assert service.save.await_args.kwargs["actor_id"] == context.current_user_id
    assert service.save.await_args.kwargs["source_id"] == context.current_message_id


@pytest.mark.asyncio
async def test_duplicate_first_names_require_disambiguation():
    context, service, executor = setup("Anna prefers aisle seats")
    anna = MemberEntry(id=uuid4(), display_name="Anna Smith", username="smith")
    context.members.extend([anna, MemberEntry(id=uuid4(), display_name="Anna Lee", username="lee")])
    result = await executor.execute(
        save_call(context.current_message, scope="user", user_id=str(anna.id))
    )
    assert result.result["error"] == "ambiguous_member"
    service.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "linked,is_bot,allowed", [(False, False, False), (True, True, False), (True, False, True)]
)
async def test_decision_requires_human_confirmation_linked_to_actual_source(
    linked, is_bot, allowed
):
    context, service, executor = setup("Destination Seoul")
    context.sources.append(
        SourceMessage(
            id=uuid4(),
            telegram_message_id=11,
            text="Agreed",
            is_bot_message=is_bot,
            user_id=uuid4(),
            reply_to_telegram_message_id=10 if linked else 999,
        )
    )
    result = await executor.execute(save_call("Destination Seoul", durability="decision"))
    assert result.result["ok"] is allowed
    if allowed:
        service.save.assert_awaited_once()
    else:
        assert result.result["error"] == "decision_not_confirmed"
        service.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,text",
    [
        ("delete", "Don't delete our morning flight memory"),
        ("delete", 'The quote says "forget our morning flight memory"'),
        ("delete", "> forget our morning flight memory"),
        ("update", "Never update our morning flight memory"),
        ("update", 'Example: "change our morning flight memory"'),
    ],
)
async def test_mutation_intent_cannot_come_from_negation_or_quotes(action, text):
    entry = memory()
    _, service, executor = setup(text, memories=[entry])
    result = await executor.execute(ToolCall(f"{action}_memory", {"memory_id": str(entry.id)}))
    assert result.result["ok"] is False
    getattr(service, action).assert_not_awaited()
    assert executor.actions == []


@pytest.mark.asyncio
async def test_ambiguous_delete_cannot_choose_model_supplied_id():
    first, second = memory("Morning flights"), memory("Morning flights preferred by group")
    _, service, executor = setup("Forget our morning flights", memories=[first, second])
    result = await executor.execute(ToolCall("delete_memory", {"memory_id": str(first.id)}))
    assert result.result["error"] == "ambiguous_memory_target"
    service.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_unseen_memory_id_is_not_authorized_then_search_can_resolve_target():
    entry = memory()
    _, service, executor = setup("Forget our morning flights")
    call = ToolCall("delete_memory", {"memory_id": str(entry.id)}, "delete-id")
    assert (await executor.execute(call)).result["error"] == "ambiguous_memory_target"
    service.search.return_value = [entry]
    await executor.execute(ToolCall("search_memories", {"query": "morning"}))
    result = await executor.execute(call)
    assert result.result["ok"] is True
    assert result.call_id == "delete-id"
    assert executor.actions[0]["action"] == "deleted"


@pytest.mark.asyncio
async def test_update_requires_current_source_quote_before_service_mutation():
    entry = memory()
    context, service, executor = setup(
        "Change our flight memory to evening flights", memories=[entry]
    )
    arguments = {"memory_id": str(entry.id), "content": "Evening flights"}
    invalid = await executor.execute(ToolCall("update_memory", arguments))
    assert invalid.result["error"] == "invalid_conversational_evidence"
    service.update.assert_not_awaited()
    arguments["source_quote"] = context.current_message
    assert (await executor.execute(ToolCall("update_memory", arguments))).result["ok"] is True
    service.update.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_response_is_bounded_and_only_exposed_results_become_known():
    _, service, executor = setup()
    entries = [memory("x" * 1900) for _ in range(5)]
    service.search.return_value = entries
    result = await executor.execute(ToolCall("search_memories", {"query": "x"}))
    exposed = result.result["memories"]
    assert 0 < len(exposed) < len(entries)
    assert len(json.dumps(exposed, ensure_ascii=False)) <= 4000
    assert set(executor.known) == {entry.id for entry in entries[: len(exposed)]}


@pytest.mark.asyncio
async def test_automatic_memory_off_does_not_block_direct_remember_request():
    context, service, executor = setup()
    context.auto_memory_enabled = False
    assert (await executor.execute(save_call(context.current_message))).result[
        "error"
    ] == "automatic_memory_disabled"
    service.save.assert_not_awaited()
    context.current_message = "Remember that we prefer morning flights"
    context.sources[0] = context.sources[0].model_copy(update={"text": context.current_message})
    result = await executor.execute(
        save_call(context.current_message, durability="explicit_request")
    )
    assert result.result["ok"] is True
    assert service.save.await_args.kwargs["explicit"] is True


@pytest.mark.asyncio
async def test_storage_failures_propagate_and_cannot_claim_saved_action():
    context, service, executor = setup()
    service.save.side_effect = MemoryInfrastructureError()
    with pytest.raises(MemoryInfrastructureError):
        await executor.execute(save_call(context.current_message))
    assert executor.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["Don't remember this", "Do not save this", "Never store this"])
async def test_explicit_current_refusal_blocks_automatic_save_from_prior_source(refusal):
    context, service, executor = setup()
    prior = context.sources[0]
    context.current_message = refusal
    result = await executor.execute(save_call(prior.text, source_message_id=str(prior.id)))
    assert result.result["error"] == "explicit_remember_intent_required"
    service.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("agreement", ['The example says "Agreed"', "Not agreed", "Maybe agreed"])
async def test_decision_cannot_use_quoted_negative_or_tentative_confirmation(agreement):
    context, service, executor = setup("Destination Seoul")
    context.sources.append(
        SourceMessage(
            id=uuid4(),
            telegram_message_id=11,
            text=agreement,
            is_bot_message=False,
            user_id=uuid4(),
            reply_to_telegram_message_id=10,
        )
    )
    result = await executor.execute(save_call("Destination Seoul", durability="decision"))
    assert result.result["error"] == "decision_not_confirmed"
    service.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_named_delete_target_cannot_select_other_persons_similar_fact():
    anna, jian = uuid4(), uuid4()
    anna_memory = memory("Morning flights", scope="user", user_id=anna, member_name="Anna")
    jian_memory = memory("Morning flights", scope="user", user_id=jian, member_name="Jian")
    context, service, executor = setup(
        "Forget Anna's morning flights", memories=[anna_memory, jian_memory]
    )
    context.members = [
        MemberEntry(id=anna, display_name="Anna", username="anna"),
        MemberEntry(id=jian, display_name="Jian", username="jian"),
    ]
    wrong = await executor.execute(ToolCall("delete_memory", {"memory_id": str(jian_memory.id)}))
    assert wrong.result["error"] == "ambiguous_memory_target"
    service.delete.assert_not_awaited()
    assert (
        await executor.execute(ToolCall("delete_memory", {"memory_id": str(anna_memory.id)}))
    ).result["ok"] is True
