"""Validated application-owned research requests and invocation URL provenance."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.ai import AssistantContext, ToolDeclaration, ToolResult
from app.schemas.research import ResearchToolBundle
from app.services.research_router import extract_urls


class ResearchValidationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ResearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["search", "maps", "url_context", "code_execution"]
    question: str = Field(min_length=1, max_length=2000)
    urls: list[str] = Field(default_factory=list)

    def cache_key(self) -> str:
        return json.dumps(
            {"kind": self.kind, "question": self.question, "urls": sorted(set(self.urls))},
            sort_keys=True,
            ensure_ascii=False,
        )


RESEARCH_DECLARATION = ToolDeclaration(
    name="research",
    description=(
        "Verify real-world recommendations or changing facts with search/maps, "
        "read supplied or saved URLs with url_context, or calculate with code_execution. "
        "Use one kind per request. URL Context requires explicit authorized URLs; "
        "other kinds must omit urls. Reuse existing results when sufficient."
    ),
    parameters=ResearchArguments.model_json_schema(),
)


def validate_research(
    arguments: dict[str, Any], allowed_urls: set[str], max_urls: int
) -> tuple[ResearchArguments, ResearchToolBundle]:
    try:
        args = ResearchArguments.model_validate(arguments)
    except ValidationError as error:
        raise ResearchValidationError("invalid_arguments") from error
    if len(args.urls) > max_urls or (args.kind == "url_context") != bool(args.urls):
        raise ResearchValidationError("invalid_arguments")
    for url in args.urls:
        if extract_urls(url) != [url]:
            raise ResearchValidationError("invalid_arguments")
    if any(url not in allowed_urls for url in [*args.urls, *extract_urls(args.question)]):
        raise ResearchValidationError("url_not_allowed")
    args = args.model_copy(update={"urls": list(dict.fromkeys(args.urls))})
    return args, ResearchToolBundle(
        use_search=args.kind == "search",
        use_maps=args.kind == "maps",
        use_url_context=args.kind == "url_context",
        use_code_execution=args.kind == "code_execution",
        urls=tuple(args.urls),
    )


def _artifact_urls(artifacts: Any) -> set[str]:
    urls: set[str] = set()
    if not isinstance(artifacts, (list, tuple)):
        return urls
    for artifact in artifacts:
        url = artifact.get("url") if isinstance(artifact, dict) else getattr(artifact, "url", None)
        if isinstance(url, str):
            urls.update(extract_urls(url))
    return urls


def allowed_research_urls(context: AssistantContext) -> set[str]:
    urls = set(extract_urls(context.current_message))
    for message in context.recent_messages:
        if not message.is_bot_message:
            urls.update(extract_urls(message.text))
    if context.reply_message and not context.reply_message.is_bot_message:
        urls.update(extract_urls(context.reply_message.text))
    if context.active_plan:
        urls.update(_artifact_urls(context.active_plan.artifacts))
    urls.update(context.verified_research_urls)
    return urls


def add_result_urls(allowed: set[str], result: ToolResult) -> None:
    """Only server-checked successful plan reads may extend URL provenance."""
    if result.result.get("ok") is not True:
        return
    if result.name == "get_plan":
        plan = result.result.get("plan")
        if isinstance(plan, dict):
            allowed.update(_artifact_urls(plan.get("artifacts")))
    elif result.name == "list_plan_artifacts":
        allowed.update(_artifact_urls(result.result.get("artifacts")))
