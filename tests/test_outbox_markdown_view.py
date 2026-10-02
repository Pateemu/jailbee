from __future__ import annotations

from rich.text import Text

from jailbee.outbox.markdown_view import AnsiLine, print_lines, render_markdown


def plain(lines: list[str]) -> list[str]:
    """What a terminal would show: the lines with their style codes interpreted away."""
    return [Text.from_ansi(line).plain for line in lines]


LONG = "word " * 40


def test_non_terminal_returns_text_verbatim(monkeypatch) -> None:
    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: None)
    assert render_markdown("# T\n\n" + LONG, indent="  ") == ["  # T", "  ", "  " + LONG]


def test_paragraph_wraps_to_width() -> None:
    lines = plain(render_markdown(LONG, width=40))
    assert len(lines) > 1
    assert all(len(line) <= 40 for line in lines)


def test_indent_counts_against_width() -> None:
    lines = plain(render_markdown(LONG, indent="    ", width=40))
    assert all(line.startswith("    ") and len(line) <= 40 for line in lines)


def test_list_continuation_is_indented() -> None:
    lines = plain(render_markdown("- " + LONG, width=40))
    assert lines[0].lstrip().startswith("•")
    assert all(line.startswith("   ") for line in lines[1:])


def test_code_block_content_is_kept() -> None:
    assert any("abc def" in line for line in plain(render_markdown("```\nabc def\n```", width=40)))


def test_styled_output_is_marked_and_coloured(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    lines = render_markdown("# T\n\n**bold** `code`", width=40)
    assert all(isinstance(line, AnsiLine) for line in lines if line)
    assert any("\x1b[" in line for line in lines)


def test_no_hyperlink_escapes_and_url_stays_visible() -> None:
    lines = render_markdown("[x](https://e.com)", width=60)
    assert not any("\x1b]8" in line for line in lines)
    assert "https://e.com" in "\n".join(plain(lines))


def test_escapes_in_the_body_never_reach_the_output() -> None:
    lines = render_markdown("hi \x1b]8;;http://evil\x07there \x1b[31mred", width=60)
    out = "\n".join(plain(lines))
    assert "\x1b" not in out and "\x07" not in out
    assert "evil" in out  # only the control characters are dropped, not the text
    assert "\x1b]8" not in "\n".join(lines)


def test_empty_text_and_no_leading_or_trailing_blank_lines() -> None:
    assert render_markdown("", width=40) == []
    lines = render_markdown("- a\n\n- b", width=40)
    assert lines[0] and lines[-1]


def test_print_lines_strips_escapes_from_plain_lines_but_keeps_styled_ones(capsys) -> None:
    print_lines(["plain \x1b[31mred\x1b[0m", AnsiLine("\x1b[1mstyled\x1b[0m")])
    out = capsys.readouterr().out
    assert "plain [31mred" in out or "plain red" in out
    assert "\x1b[31m" not in out


def test_issue_prose_wraps_bodies_but_not_titles(monkeypatch) -> None:
    from jailbee import issue_outbox

    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: 40)
    body = plain(issue_outbox._prose("body", LONG, markdown=True))
    title = issue_outbox._prose("title", LONG)
    assert len(body) > 3 and all(len(line) <= 40 for line in body)
    assert title == ["  title:", f"    {LONG}"]
