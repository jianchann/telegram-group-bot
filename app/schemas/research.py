"""Provider-independent contracts for routed Gemini research tools."""

from dataclasses import dataclass, field
from typing import Literal

ResearchSourceKind = Literal["search", "maps", "url_context"]
URLRetrievalOutcome = Literal["succeeded", "failed", "unsafe", "paywalled"]


@dataclass(frozen=True)
class ResearchToolBundle:
    """Built-in tools that a provider may offer for one assistant invocation."""

    use_search: bool = False
    use_maps: bool = False
    use_url_context: bool = False
    use_code_execution: bool = False
    urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchSource:
    """A source verified from provider grounding metadata."""

    kind: ResearchSourceKind
    title: str
    uri: str
    domain: str | None = None
    place_id: str | None = None
    attribution: str | None = None
    flag_uri: str | None = None


@dataclass(frozen=True)
class URLRetrievalStatus:
    """Provider-reported retrieval result for a requested URL."""

    url: str
    outcome: URLRetrievalOutcome
    detail: str | None = None


@dataclass(frozen=True)
class ResearchResult:
    """Research metadata accumulated across one or more provider turns."""

    sources: tuple[ResearchSource, ...] = field(default_factory=tuple)
    url_statuses: tuple[URLRetrievalStatus, ...] = field(default_factory=tuple)
    used_search: bool = False
    used_maps: bool = False
    used_url_context: bool = False
    used_code_execution: bool = False
