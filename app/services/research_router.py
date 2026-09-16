"""HTTP(S) URL extraction shared by research and plan tools."""

import re
from urllib.parse import urlsplit, urlunsplit

_URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"


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
