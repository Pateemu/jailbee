from __future__ import annotations

from jailbee.outbox.markdown_view import render_markdown

LONG = "word " * 40


def test_non_terminal_returns_text_verbatim(monkeypatch) -> None:
    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: None)
    assert render_markdown("# T\n\n" + LONG, indent="  ") == ["  # T", "  ", "  " + LONG]


def test_paragraph_wraps_to_width() -> None:
    lines = render_markdown(LONG, width=40)
    assert len(lines) > 1
    assert all(len(line) <= 40 for line in lines)


def test_indent_counts_against_width() -> None:
    lines = render_markdown(LONG, indent="    ", width=40)
    assert all(line.startswith("    ") and len(line) <= 40 for line in lines)


def test_list_continuation_is_indented() -> None:
    lines = render_markdown("- " + LONG, width=40)
    assert lines[0].lstrip().startswith("•")
    assert all(line.startswith("   ") for line in lines[1:])


def test_code_block_is_kept_unwrapped_in_content() -> None:
    lines = render_markdown("```\nabc def\n```", width=40)
    assert any("abc def" in line for line in lines)


def test_no_ansi_or_hyperlink_escapes() -> None:
    out = "\n".join(render_markdown("**b** [x](https://e.com) `c`", width=60))
    assert "\x1b" not in out
    assert "https://e.com" in out


def test_terminal_controls_are_stripped() -> None:
    out = "\n".join(render_markdown("hi \x1b]8;;http://evil\x07there", width=60))
    assert "\x1b" not in out and "\x07" not in out


def test_no_trailing_blank_lines_and_empty_text() -> None:
    assert render_markdown("", width=40) == []
    assert render_markdown("para", width=40)[-1] != ""


def test_issue_prose_wraps_bodies_but_not_titles(monkeypatch) -> None:
    from jailbee import issue_outbox

    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: 40)
    body = issue_outbox._prose("body", LONG, markdown=True)
    title = issue_outbox._prose("title", LONG)
    assert len(body) > 3 and all(len(line) <= 40 for line in body)
    assert title == ["  title:", f"    {LONG}"]
