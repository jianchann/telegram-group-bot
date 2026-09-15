"""Small deterministic hint router for Gemini built-in research tools."""

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.schemas.ai import AssistantContext
from app.schemas.research import ResearchToolBundle

_URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"

_SEARCH_HINT = re.compile(
    r"\b(?:current|currently|latest|recent|today|news|research|search|look\s+up|"
    r"verify|price|prices|availability|available|events?|opened?|closed?|closures?|"
    r"polic(?:y|ies)|recommendations?|find)\b",
    re.IGNORECASE,
)
_MAPS_HINT = re.compile(
    r"\b(?:maps?|restaurants?|hotels?|attractions?|places?|locations?|routes?|"
    r"neighbou?rhoods?|nearby|around|distance|directions?|proximity|"
    r"where\s+to\s+(?:stay|eat))\b",
    re.IGNORECASE,
)
_CODE_HINT = re.compile(
    r"\b(?:calculate|compute|arithmetic|split(?:ting)?|per\s+person|total|percent(?:age)?|"
    r"budget|currency|convert|conversion|timing|rank(?:ing)?|optimi[sz](?:e|ation)|"
    r"schedule)\b",
    re.IGNORECASE,
)
_SAVE_HINT = re.compile(r"\b(?:save|add|store|bookmark|attach)\b", re.IGNORECASE)
_RESEARCH_ACTION_HINT = re.compile(
    r"\b(?:compare|summari[sz]e|review|analy[sz]e|open|check|find|research|search|"
    r"recommend|verify|calculate|compute|good|worth|current|latest|today)\b",
    re.IGNORECASE,
)
_SAVED_URL_HINT = re.compile(
    r"\b(?:compare|saved|links?|urls?|websites?|pages?|previous|those|these\s+options)\b",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Return valid HTTP(S) URLs in appearance order."""

    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        candidate = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            continue
        urls.append(urlunsplit(parsed))
    return urls


def _artifact_urls(active_plan: Any) -> Iterable[str]:
    if active_plan is None:
        return ()
    values: list[str] = []
    for artifact in getattr(active_plan, "artifacts", ()):
        url = artifact.get("url") if isinstance(artifact, dict) else getattr(artifact, "url", None)
        if isinstance(url, str):
            values.extend(extract_urls(url))
    return values


class ResearchRouter:
    """Select allowed built-ins; Gemini still decides whether to call them."""

    def __init__(self, max_url_context_urls: int = 5) -> None:
        if not 1 <= max_url_context_urls <= 20:
            raise ValueError("max_url_context_urls must be between 1 and 20")
        self.max_url_context_urls = max_url_context_urls

    def route(self, context: AssistantContext) -> ResearchToolBundle:
        request = context.current_message
        saved_urls = _artifact_urls(context.active_plan) if _SAVED_URL_HINT.search(request) else ()
        prioritized = [
            *extract_urls(context.current_message),
            *extract_urls(context.reply_message.text if context.reply_message else ""),
            *saved_urls,
        ]
        urls = tuple(dict.fromkeys(prioritized))[: self.max_url_context_urls]
        save_only = bool(_SAVE_HINT.search(request)) and not _RESEARCH_ACTION_HINT.search(request)
        return ResearchToolBundle(
            use_search=not save_only and bool(_SEARCH_HINT.search(request)),
            use_maps=not save_only and bool(_MAPS_HINT.search(request)),
            use_url_context=not save_only and bool(urls),
            use_code_execution=not save_only and bool(_CODE_HINT.search(request)),
            urls=urls,
        )
