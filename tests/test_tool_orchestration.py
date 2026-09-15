import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.schemas.ai import AssistantContext, GeminiTurn, ToolCall, ToolDeclaration, ToolResult
from app.schemas.memory import MemoryInfrastructureError
from app.schemas.research import ResearchSource, ResearchToolBundle
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
async def test_final_turn_cannot_execute_tools():
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


@pytest.mark.asyncio
async def test_four_turns_allow_research_two_tool_rounds_and_final_synthesis():
    executor = Executor()
    calls = [
        GeminiTurn("Research", [], 1, 1),
        GeminiTurn(None, [ToolCall("save_memory", {}, "one")], 1, 1, "first"),
        GeminiTurn(None, [ToolCall("save_memory", {}, "two")], 1, 1, "second"),
        GeminiTurn("Done", [], 1, 1, "final"),
    ]
    gateway = SimpleNamespace(turn=AsyncMock(side_effect=calls))
    router = SimpleNamespace(route=lambda _: ResearchToolBundle(use_search=True))

    response = await AIOrchestrator(
        gateway, "rules", lambda _: executor, research_router=router
    ).respond(context())

    assert response.text == "Done"
    assert response.error_code is None
    assert len(executor.calls) == 2
    assert gateway.turn.await_count == 4
    assert gateway.turn.await_args_list[-1].kwargs["allow_tools"] is False


@pytest.mark.asyncio
async def test_call_limit_returns_all_results_without_ninth_execution():
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
async def test_unsupported_combination_falls_back_to_research_then_custom_synthesis():
    executor = Executor()
    source = ResearchSource("search", "Example", "https://example.com/source")
    gateway = SimpleNamespace(
        turn=AsyncMock(
            side_effect=[
                GeminiError("unsupported_tool_combination"),
                GeminiTurn(
                    text="Grounded result",
                    research_sources=(source,),
                    used_search=True,
                ),
                GeminiTurn(text="Final grounded answer"),
            ]
        )
    )
    router = SimpleNamespace(route=lambda _: ResearchToolBundle(use_search=True, use_maps=True))
    response = await AIOrchestrator(
        gateway, "rules", lambda _: executor, research_router=router
    ).respond(context())
    assert response.text == "Final grounded answer"
    assert response.used_search
    assert response.research_sources == (source,)
    assert gateway.turn.await_args_list[0].kwargs["tools"] == []
    assert gateway.turn.await_args_list[1].kwargs["research_tools"] == ResearchToolBundle(
        use_maps=True
    )
    assert gateway.turn.await_args_list[2].kwargs["research_tools"] == ResearchToolBundle()
    assert "untrusted_research_result" in gateway.turn.await_args_list[2].kwargs["prompt"]
