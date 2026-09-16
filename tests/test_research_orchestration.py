"""Research is requested within reasoning, with shared budgets and provenance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.schemas.ai import AssistantContext, GeminiTurn, ToolCall, ToolDeclaration, ToolResult
from app.schemas.research import ResearchSource, URLRetrievalStatus
from app.services.ai_orchestrator import AIOrchestrator
from app.services.gemini_gateway import GeminiError


class ReadExecutor:
    declarations = [ToolDeclaration("get_plan", "Read a plan", {"type": "object"})]

    def __init__(self):
        self.actions = []
        self.calls = []

    async def execute(self, call):
        self.calls.append(call)
        return ToolResult(
            call.name,
            {"ok": True, "plan": {"artifacts": [{"url": "https://hotel.example/details"}]}},
            call.call_id,
        )


def research(kind="search", question="Check hotel prices", **kwargs):
    return ToolCall("research", {"kind": kind, "question": question, **kwargs}, "research-id")


def request(calls, continuation="native-state"):
    return GeminiTurn(None, calls, 10, 2, continuation)


async def respond(turns, message="What are some good hotels in Seoul?", **limits):
    executor = ReadExecutor()
    gateway = SimpleNamespace(turn=AsyncMock(side_effect=turns))
    response = await AIOrchestrator(gateway, "rules", lambda _: executor, **limits).respond(
        AssistantContext(uuid4(), "Jian", message)
    )
    return response, gateway, executor


@pytest.mark.asyncio
async def test_local_answer_does_not_start_research():
    response, gateway, executor = await respond(
        [GeminiTurn("Your plan starts Friday", [], 10, 2)], message="When does our plan start?"
    )
    assert response.error_code is None
    assert gateway.turn.await_count == 1
    assert executor.calls == []
    arguments = gateway.turn.await_args.kwargs
    assert "research" in [tool.name for tool in arguments["tools"]]
    assert not arguments["research_tools"].use_search


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Recommend Seoul hotels", "What are good Seoul hotels?"])
async def test_recommendation_can_research_independent_of_phrasing(message):
    source = ResearchSource("search", "Hotel", "https://hotel.example")
    response, gateway, executor = await respond(
        [
            request([research()]),
            GeminiTurn("Verified options", [], 20, 3, research_sources=(source,), used_search=True),
            GeminiTurn("Here are the options", [], 30, 4),
        ],
        message=message,
    )
    assert response.research_sources == (source,)
    assert response.used_search
    assert (response.input_tokens, response.output_tokens) == (60, 9)
    assert executor.calls == []
    native, builtin, synthesis = [call.kwargs for call in gateway.turn.await_args_list]
    assert builtin["tools"] == [] and builtin["allow_tools"] is False
    assert builtin["research_tools"].use_search
    assert synthesis["continuation"] == "native-state"
    assert synthesis["results"][0].call_id == "research-id"
    assert synthesis["results"][0].result["ok"] is True
    assert synthesis["max_output_tokens"] == native["max_output_tokens"] - 5


@pytest.mark.asyncio
async def test_successful_saved_link_read_authorizes_research_and_supplies_read_data():
    url = "https://hotel.example/details"
    response, gateway, executor = await respond(
        [
            request([ToolCall("get_plan", {}, "plan-id")], "read-native"),
            request([research("url_context", "Review the hotel", urls=[url])], "research-native"),
            GeminiTurn(
                "Hotel details", [], 10, 2,
                url_statuses=(URLRetrievalStatus(url, "succeeded"),),
            ),
            GeminiTurn("Comparison", [], 10, 2),
        ],
        max_turns=5,
    )
    assert response.error_code is None
    assert len(executor.calls) == 1
    builtin = gateway.turn.await_args_list[2].kwargs
    assert "successful_read_results" in builtin["prompt"] and url in builtin["prompt"]
    assert builtin["research_tools"].urls == (url,)
    synthesis = gateway.turn.await_args_list[3].kwargs
    assert synthesis["continuation"] == "research-native"
    assert synthesis["results"][0].result["url_statuses"][0]["outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_two_research_kinds_cache_repeat_and_reject_third_actual_step():
    search = research()
    source = ResearchSource("search", "Hotel", "https://hotel.example")
    response, gateway, _ = await respond(
        [
            request([search]),
            GeminiTurn("Prices", [], 10, 2, research_sources=(source,)),
            request([search, research("code_execution", "Calculate the total")]),
            GeminiTurn("Total", [], 10, 2, used_code_execution=True),
            request([research("maps", "Find the nearest station")]),
            GeminiTurn("Done", [], 10, 2),
        ],
        max_turns=8,
    )
    assert response.error_code is None
    assert gateway.turn.await_count == 6
    assert gateway.turn.await_args_list[4].kwargs["results"][0].result["ok"] is True
    assert gateway.turn.await_args_list[4].kwargs["results"][1].result["ok"] is True
    assert gateway.turn.await_args_list[-1].kwargs["results"][0].result["error"] == (
        "research_step_limit"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "call", "error"),
    [
        ({"max_turns": 2}, research(), "research_turn_budget"),
        (
            {},
            research("url_context", "Read this", urls=["https://unknown.example"]),
            "url_not_allowed",
        ),
    ],
)
async def test_rejected_research_does_not_make_provider_call(limits, call, error):
    response, gateway, _ = await respond(
        [request([call]), GeminiTurn("Cannot verify", [], 10, 2)], **limits
    )
    assert response.error_code is None
    assert gateway.turn.await_count == 2
    assert gateway.turn.await_args_list[-1].kwargs["results"][0].result["error"] == error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("research_turn", "error"),
    [
        (GeminiError("provider_error"), "provider_error"),
        (GeminiTurn("Unsupported claims", [], 10, 2), "research_unverified"),
    ],
)
async def test_failed_or_unverified_research_returns_failure_for_synthesis(research_turn, error):
    response, gateway, executor = await respond(
        [request([research()]), research_turn, GeminiTurn("Could not verify", [], 10, 2)]
    )
    assert response.error_code is None
    payload = gateway.turn.await_args_list[-1].kwargs["results"][0].result
    assert payload["ok"] is False and payload["error"] == error
    assert executor.actions == []


@pytest.mark.asyncio
async def test_research_output_exhaustion_stops_without_synthesis_or_other_tools():
    response, gateway, executor = await respond(
        [
            request([research(), ToolCall("get_plan", {})]),
            GeminiTurn("Too long", [], 10, 62),
        ],
        max_output_tokens=64,
    )
    assert response.error_code == "output_limit"
    assert gateway.turn.await_count == 2
    assert executor.calls == []


@pytest.mark.asyncio
async def test_research_consumes_custom_call_budget_and_preserves_final_answer():
    response, gateway, executor = await respond(
        [
            request([research("code_execution", "Calculate the total"), ToolCall("get_plan", {})]),
            GeminiTurn("Total is 50", [], 10, 2, used_code_execution=True),
            GeminiTurn("The total is 50; plan retrieval was incomplete", [], 10, 2),
        ],
        max_tool_calls=1,
    )
    assert response.error_code is None
    assert executor.calls == []
    final = gateway.turn.await_args_list[-1].kwargs
    assert final["allow_tools"] is False
    assert final["results"][0].result["ok"] is True
    assert final["results"][1].result["error"] == "tool_limit"
