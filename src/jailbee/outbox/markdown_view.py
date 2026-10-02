"""Readable console rendering of the Markdown bodies an outbox proposal carries.

A body is Markdown written by an agent in a container, so it routinely holds
paragraphs that are one line long. Printed verbatim they run off the terminal
and the structure (lists, code, headings) is lost. `render_markdown` lays a
body out for a terminal of a given width using Rich's Markdown renderer.

The output is plain text, never ANSI: the body is untrusted, so it is
stripped of terminal controls *before* rendering (`safe_text`), links are
shown as text rather than OSC 8 hyperlinks, and no colour is emitted. That is
also why the result is a list of lines — the outbox renderers stay pure and
their callers keep printing line by line.
"""

from __future__ import annotations

import io
import shutil
import sys

from rich.console import Console
from rich.markdown import Markdown

# Never wrap narrower than this, however deep the indent: a few columns of
# text per line is less readable than a long line.
_MIN_WIDTH = 20


def _terminal_width() -> int | None:
    """The terminal's width, or None when stdout is not a terminal.

    A pipe or a log gets the body verbatim, so `jb ... show | grep` keeps
    matching what the agent wrote.
    """
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size().columns


def render_markdown(text: str, *, indent: str = "", width: int | None = None) -> list[str]:
    """Lay `text` out as Markdown for a terminal, one string per line, each prefixed by `indent`.

    `width` is the full line width including `indent`; when omitted it is the
    terminal's, and a non-terminal stdout returns the text unwrapped and
    unrendered.
    """
    # Imported here: `outbox.inspect` imports `pr_outbox`, which imports this module.
    from jailbee.outbox.inspect import safe_text

    clean = safe_text(text)
    if width is None:
        width = _terminal_width()
    if width is None:
        return [f"{indent}{line}" for line in clean.split("\n")]
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=max(width - len(indent), _MIN_WIDTH),
        force_terminal=False,
        color_system=None,
        highlight=False,
    )
    console.print(Markdown(clean, hyperlinks=False))
    lines = [line.rstrip() for line in buffer.getvalue().split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return [f"{indent}{line}" if line else "" for line in lines]
