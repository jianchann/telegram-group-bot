from app.services.research_router import extract_urls


def test_url_extraction_ignores_non_http_and_strips_sentence_punctuation():
    assert extract_urls(
        "See https://example.com/a?q=1), ftp://example.org and https://second.example/x#part."
    ) == ["https://example.com/a?q=1", "https://second.example/x#part"]
