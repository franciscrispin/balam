"""Rich-message (Bot API 10.1) payload construction — mainly the math escaping.

The bug these guard: Telegram's rich parser implements the GFM math extension,
so ``$…$`` in agent prose became a ``mathematical_expression`` — serif italics,
collapsed whitespace, ``-`` as ``−``, and any markdown inside it left literal.
"""

from balam.rich_messages import _rich_payload, chunk_rich, escape_math_delimiters

# The line that actually shipped broken, from session fa2e5a2c (chaska, Amex
# Shop Small). Telegram parsed it as "S" + math("3 back per in-store bill of
# **S") + "10+**".
REGRESSION = "| Offer | S$3 back per in-store bill of **S$10+** |"


def test_escapes_the_dollar_pair_that_broke_the_amex_table() -> None:
    assert escape_math_delimiters(REGRESSION) == (
        "| Offer | S\\$3 back per in-store bill of **S\\$10+** |"
    )


def test_escapes_every_dollar_not_only_the_pairs() -> None:
    # A lone "\$" renders as a plain "$", so escaping unconditionally costs
    # nothing and does not depend on Telegram's pairing rules staying put.
    assert escape_math_delimiters("Just S$5 off") == "Just S\\$5 off"


def test_leaves_fenced_code_alone() -> None:
    # Escaping here would show the user a literal backslash: Telegram keeps the
    # "\" inside a pre block, and code is immune to the math extension anyway.
    source = "text\n\n```sh\necho $PATH and $HOME\n```\n\nmore $5 and $6"
    assert "echo $PATH and $HOME" in escape_math_delimiters(source)
    assert escape_math_delimiters(source).endswith("more \\$5 and \\$6")


def test_leaves_tilde_fences_and_info_strings_alone() -> None:
    source = "~~~python\ncost = $5\n~~~"
    assert escape_math_delimiters(source) == source


def test_info_string_does_not_close_its_own_fence() -> None:
    # "```python" matches the fence regex; only a bare fence line closes.
    source = "```python\nx = $1\n```\n\nprice $9 and $8"
    result = escape_math_delimiters(source)
    assert "x = $1" in result
    assert result.endswith("price \\$9 and \\$8")


def test_leaves_code_spans_alone() -> None:
    source = "run `echo $PATH` then pay $5 and $6"
    assert escape_math_delimiters(source) == "run `echo $PATH` then pay \\$5 and \\$6"


def test_leaves_multi_backtick_code_spans_alone() -> None:
    source = "``a ` $b`` and $1 and $2"
    assert escape_math_delimiters(source) == "``a ` $b`` and \\$1 and \\$2"


def test_code_span_across_lines_stays_code() -> None:
    source = "`first $1\nsecond $2` then $3 and $4"
    assert escape_math_delimiters(source) == "`first $1\nsecond $2` then \\$3 and \\$4"


def test_unclosed_backtick_still_escapes_its_dollars() -> None:
    # A stray backtick must not swallow the rest of the message and leave the
    # bug in place — treat it as literal and keep escaping.
    assert escape_math_delimiters("a ` b $1 and $2") == "a ` b \\$1 and \\$2"


def test_does_not_double_escape_an_existing_escape() -> None:
    assert escape_math_delimiters("already \\$5 here") == "already \\$5 here"
    assert escape_math_delimiters("backslash \\\\ then $5") == "backslash \\\\ then \\$5"


def test_escapes_display_math_delimiters_too() -> None:
    assert escape_math_delimiters("Cost $$100 and $$200") == "Cost \\$\\$100 and \\$\\$200"


def test_unclosed_fence_is_treated_as_code() -> None:
    # Truncation mid-stream leaves an open fence; leaving it unescaped is the
    # safe direction (a missed escape, never a corrupted code block).
    source = "intro\n\n```sh\necho $PATH"
    assert escape_math_delimiters(source) == source


def test_leaves_dollar_free_markdown_untouched() -> None:
    source = "# Report\n\n| Metric | Value |\n|:---|---:|\n| Speed | 42 |\n\n- [x] done\n"
    assert escape_math_delimiters(source) == source


def test_rich_payload_escapes_so_no_send_path_can_miss_it() -> None:
    payload = _rich_payload(REGRESSION)
    assert "\\$" in payload["markdown"]
    assert payload["skip_entity_detection"] is True


def test_chunk_rich_leaves_escaping_to_the_payload() -> None:
    # Escaping happens once, in _rich_payload; chunking must not also do it or
    # the backslashes would double up.
    assert chunk_rich(REGRESSION) == [REGRESSION]
