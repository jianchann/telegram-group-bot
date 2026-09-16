import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.logging import JSONFormatter, invocation_logging, log_ai_event
from app.schemas.ai import AssistantContext, GeminiTurn, ToolCall, ToolDeclaration, ToolResult
from app.schemas.memory import MemoryInfrastructureError
from app.schemas.research import ResearchSource, ResearchToolBundle, URLRetrievalStatus
from app.services.ai_orchestrator import AIOrchestrator
from app.services.gemini_gateway import GeminiError


class Executor:
    declarations = [ToolDeclaration("save_memory", "Save explicit memory", {"type": "object"})]

    def __init__(self):
        self.actions = []
        self.calls = []

    async def execute(self, call):
        self.calls.append(call)
        self.actions.append(
            {"action": "saved", "content": call.arguments.get("content", "example")}
        )
        return ToolResult(call.name, {"ok": True}, call.call_id)


def context():
    return AssistantContext(uuid4(), "Jian", "Remember morning flights")


@pytest.mark.asyncio
async def test_sequential_tools_preserve_ids_results_and_usage():
    executor = Executor()
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(
                    None,
                    [
                        ToolCall("save_memory", {"content": "morning"}, "a"),
                        ToolCall("save_memory", {"content": "aisle"}, "b"),
                    ],
                    10,
                    3,
                    "native",
                ),
                GeminiTurn("Saved", [], 15, 4, "finished"),
            ]
        )
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert response.text == "Saved"
    assert response.used_custom_functions
    assert (response.input_tokens, response.output_tokens) == (25, 7)
    assert len(response.memory_actions) == 2
    second = gateway.turn.await_args_list[1].kwargs
    assert second["continuation"] == "native"
    assert [item.call_id for item in second["results"]] == ["a", "b"]
    assert second["max_output_tokens"] == 1021


@pytest.mark.asyncio
async def test_final_turn_cannot_execute_tools(caplog):
    caplog.set_level(logging.INFO, logger="app")
    executor = Executor()
    turn = GeminiTurn(None, [ToolCall("save_memory", {})], 1, 1)
    gateway = SimpleNamespace(turn=AsyncMock(side_effect=[turn, turn, turn]))
    response = await AIOrchestrator(gateway, "rules", lambda _: executor, max_turns=3).respond(
        context()
    )
    assert len(executor.calls) == 2
    assert gateway.turn.await_args_list[2].kwargs["allow_tools"] is False
    assert response.error_code == "tool_limit"
    assert len(response.memory_actions) == 2
    final = gateway.turn.await_args_list[-1].kwargs
    assert "final synthesis turn" in final["system_prompt"]
    limits = [record for record in caplog.records if record.message == "ai_tool_limit"]
    assert limits[-1].limit_reason == "turn_budget"
    assert limits[-1].tool_calls_executed == 2


@pytest.mark.asyncio
async def test_four_turns_allow_state_research_and_final_synthesis():
    executor = Executor()
    source = ResearchSource("search", "Example", "https://example.com/source")
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(None, [ToolCall("save_memory", {}, "one")], 1, 1, "first"),
                GeminiTurn(
                    None,
                    [ToolCall("research", {"kind": "search", "question": "Current facts"}, "two")],
                    1,
                    1,
                    "second",
                ),
                GeminiTurn("Research", [], 1, 1, research_sources=(source,), used_search=True),
                GeminiTurn("Done", [], 1, 1, "final"),
            ]
        )
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert response.text == "Done"
    assert response.error_code is None
    assert len(executor.calls) == 1
    assert gateway.turn.await_count == 4
    final = gateway.turn.await_args_list[-1].kwargs
    assert final["allow_tools"] is False
    assert final["continuation"] == "second"
    assert final["results"][0].call_id == "two"
    assert final["results"][0].result["ok"] is True


@pytest.mark.asyncio
async def test_call_limit_returns_all_results_without_ninth_execution(caplog):
    caplog.set_level(logging.INFO, logger="app")
    executor = Executor()
    calls = [ToolCall("save_memory", {}, str(index)) for index in range(9)]
    gateway = SimpleNamespace(
        turn=AsyncMock(side_effect=[GeminiTurn(None, calls, 1, 1), GeminiTurn("Done", [], 1, 1)])
    )
    await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert len(executor.calls) == 8
    final = gateway.turn.await_args_list[1].kwargs
    assert final["allow_tools"] is False
    assert len(final["results"]) == 9
    assert final["results"][-1].result == {"error": "tool_limit"}
    limits = [record for record in caplog.records if record.message == "ai_tool_limit"]
    assert limits[-1].limit_reason == "tool_call_budget"
    skipped = [
        record
        for record in caplog.records
        if record.message == "ai_tool_finished" and record.status == "skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].tool_calls_executed == 8
    assert "final synthesis turn" in final["system_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [GeminiError("provider_error"), GeminiError("timeout")])
async def test_provider_failure_preserves_committed_actions(failure):
    executor = Executor()
    gateway = SimpleNamespace(
        turn=AsyncMock(side_effect=[GeminiTurn(None, [ToolCall("save_memory", {})], 2, 3), failure])
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert response.error_code == failure.code
    assert response.memory_actions == executor.actions
    assert (response.input_tokens, response.output_tokens) == (2, 3)


@pytest.mark.asyncio
async def test_deadline_includes_tool_execution_and_preserves_prior_action():
    executor = Executor()
    original = executor.execute

    async def slow(call):
        result = await original(call)
        await asyncio.sleep(1)
        return result

    executor.execute = slow
    gateway = SimpleNamespace(
        turn=AsyncMock(return_value=GeminiTurn(None, [ToolCall("save_memory", {})], 1, 1))
    )
    response = await AIOrchestrator(
        gateway, "rules", lambda _: executor, timeout_seconds=0.01
    ).respond(context())
    assert response.error_code == "timeout"
    assert len(response.memory_actions) == 1
    assert gateway.turn.await_count == 1


@pytest.mark.asyncio
async def test_storage_failure_aborts_remaining_tools_without_discarding_actions():
    executor = Executor()
    original = executor.execute

    async def fail_second(call):
        if executor.calls:
            raise MemoryInfrastructureError()
        return await original(call)

    executor.execute = fail_second
    gateway = SimpleNamespace(
        turn=AsyncMock(
            return_value=GeminiTurn(
                None, [ToolCall("save_memory", {}), ToolCall("save_memory", {})], 1, 1
            )
        )
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert response.error_code == "storage_error"
    assert len(response.memory_actions) == 1
    assert gateway.turn.await_count == 1


@pytest.mark.asyncio
async def test_exhausted_output_budget_stops_before_mutation():
    executor = Executor()
    gateway = SimpleNamespace(
        turn=AsyncMock(return_value=GeminiTurn(None, [ToolCall("save_memory", {})], 1, 1024))
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert response.error_code == "output_limit"
    assert not executor.actions
    assert gateway.turn.await_count == 1


@pytest.mark.asyncio
async def test_input_limit_preserves_prior_actions_and_provides_next_step():
    executor = Executor()
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(None, [ToolCall("save_memory", {})], 1, 1),
                GeminiError("input_limit"),
            ]
        )
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert response.error_code == "input_limit"
    assert len(response.memory_actions) == 1
    assert "/memory" in response.text


@pytest.mark.asyncio
async def test_unsupported_research_returns_failure_for_synthesis(caplog):
    caplog.set_level(logging.INFO, logger="app")
    executor = Executor()
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(
                    None,
                    [ToolCall("research", {"kind": "maps", "question": "Restaurants"})],
                    continuation="native",
                ),
                GeminiError("unsupported_tool_combination"),
                GeminiTurn("Could not verify places"),
            ]
        )
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    assert response.text == "Could not verify places"
    assert gateway.turn.await_args_list[1].kwargs["tools"] == []
    assert gateway.turn.await_args_list[1].kwargs["research_tools"] == ResearchToolBundle(
        use_maps=True
    )
    final = gateway.turn.await_args_list[2].kwargs
    assert final["continuation"] == "native"
    assert final["results"][0].result["error"] == "unsupported_tool_combination"
    attempts = [record for record in caplog.records if record.message == "ai_turn_finished"]
    assert [record.attempt_number for record in attempts] == [1, 2, 3]
    assert [record.turn_number for record in attempts] == [1, 2, 3]
    assert attempts[1].research_eligible == ["maps"]
    assert attempts[1].status == "failed"


@pytest.mark.asyncio
async def test_turn_logs_correlate_and_exclude_content(caplog):
    caplog.set_level(logging.INFO, logger="app")
    secret = "private-content-not-for-logs"
    executor = Executor()
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(None, [ToolCall(secret, {"content": secret})], 1, 2),
                GeminiError("provider_error"),
            ]
        )
    )
    with invocation_logging({"request_id": "request-one", "update_id": 123, "model": "fake"}):
        await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    events = [json.loads(JSONFormatter().format(record)) for record in caplog.records]
    assert len(events) == 3
    assert all(event["request_id"] == "request-one" for event in events)
    assert events[0]["tool_names"] == ["unknown"]
    assert events[1]["status"] == "completed"
    assert events[2]["status"] == "failed"
    assert events[2]["error_code"] == "provider_error"
    assert secret not in json.dumps(events)


@pytest.mark.asyncio
async def test_logging_context_is_isolated_and_reset_after_failure(caplog):
    caplog.set_level(logging.INFO, logger="app")
    logger = logging.getLogger("app.test")

    async def invocation(request_id):
        try:
            with invocation_logging({"request_id": request_id}):
                await asyncio.sleep(0)
                log_ai_event(logger, "inside", status="completed")
                raise ValueError("private exception")
        except ValueError:
            log_ai_event(logger, "outside")

    await asyncio.gather(invocation("one"), invocation("two"))
    inside = [record for record in caplog.records if record.message == "inside"]
    outside = [record for record in caplog.records if record.message == "outside"]
    assert {record.request_id for record in inside} == {"one", "two"}
    assert all(not hasattr(record, "request_id") for record in outside)


@pytest.mark.asyncio
async def test_tool_failure_logs_only_outcome(caplog):
    caplog.set_level(logging.INFO, logger="app")
    executor = Executor()
    executor.execute = AsyncMock(
        return_value=ToolResult(
            "save_memory", {"ok": False, "error": "invalid_tool_arguments", "content": "private"}
        )
    )
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(None, [ToolCall("save_memory", {})]),
                GeminiTurn("Could not save"),
            ]
        )
    )
    await AIOrchestrator(gateway, "rules", lambda _: executor).respond(context())
    event = next(record for record in caplog.records if record.message == "ai_tool_finished")
    assert event.status == "failed"
    assert event.error_code == "invalid_tool_arguments"
    assert "private" not in JSONFormatter().format(event)


@pytest.mark.asyncio
async def test_rejected_research_logs_kind_without_question(caplog):
    caplog.set_level(logging.INFO, logger="app")
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(
                    None,
                    [
                        ToolCall(
                            "research",
                            {
                                "kind": "url_context",
                                "question": "private request",
                                "urls": ["https://unknown.example"],
                            },
                        )
                    ],
                ),
                GeminiTurn("Cannot read that link"),
            ]
        )
    )
    await AIOrchestrator(gateway, "rules", lambda _: Executor()).respond(context())
    event = next(record for record in caplog.records if record.message == "ai_research_requested")
    assert event.research_kind == "url_context"
    assert event.rejection_reason == "url_not_allowed"
    serialized = JSONFormatter().format(event)
    assert "private request" not in serialized and "https://unknown.example" not in serialized


@pytest.mark.asyncio
async def test_unrequested_url_status_does_not_establish_verification():
    value = context()
    value.current_message = "Read https://allowed.example"
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiTurn(
                    None,
                    [
                        ToolCall(
                            "research",
                            {
                                "kind": "url_context",
                                "question": "Read the page",
                                "urls": ["https://allowed.example"],
                            },
                        )
                    ],
                ),
                GeminiTurn(
                    "Unrequested content",
                    url_statuses=(URLRetrievalStatus("https://other.example", "succeeded"),),
                    research_sources=(
                        ResearchSource("url_context", "Other", "https://other.example"),
                    ),
                    used_url_context=True,
                ),
                GeminiTurn("Could not verify the requested page"),
            ]
        )
    )
    response = await AIOrchestrator(gateway, "rules", lambda _: Executor()).respond(value)
    assert value.verified_research_urls == ()
    assert response.research_sources == () and response.url_statuses == ()
    assert (
        gateway.turn.await_args_list[-1].kwargs["results"][0].result["error"]
        == "research_unverified"
    )
