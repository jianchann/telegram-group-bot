"""Safe, deliberately small Telegram HTML renderer."""

import re
from dataclasses import dataclass
from html import escape

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,!?;:"


@dataclass(frozen=True, slots=True)
class RenderedChunk:
    """One Telegram message and the equivalent unformatted fallback."""

    html: str
    plain: str


@dataclass(frozen=True, slots=True)
class _Character:
    value: str
    bold: bool = False
    href: str | None = None


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def plain_chunks(text: str, max_chars: int = 4000) -> list[str]:
    """Split text on Unicode boundaries, counting Telegram UTF-16 units."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for char in text:
        width = _utf16_length(char)
        if width > max_chars:
            raise ValueError("max_chars cannot fit a Unicode character")
        if length + width > max_chars:
            chunks.append("".join(current))
            current, length = [], 0
        current.append(char)
        length += width
    if current:
        chunks.append("".join(current))
    return chunks


def _split_url_punctuation(candidate: str) -> tuple[str, str]:
    suffix = ""
    while candidate and candidate[-1] in _TRAILING_URL_PUNCTUATION:
        suffix = candidate[-1] + suffix
        candidate = candidate[:-1]
    while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
        suffix = ")" + suffix
        candidate = candidate[:-1]
    return candidate, suffix


def _characters_with_urls(text: str, *, bold: bool) -> list[_Character]:
    result: list[_Character] = []
    position = 0
    for match in _URL_RE.finditer(text):
        result.extend(_Character(char, bold=bold) for char in text[position : match.start()])
        url, suffix = _split_url_punctuation(match.group())
        result.extend(_Character(char, bold=bold, href=url) for char in url)
        result.extend(_Character(char, bold=bold) for char in suffix)
        position = match.end()
    result.extend(_Character(char, bold=bold) for char in text[position:])
    return result


def _inline_characters(text: str, *, force_bold: bool = False) -> list[_Character]:
    """Recognize paired asterisk emphasis; unmatched markers remain literal."""
    result: list[_Character] = []
    position = 0
    for match in re.finditer(r"(?<!\*)\*\*([^*\n]+)\*\*|(?<!\*)\*([^*\n]+)\*", text):
        result.extend(_characters_with_urls(text[position : match.start()], bold=force_bold))
        result.extend(_characters_with_urls(match.group(1) or match.group(2), bold=True))
        position = match.end()
    result.extend(_characters_with_urls(text[position:], bold=force_bold))
    return result


def _parse(text: str) -> list[_Character]:
    result: list[_Character] = []
    for line in text.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\n"):
            body, newline = line[:-1], "\n"
        heading = re.fullmatch(r"#{1,6}[ \t]+(.+)", body)
        if heading:
            result.extend(_inline_characters(heading.group(1), force_bold=True))
        else:
            bullet = re.match(r"^[ \t]*(?:[-*•])[ \t]+", body)
            if bullet:
                result.extend([_Character("•"), _Character(" ")])
                body = body[bullet.end() :]
            result.extend(_inline_characters(body))
        if newline:
            result.append(_Character(newline))
    return result


def _render(characters: list[_Character]) -> RenderedChunk:
    html_parts: list[str] = []
    position = 0
    while position < len(characters):
        first = characters[position]
        end = position + 1
        while end < len(characters):
            candidate = characters[end]
            if (candidate.bold, candidate.href) != (first.bold, first.href):
                break
            end += 1
        value = "".join(char.value for char in characters[position:end])
        rendered = escape(value, quote=False)
        if first.href is not None:
            rendered = f'<a href="{escape(first.href, quote=True)}">{rendered}</a>'
        if first.bold:
            rendered = f"<b>{rendered}</b>"
        html_parts.append(rendered)
        position = end
    return RenderedChunk(
        html="".join(html_parts),
        plain="".join(character.value for character in characters),
    )


def render_rich_chunks(text: str, max_chars: int = 4000) -> list[RenderedChunk]:
    """Render a safe Markdown-like subset into UTF-16-bounded Telegram chunks."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    characters = _parse(text)
    chunks: list[RenderedChunk] = []
    current: list[_Character] = []
    length = 0
    for character in characters:
        width = _utf16_length(character.value)
        if width > max_chars:
            raise ValueError("max_chars cannot fit a Unicode character")
        if length + width > max_chars:
            chunks.append(_render(current))
            current, length = [], 0
        current.append(character)
        length += width
    if current:
        chunks.append(_render(current))
    return chunks


def render_chunks(text: str, max_chars: int = 4000) -> list[str]:
    """Legacy escaped-text renderer retained for callers not using rich chunks."""
    if max_chars < 6:
        raise ValueError("max_chars must be at least six for escaped entities")
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for char in text:
        escaped = escape(char, quote=False)
        width = _utf16_length(escaped)
        if length + width > max_chars:
            chunks.append("".join(current))
            current, length = [], 0
        current.append(escaped)
        length += width
    if current:
        chunks.append("".join(current))
    return chunks
