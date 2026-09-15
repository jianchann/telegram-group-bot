from app.schemas.research import ResearchSource, URLRetrievalStatus
from app.services.source_renderer import render_research_sources


def test_verified_sources_are_deduplicated_and_maps_attributed():
    sources = [
        ResearchSource(
            "maps",
            "Example Hotel",
            "https://maps.google.com/example",
            attribution="Google Maps",
        ),
        ResearchSource("maps", "Duplicate", "https://maps.google.com/example"),
        ResearchSource("search", "Travel guide", "https://example.com/guide"),
    ]
    text = render_research_sources("Answer", sources)
    assert "Answer\n\nSources:" in text
    assert "Google Maps — Example Hotel — https://maps.google.com/example" in text
    assert text.count("https://maps.google.com/example") == 1


def test_failed_url_status_is_reported_without_becoming_a_source():
    text = render_research_sources(
        "I could not compare it.",
        [],
        [URLRetrievalStatus("https://example.com/private", "unsafe")],
    )
    assert "URLs I couldn't open:" in text
    assert "blocked as unsafe" in text
    assert "Sources:" not in text
