from app.services.formatting import render_rich_chunks


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_safe_rich_rendering_and_plain_fallback() -> None:
    source = (
        "# Trip <draft>\n\n"
        "- Book **Hotel & Spa** at https://example.com/a?x=1&y=2.\n"
        "Raw <b>HTML</b>"
    )

    [chunk] = render_rich_chunks(source)

    assert chunk.html == (
        "<b>Trip &lt;draft&gt;</b>\n\n"
        "• Book <b>Hotel &amp; Spa</b> at "
        '<a href="https://example.com/a?x=1&amp;y=2">'
        "https://example.com/a?x=1&amp;y=2</a>.\n"
        "Raw &lt;b&gt;HTML&lt;/b&gt;"
    )
    assert chunk.plain == (
        "Trip <draft>\n\n• Book Hotel & Spa at https://example.com/a?x=1&y=2.\nRaw <b>HTML</b>"
    )


def test_unmatched_bold_marker_remains_literal() -> None:
    [chunk] = render_rich_chunks("An **unfinished thought")
    assert chunk.html == "An **unfinished thought"
    assert chunk.plain == "An **unfinished thought"


def test_single_asterisk_emphasis_is_rendered_without_visible_markers() -> None:
    [chunk] = render_rich_chunks("• Wahuomi\n  *Vibe:* Stylish and cozy")

    assert chunk.html == "• Wahuomi\n  <b>Vibe:</b> Stylish and cozy"
    assert chunk.plain == "• Wahuomi\n  Vibe: Stylish and cozy"


def test_chunks_bound_visible_utf16_and_reopen_markup() -> None:
    chunks = render_rich_chunks("**" + "😀" * 9 + "**", max_chars=6)

    assert [chunk.plain for chunk in chunks] == ["😀😀😀", "😀😀😀", "😀😀😀"]
    assert all(chunk.html == f"<b>{chunk.plain}</b>" for chunk in chunks)
    assert all(utf16_length(chunk.plain) <= 6 for chunk in chunks)


def test_long_url_chunks_have_complete_tags_and_matching_fallback() -> None:
    chunks = render_rich_chunks("https://example.com/abcdefgh", max_chars=8)

    assert "".join(chunk.plain for chunk in chunks) == "https://example.com/abcdefgh"
    assert all(chunk.html.count("<a ") == chunk.html.count("</a>") == 1 for chunk in chunks)
    assert all(utf16_length(chunk.plain) <= 8 for chunk in chunks)


def test_empty_text_has_no_chunks() -> None:
    assert render_rich_chunks("") == []
