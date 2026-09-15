"""Render only provider-verified research sources for Telegram."""

from collections.abc import Iterable

from app.schemas.research import ResearchSource, URLRetrievalStatus


def render_research_sources(
    text: str,
    sources: Iterable[ResearchSource],
    url_statuses: Iterable[URLRetrievalStatus] = (),
) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not source.uri or source.uri in seen:
            continue
        seen.add(source.uri)
        title = source.title.strip() or source.domain or "Source"
        label = f"Google Maps — {title}" if source.kind == "maps" else title
        lines.append(f"• {label} — {source.uri}")
        if source.kind == "maps" and source.flag_uri:
            lines.append(f"  Report a source issue — {source.flag_uri}")
    failures: list[str] = []
    failure_labels = {
        "failed": "unavailable",
        "unsafe": "blocked as unsafe",
        "paywalled": "behind a paywall",
    }
    for status in url_statuses:
        if status.outcome == "succeeded":
            continue
        failures.append(f"• {status.url} — {failure_labels[status.outcome]}")
    sections = [text.strip()]
    if lines:
        sections.append("Sources:\n" + "\n".join(lines))
    if failures:
        sections.append("URLs I couldn't open:\n" + "\n".join(failures))
    return "\n\n".join(section for section in sections if section)
