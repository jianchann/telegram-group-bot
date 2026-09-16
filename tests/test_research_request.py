from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.ai import AssistantContext, ContextMessage, ToolResult
from app.services.research_request import (
    ResearchValidationError,
    add_result_urls,
    allowed_research_urls,
    validate_research,
)


@pytest.mark.parametrize("kind", ["search", "maps", "url_context", "code_execution"])
def test_builds_exactly_one_builtin_and_canonical_cache(kind):
    urls = ["https://example.com/b", "https://example.com/a"] if kind == "url_context" else []
    args, bundle = validate_research(
        {"kind": kind, "question": " Verify this ", "urls": urls}, set(urls), 2
    )
    assert (
        sum((bundle.use_search, bundle.use_maps, bundle.use_url_context, bundle.use_code_execution))
        == 1
    )
    assert bundle.urls == tuple(urls)
    reordered, _ = validate_research(
        {"kind": kind, "question": "Verify this", "urls": list(reversed(urls))}, set(urls), 2
    )
    assert args.cache_key() == reordered.cache_key()


@pytest.mark.parametrize(
    "arguments",
    [
        {"kind": "unknown", "question": "Verify"},
        {"kind": "search", "question": "   "},
        {"kind": "search", "question": "x" * 2001},
        {"kind": "url_context", "question": "Read"},
        {"kind": "maps", "question": "Find", "urls": ["https://example.com"]},
        {"kind": "url_context", "question": "Read", "urls": ["not-a-url"]},
        {"kind": "search", "question": "Verify", "extra": True},
    ],
)
def test_invalid_arguments(arguments):
    with pytest.raises(ResearchValidationError, match="invalid_arguments"):
        validate_research(arguments, set(), 2)


def test_dynamic_url_cap_and_embedded_url_provenance():
    urls = ["https://example.com/a", "https://example.com/b"]
    with pytest.raises(ResearchValidationError, match="invalid_arguments"):
        validate_research({"kind": "url_context", "question": "Read", "urls": urls}, set(urls), 1)
    for kind in ("search", "maps", "code_execution"):
        with pytest.raises(ResearchValidationError, match="url_not_allowed"):
            validate_research({"kind": kind, "question": "Check https://evil.example"}, set(), 2)
    with pytest.raises(ResearchValidationError, match="url_not_allowed"):
        validate_research({"kind": "url_context", "question": "Read", "urls": urls}, {urls[0]}, 2)


def test_provenance_uses_human_context_and_active_artifacts_only():
    context = AssistantContext(
        chat_id=uuid4(),
        current_user="Jian",
        current_message="Read https://current.example",
        recent_messages=[
            ContextMessage(1, "Anna", "https://human.example"),
            ContextMessage(2, "Bot", "https://bot.example", True),
        ],
        reply_message=ContextMessage(3, "Bot", "https://reply-bot.example", True),
        verified_research_urls=("https://verified.example",),
    )
    context.active_plan = SimpleNamespace(artifacts=[{"url": "https://saved.example"}])
    assert allowed_research_urls(context) == {
        "https://current.example",
        "https://human.example",
        "https://saved.example",
        "https://verified.example",
    }
    context.reply_message = ContextMessage(4, "Anna", "https://reply-human.example")
    assert "https://reply-human.example" in allowed_research_urls(context)


def test_only_successful_authorized_read_results_extend_urls():
    allowed: set[str] = set()
    artifact = {"url": "https://saved.example"}
    add_result_urls(
        allowed, ToolResult("get_plan", {"ok": False, "plan": {"artifacts": [artifact]}})
    )
    add_result_urls(
        allowed, ToolResult("save_plan_artifact", {"ok": True, "artifacts": [artifact]})
    )
    assert not allowed
    add_result_urls(
        allowed,
        ToolResult(
            "get_plan",
            {
                "ok": True,
                "plan": {"artifacts": [artifact], "description": "https://untrusted.example"},
            },
        ),
    )
    add_result_urls(
        allowed,
        ToolResult(
            "list_plan_artifacts", {"ok": True, "artifacts": [{"url": "https://second.example"}]}
        ),
    )
    assert allowed == {"https://saved.example", "https://second.example"}
