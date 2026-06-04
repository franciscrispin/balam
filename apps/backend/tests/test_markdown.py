from balam.markdown import TELEGRAM_MAX_LENGTH, gfm_to_telegram, split_message


def test_bold_and_heading_render_as_markdownv2() -> None:
    # Heading and **bold** both map to *...*; the period is escaped.
    (out,) = gfm_to_telegram("# Title\n\nHello **bold**.")
    assert "*Title*" in out
    assert "*bold*" in out
    assert "\\." in out


def test_special_characters_are_escaped_outside_code() -> None:
    (out,) = gfm_to_telegram("a-b (c) .")
    assert "\\-" in out
    assert "\\(" in out and "\\)" in out


def test_code_span_and_block_are_preserved() -> None:
    (inline,) = gfm_to_telegram("call `f(x)` now")
    assert "`f(x)`" in inline  # contents not escaped inside a code span

    (block,) = gfm_to_telegram("```python\nprint(1)\n```")
    assert block.startswith("```python")
    assert "print(1)" in block


def test_table_becomes_monospace_block() -> None:
    (out,) = gfm_to_telegram("| a | bb |\n|---|----|\n| 1 | 22 |")
    assert out.startswith("```")
    assert "a" in out and "bb" in out


def test_links_keep_url_and_escape_closing_paren() -> None:
    (out,) = gfm_to_telegram("see [docs](https://x.com/a(b))")
    assert "[docs](https://x.com/a(b)\\)" in out


def test_split_keeps_chunks_within_limit() -> None:
    chunks = split_message("x" * (TELEGRAM_MAX_LENGTH + 500))
    assert len(chunks) >= 2
    assert all(len(c) <= TELEGRAM_MAX_LENGTH for c in chunks)


def test_split_is_code_block_aware() -> None:
    # A fenced block straddling the boundary is closed and reopened.
    body = "```python\n" + "a = 1\n" * 2000 + "```"
    chunks = split_message(body, max_length=200)
    assert len(chunks) >= 2
    assert chunks[0].rstrip().endswith("```")  # first chunk closes the fence
    assert chunks[1].lstrip().startswith("```python")  # next reopens it


def test_empty_text_yields_no_chunks() -> None:
    assert gfm_to_telegram("") == []
    assert split_message("   ") == []
