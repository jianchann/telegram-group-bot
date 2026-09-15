from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.ai import AssistantContext, ContextMessage
from app.services.research_router import ResearchRouter, extract_urls


def context(message: str) -> AssistantContext:
    return AssistantContext(chat_id=uuid4(), current_user="Jian", current_message=message)


def test_routes_small_deterministic_tool_hints():
    bundle = ResearchRouter().route(
        context("Find current hotel prices nearby and calculate the total per person")
    )
    assert bundle.use_search is True
    assert bundle.use_maps is True
    assert bundle.use_code_execution is True
    assert bundle.use_url_context is False


def test_static_conversation_enables_no_research_tools():
    bundle = ResearchRouter().route(context("Help us phrase the plan description"))
    assert not any(
        (
            bundle.use_search,
            bundle.use_maps,
            bundle.use_url_context,
            bundle.use_code_execution,
        )
    )


def test_url_priority_deduplication_and_limit():
    value = context("Compare https://current.example/a and https://same.example/x.")
    value.reply_message = ContextMessage(
        4,
        "Anna",
        "Earlier https://reply.example/b plus https://same.example/x",
    )
    value.active_plan = SimpleNamespace(
        artifacts=[
            SimpleNamespace(url="https://saved.example/c"),
            {"url": "https://saved.example/d"},
        ]
    )
    bundle = ResearchRouter(max_url_context_urls=4).route(value)
    assert bundle.use_url_context is True
    assert bundle.urls == (
        "https://current.example/a",
        "https://same.example/x",
        "https://reply.example/b",
        "https://saved.example/c",
    )


def test_saved_links_are_not_retrieved_for_an_unrelated_plan_question():
    value = context("What decisions have we made?")
    value.active_plan = SimpleNamespace(
        artifacts=[SimpleNamespace(url="https://saved.example/hotel")]
    )
    bundle = ResearchRouter().route(value)
    assert not bundle.use_url_context
    assert bundle.urls == ()


def test_url_extraction_ignores_non_http_and_strips_sentence_punctuation():
    assert extract_urls(
        "See https://example.com/a?q=1), ftp://example.org and https://second.example/x#part."
    ) == ["https://example.com/a?q=1", "https://second.example/x#part"]


@pytest.mark.parametrize("limit", [0, 21])
def test_url_limit_is_bounded(limit):
    with pytest.raises(ValueError, match="between 1 and 20"):
        ResearchRouter(limit)
