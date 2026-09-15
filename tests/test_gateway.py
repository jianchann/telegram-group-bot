import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from google.genai import types

from app.schemas.ai import AssistantContext, GeminiResult, ToolDeclaration, ToolResult
from app.schemas.research import ResearchToolBundle
from app.services.ai_orchestrator import AIOrchestrator
from app.services.gemini_gateway import GeminiError, GoogleGeminiGateway


def provider(text="Answer", usage=True):
    response = SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=42, candidates_token_count=8)
        if usage
        else None,
    )
    return SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=AsyncMock(return_value=response)),
            aclose=AsyncMock(),
        ),
        close=Mock(),
    )


@pytest.mark.asyncio
async def test_managed_cloud_adapter_configures_adc_model_and_output(settings):
    client = provider()
    with patch("app.services.gemini_gateway.genai.Client", return_value=client) as factory:
        gateway = GoogleGeminiGateway(settings)
    options = factory.call_args.kwargs
    assert options["enterprise"] is True
    assert options["project"] == "test-project"
    assert options["location"] == "global"
    assert "api_key" not in options
    assert options["http_options"].api_version == "v1"
    assert options["http_options"].retry_options.attempts == 1
    result = await gateway.run(prompt="Current question", system_prompt="Application rules")
    assert result == GeminiResult("Answer", 42, 8)
    call = client.aio.models.generate_content.call_args.kwargs
    assert call["model"] == settings.gemini_model
    assert call["contents"] == "Current question"
    assert call["config"].max_output_tokens == 1024
    assert call["config"].system_instruction == "Application rules"
    assert call["config"].tools is None
    await gateway.close()
    client.aio.aclose.assert_awaited_once()
    client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,usage", [("", True), ("   ", False), (None, False)])
async def test_empty_or_blocked_provider_response_is_failure(settings, text, usage):
    gateway = GoogleGeminiGateway(settings, client=provider(text, usage))
    with pytest.raises(GeminiError, match="empty_response"):
        await gateway.run(prompt="Question", system_prompt="Rules")


@pytest.mark.asyncio
async def test_provider_exception_is_sanitized_without_retry(settings):
    client = provider()
    client.aio.models.generate_content.side_effect = RuntimeError("SECRET prompt and token")
    gateway = GoogleGeminiGateway(settings, client=client)
    with pytest.raises(GeminiError) as error:
        await gateway.run(prompt="Question", system_prompt="Rules")
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__
    assert client.aio.models.generate_content.await_count == 1


@pytest.mark.asyncio
async def test_provider_timeout_is_bounded(settings):
    client = provider()

    async def slow(**kwargs):
        await asyncio.sleep(1)

    client.aio.models.generate_content.side_effect = slow
    gateway = GoogleGeminiGateway(settings, client=client)
    gateway.timeout_seconds = 0.01
    with pytest.raises(GeminiError, match="timeout"):
        await gateway.run(prompt="Question", system_prompt="Rules")


@pytest.mark.asyncio
async def test_orchestrator_uses_a_fake_gateway_and_preserves_usage():
    from uuid import uuid4

    gateway = SimpleNamespace(run=AsyncMock(return_value=GeminiResult("Answer", 7, 3)))
    orchestrator = AIOrchestrator(gateway, "Application rules")
    context = AssistantContext(uuid4(), "Jian", "Question")
    response = await orchestrator.respond(context)
    assert response.text == "Answer"
    assert (response.input_tokens, response.output_tokens) == (7, 3)
    gateway.run.assert_awaited_once_with(prompt=context.prompt(), system_prompt="Application rules")


@pytest.mark.asyncio
async def test_tool_turn_preserves_native_signatures_and_matching_response_ids(settings):
    client = provider()
    native = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    name="save_memory", args={"content": "morning"}, id="call-1"
                ),
                thought_signature=b"signature",
            ),
        ],
    )
    answer = types.Content(role="model", parts=[types.Part(text="Saved")])
    client.aio.models.generate_content.side_effect = [
        types.GenerateContentResponse(
            candidates=[types.Candidate(content=native)],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=12, candidates_token_count=4, thoughts_token_count=6
            ),
        ),
        types.GenerateContentResponse(candidates=[types.Candidate(content=answer)]),
    ]
    gateway = GoogleGeminiGateway(settings, client=client)
    declarations = [
        ToolDeclaration(
            "save_memory",
            "Save memory",
            {"type": "object", "properties": {"content": {"type": "string"}}},
        )
    ]
    first = await gateway.turn(prompt="Remember", system_prompt="Rules", tools=declarations)
    assert first.text is None
    assert first.calls[0].call_id == "call-1"
    assert first.output_tokens == 10
    assert first.continuation[-1] is native
    second = await gateway.turn(
        prompt="Remember",
        system_prompt="Rules",
        tools=declarations,
        continuation=first.continuation,
        results=[ToolResult("save_memory", {"ok": True}, "call-1")],
        allow_tools=False,
        max_output_tokens=1000,
    )
    assert second.text == "Saved"
    call = client.aio.models.generate_content.await_args_list[1].kwargs
    assert call["contents"][1] is native
    function_response = call["contents"][2].parts[0].function_response
    assert function_response.id == "call-1"
    assert function_response.name == "save_memory"
    assert call["config"].automatic_function_calling.disable is True
    assert call["config"].tool_config.function_calling_config.mode == "NONE"
    assert call["config"].max_output_tokens == 1000


@pytest.mark.asyncio
async def test_tool_turn_rejects_thought_only_response(settings):
    client = provider()
    client.aio.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text="internal", thought=True)]
                )
            )
        ]
    )
    with pytest.raises(GeminiError, match="empty_response"):
        await GoogleGeminiGateway(settings, client=client).turn(
            prompt="Question", system_prompt="Rules", tools=[]
        )


@pytest.mark.asyncio
async def test_tool_result_transcript_limit_stops_before_second_provider_call(settings):
    client = provider()
    native = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(name="search_memories", args={}, id="search-1"),
                thought_signature=b"original-signature",
            )
        ],
    )
    client.aio.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[types.Candidate(content=native)]
    )
    gateway = GoogleGeminiGateway(settings, client=client)
    first = await gateway.turn(prompt="Find memories", system_prompt="Rules", tools=[])
    with pytest.raises(GeminiError, match="input_limit"):
        await gateway.turn(
            prompt="Find memories",
            system_prompt="Rules",
            tools=[],
            continuation=first.continuation,
            results=[ToolResult("search_memories", {"content": "x" * 40000}, "search-1")],
        )
    assert client.aio.models.generate_content.await_count == 1
    assert first.continuation[-1] is native
    assert native.parts[0].thought_signature == b"original-signature"


@pytest.mark.asyncio
async def test_visible_text_limit_counts_plain_characters_once(settings):
    client = provider()
    client.aio.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=[types.Part(text="Answer")]))
        ]
    )
    gateway = GoogleGeminiGateway(settings, client=client)
    gateway.max_context_chars = 100
    await gateway.turn(prompt='"' * 100, system_prompt="Rules", tools=[])
    assert client.aio.models.generate_content.await_count == 1


@pytest.mark.asyncio
async def test_research_turn_configures_builtins_and_preserves_verified_metadata(settings):
    client = provider()
    content = types.Content(
        role="model",
        parts=[
            types.Part(text="Try this hotel."),
            types.Part(tool_call=types.ToolCall(tool_type="GOOGLE_MAPS", id="maps-1", args={})),
        ],
    )
    candidate = types.Candidate(
        content=content,
        grounding_metadata=types.GroundingMetadata(
            grounding_chunks=[
                types.GroundingChunk(
                    maps=types.GroundingChunkMaps(
                        title="Example Hotel",
                        uri="https://maps.google.com/example",
                        place_id="places/1",
                    )
                )
            ]
        ),
        url_context_metadata=types.UrlContextMetadata(
            url_metadata=[
                types.UrlMetadata(
                    retrieved_url="https://example.com/hotel",
                    url_retrieval_status="URL_RETRIEVAL_STATUS_SUCCESS",
                )
            ]
        ),
    )
    client.aio.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[candidate]
    )
    gateway = GoogleGeminiGateway(settings, client=client)
    result = await gateway.turn(
        prompt="Compare the hotel",
        system_prompt="Rules",
        tools=[ToolDeclaration("save_plan_artifact", "Save", {"type": "object"})],
        research_tools=ResearchToolBundle(
            use_maps=True,
            use_url_context=True,
            urls=("https://example.com/hotel",),
        ),
    )
    assert result.used_maps and result.used_url_context
    assert result.research_sources[0].attribution == "Google Maps"
    assert result.url_statuses[0].outcome == "succeeded"
    config = client.aio.models.generate_content.call_args.kwargs["config"]
    assert config.tool_config.include_server_side_tool_invocations is None
    assert config.tool_config.function_calling_config.mode == "VALIDATED"
    assert any(tool.google_maps for tool in config.tools)
    assert any(tool.url_context for tool in config.tools)
