"""Google Cloud Gemini adapter; no provider state is a durable memory store."""

import asyncio
import json
from typing import Any, Protocol
from urllib.parse import urlsplit

from google import genai
from google.genai import types

from app.config import Settings
from app.schemas.ai import GeminiResult, GeminiTurn, ToolCall, ToolDeclaration, ToolResult
from app.schemas.media import MediaAttachment
from app.schemas.research import (
    ResearchSource,
    ResearchToolBundle,
    URLRetrievalOutcome,
    URLRetrievalStatus,
)


class GeminiError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Gemini request failed ({code})")


class GeminiGateway(Protocol):
    async def run(
        self, *, prompt: str, system_prompt: str, media: MediaAttachment | None = None
    ) -> GeminiResult: ...

    async def turn(
        self,
        *,
        prompt: str,
        system_prompt: str,
        tools: list[ToolDeclaration],
        continuation: Any = None,
        results: list[ToolResult] | None = None,
        allow_tools: bool = True,
        max_output_tokens: int | None = None,
        research_tools: ResearchToolBundle | None = None,
        media: MediaAttachment | None = None,
    ) -> GeminiTurn: ...


def _tool_type(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").lower()


def _research_result(
    candidate: Any, content: Any
) -> tuple[tuple[ResearchSource, ...], tuple[URLRetrievalStatus, ...], bool, bool, bool, bool]:
    sources: list[ResearchSource] = []
    statuses: list[URLRetrievalStatus] = []
    used_search = used_maps = used_url_context = used_code_execution = False
    metadata = getattr(candidate, "grounding_metadata", None)
    flag_uris = {
        str(flag.source_id): str(flag.flag_content_uri)
        for flag in (getattr(metadata, "source_flagging_uris", None) or [])
        if getattr(flag, "source_id", None) and getattr(flag, "flag_content_uri", None)
    }
    for chunk in getattr(metadata, "grounding_chunks", None) or []:
        web = getattr(chunk, "web", None)
        maps = getattr(chunk, "maps", None)
        if web is not None and getattr(web, "uri", None):
            used_search = True
            sources.append(
                ResearchSource(
                    "search",
                    str(getattr(web, "title", None) or getattr(web, "domain", None) or "Source"),
                    str(web.uri),
                    domain=getattr(web, "domain", None),
                )
            )
        if maps is not None and getattr(maps, "uri", None):
            used_maps = True
            answer_sources = getattr(maps, "place_answer_sources", None)
            sources.append(
                ResearchSource(
                    "maps",
                    str(getattr(maps, "title", None) or "Place"),
                    str(maps.uri),
                    place_id=getattr(maps, "place_id", None),
                    attribution="Google Maps",
                    flag_uri=getattr(answer_sources, "flag_content_uri", None)
                    or flag_uris.get(str(getattr(maps, "place_id", ""))),
                )
            )
            review_snippets = (
                getattr(answer_sources, "review_snippets", None)
                or getattr(answer_sources, "review_snippet", None)
                or []
            )
            for review in review_snippets:
                review_uri = getattr(review, "google_maps_uri", None)
                if not review_uri:
                    continue
                author = getattr(review, "author_attribution", None)
                author_name = getattr(author, "display_name", None)
                review_title = getattr(review, "title", None)
                title = f"Review by {author_name}" if author_name else str(review_title or "Review")
                sources.append(
                    ResearchSource(
                        "maps",
                        title,
                        str(review_uri),
                        attribution="Google Maps",
                        flag_uri=getattr(review, "flag_content_uri", None)
                        or flag_uris.get(str(getattr(review, "review_id", ""))),
                    )
                )
    if metadata is not None and getattr(metadata, "web_search_queries", None):
        used_search = True
    url_metadata = getattr(getattr(candidate, "url_context_metadata", None), "url_metadata", None)
    for item in url_metadata or []:
        url = str(getattr(item, "retrieved_url", "") or "")
        raw = _tool_type(getattr(item, "url_retrieval_status", None))
        outcome: URLRetrievalOutcome = (
            "succeeded"
            if "success" in raw
            else "unsafe"
            if "unsafe" in raw
            else "paywalled"
            if "paywall" in raw
            else "failed"
        )
        if url:
            statuses.append(URLRetrievalStatus(url, outcome))
            if outcome == "succeeded":
                host = urlsplit(url).hostname or "URL"
                sources.append(ResearchSource("url_context", host, url, domain=host))
        used_url_context = True
    for part in getattr(content, "parts", None) or []:
        tool_kind = _tool_type(getattr(getattr(part, "tool_call", None), "tool_type", None))
        used_search = used_search or "search" in tool_kind
        used_maps = used_maps or "map" in tool_kind
        used_url_context = used_url_context or "url" in tool_kind
        used_code_execution = used_code_execution or "code" in tool_kind
        used_code_execution = used_code_execution or any(
            getattr(part, field, None) is not None
            for field in ("executable_code", "code_execution_result")
        )
    unique: dict[tuple[str, str], ResearchSource] = {}
    for source in sources:
        unique.setdefault((source.kind, source.uri), source)
    return (
        tuple(unique.values()),
        tuple(statuses),
        used_search,
        used_maps,
        used_url_context,
        used_code_execution,
    )


class GoogleGeminiGateway:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.gemini_model
        self.max_output_tokens = settings.max_ai_output_tokens
        self.timeout_seconds = settings.gemini_timeout_seconds
        self.max_context_chars = settings.max_context_chars
        self._client = client or genai.Client(
            enterprise=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            http_options=types.HttpOptions(
                api_version="v1",
                timeout=int(self.timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    async def run(
        self, *, prompt: str, system_prompt: str, media: MediaAttachment | None = None
    ) -> GeminiResult:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_bytes(data=media.data, mime_type=media.mime_type),
                                types.Part(text=prompt),
                            ],
                        )
                        if media
                        else prompt
                    ),
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        max_output_tokens=self.max_output_tokens,
                    ),
                )
            if not response.text or not response.text.strip():
                raise GeminiError("empty_response")
            usage = response.usage_metadata
            return GeminiResult(
                response.text.strip(),
                usage.prompt_token_count if usage else None,
                usage.candidates_token_count if usage else None,
            )
        except GeminiError:
            raise
        except TimeoutError:
            raise GeminiError("timeout") from None
        except Exception:
            raise GeminiError("provider_error") from None

    async def summarize(
        self, *, prompt: str, system_prompt: str, max_output_tokens: int, timeout_seconds: float
    ) -> GeminiResult:
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        max_output_tokens=max_output_tokens,
                    ),
                )
            if not response.text or not response.text.strip():
                raise GeminiError("empty_response")
            usage = response.usage_metadata
            return GeminiResult(
                response.text.strip(),
                usage.prompt_token_count if usage else None,
                usage.candidates_token_count if usage else None,
            )
        except GeminiError:
            raise
        except TimeoutError:
            raise GeminiError("timeout") from None
        except Exception:
            raise GeminiError("provider_error") from None

    async def close(self) -> None:
        try:
            await self._client.aio.aclose()
        finally:
            self._client.close()

    async def turn(
        self,
        *,
        prompt: str,
        system_prompt: str,
        tools: list[ToolDeclaration],
        continuation: Any = None,
        results: list[ToolResult] | None = None,
        allow_tools: bool = True,
        max_output_tokens: int | None = None,
        research_tools: ResearchToolBundle | None = None,
        media: MediaAttachment | None = None,
    ) -> GeminiTurn:
        """Run one application-owned tool turn; native history is invocation-local.

        Output usage includes candidate and thought tokens: both consume the shared
        generation budget. Native model Content is preserved, including signatures.
        """
        history = (
            list(continuation)
            if continuation is not None
            else [
                types.Content(
                    role="user",
                    parts=[
                        *(
                            [types.Part.from_bytes(data=media.data, mime_type=media.mime_type)]
                            if media
                            else []
                        ),
                        types.Part(text=prompt),
                    ],
                )
            ]
        )
        if results:
            history.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=result.name, id=result.call_id, response=result.result
                            )
                        )
                        for result in results
                    ],
                )
            )
        visible_chars = 0
        for content in history:
            for part in content.parts or []:
                visible_chars += len(part.text or "")
                for payload in (part.function_call, part.function_response):
                    if payload is not None:
                        visible_chars += len(
                            json.dumps(
                                payload.model_dump(mode="json", exclude_none=True),
                                ensure_ascii=False,
                            )
                        )
        if visible_chars > self.max_context_chars:
            raise GeminiError("input_limit")
        declarations = [
            types.FunctionDeclaration(
                name=tool.name, description=tool.description, parameters_json_schema=tool.parameters
            )
            for tool in tools
        ]
        research_tools = research_tools or ResearchToolBundle()
        provider_tools: list[Any] = []
        if declarations:
            provider_tools.append(types.Tool(function_declarations=declarations))
        if research_tools.use_search:
            provider_tools.append(types.Tool(google_search=types.GoogleSearch()))
        if research_tools.use_maps:
            provider_tools.append(types.Tool(google_maps=types.GoogleMaps()))
        if research_tools.use_url_context:
            provider_tools.append(types.Tool(url_context=types.UrlContext()))
        if research_tools.use_code_execution:
            provider_tools.append(types.Tool(code_execution=types.ToolCodeExecution()))
        has_research = any(
            (
                research_tools.use_search,
                research_tools.use_maps,
                research_tools.use_url_context,
                research_tools.use_code_execution,
            )
        )
        provider_prompt = prompt
        if research_tools.use_url_context and research_tools.urls and continuation is None:
            provider_prompt += "\n\nOnly retrieve these URLs with URL Context:\n" + "\n".join(
                research_tools.urls
            )
            if visible_chars + len(provider_prompt) - len(prompt) > self.max_context_chars:
                raise GeminiError("input_limit")
            history[0] = types.Content(
                role="user",
                parts=[
                    *(part for part in (history[0].parts or []) if part.inline_data is not None),
                    types.Part(text=provider_prompt),
                ],
            )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        max_output_tokens=max_output_tokens or self.max_output_tokens,
                        tools=provider_tools or None,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                        tool_config=types.ToolConfig(
                            function_calling_config=(
                                types.FunctionCallingConfig(
                                    mode=(
                                        types.FunctionCallingConfigMode.VALIDATED
                                        if allow_tools and has_research
                                        else types.FunctionCallingConfigMode.AUTO
                                        if allow_tools
                                        else types.FunctionCallingConfigMode.NONE
                                    )
                                )
                                if declarations
                                else None
                            ),
                        )
                        if provider_tools
                        else None,
                    ),
                )
            candidates = response.candidates or []
            content = candidates[0].content if candidates else None
            if content is None:
                raise GeminiError("empty_response")
            calls: list[ToolCall] = []
            texts: list[str] = []
            for part in content.parts or []:
                if part.function_call is not None:
                    call = part.function_call
                    calls.append(ToolCall(call.name or "", dict(call.args or {}), call.id))
                if part.text and not part.thought:
                    texts.append(part.text)
            text = "".join(texts).strip() or None
            if not text and not calls:
                raise GeminiError("empty_response")
            usage = response.usage_metadata
            inputs = usage.prompt_token_count if usage else None
            outputs = usage.candidates_token_count if usage else None
            thoughts = usage.thoughts_token_count if usage else None
            if type(outputs) is int and outputs >= 0:
                outputs += thoughts if type(thoughts) is int and thoughts >= 0 else 0
            elif type(thoughts) is int and thoughts >= 0:
                outputs = thoughts
            else:
                outputs = None
            if type(inputs) is not int or inputs < 0:
                inputs = None
            research = _research_result(candidates[0], content)
            return GeminiTurn(
                text,
                calls,
                inputs,
                outputs,
                [*history, content],
                *research,
            )
        except GeminiError:
            raise
        except TimeoutError:
            raise GeminiError("timeout") from None
        except Exception as exc:
            code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            description = str(exc).lower()
            built_in_count = sum(
                (
                    research_tools.use_search,
                    research_tools.use_maps,
                    research_tools.use_url_context,
                    research_tools.use_code_execution,
                )
            )
            if (
                (declarations or built_in_count > 1)
                and has_research
                and code in {400, 404}
                and any(
                    term in description for term in ("tool", "function", "unsupported", "invalid")
                )
            ):
                raise GeminiError("unsupported_tool_combination") from None
            raise GeminiError("provider_error") from None
