from __future__ import annotations

from dataclasses import replace

from rich.console import Console

from jailbee import dashboard_overlays as ov


def _prompt(**kw) -> ov.TextPrompt:
    base = {"purpose": "new-branch", "title": "New container", "label": "New branch"}
    return ov.TextPrompt(**{**base, **kw})


def _type(prompt: ov.TextPrompt, text: str) -> ov.TextPrompt:
    for ch in text.encode():
        prompt, outcome = ov.handle_prompt_key(prompt, bytes([ch]))
        assert outcome == "editing"
    return prompt


def test_typing_and_backspace_edit_the_text():
    p = _type(_prompt(), "feat")
    p, _ = ov.handle_prompt_key(p, b"\x7f")
    assert p.text == "fea"


def test_multibyte_character_split_across_reads_is_reassembled():
    p = _prompt()
    p, _ = ov.handle_prompt_key(p, b"\xc3")  # first half of "ä"
    assert p.text == "" and p.pending_utf8 == b"\xc3"
    p, _ = ov.handle_prompt_key(p, b"\xa4")
    assert p.text == "ä" and p.pending_utf8 == b""


def test_pasted_multibyte_text_lands_in_one_read():
    p, outcome = ov.handle_prompt_key(_prompt(), "työ-ä".encode())
    assert (p.text, outcome) == ("työ-ä", "editing")


def test_backspace_drops_a_pending_partial_before_touching_the_text():
    p = _type(_prompt(), "ab")
    p, _ = ov.handle_prompt_key(p, b"\xc3")
    p, _ = ov.handle_prompt_key(p, b"\x7f")
    assert (p.text, p.pending_utf8) == ("ab", b"")


def test_escape_ctrl_c_and_eof_cancel():
    for key in (b"\x1b", b"\x03", b""):
        assert ov.handle_prompt_key(_type(_prompt(), "x"), key)[1] == "cancel"


def test_arrow_escape_sequences_are_ignored_not_typed():
    p, outcome = ov.handle_prompt_key(_type(_prompt(), "x"), b"\x1b[A")
    assert (p.text, outcome) == ("x", "editing")


def test_enter_on_blank_answer_stays_open_with_an_inline_error():
    for blank in ("", "   "):
        p, outcome = ov.handle_prompt_key(_type(_prompt(), blank), b"\r")
        assert outcome == "editing"
        assert p.error == "New branch cannot be empty"
    # typing clears the error again
    p, _ = ov.handle_prompt_key(p, b"a")
    assert p.error is None


def test_enter_submits_and_the_caller_trims():
    p, outcome = ov.handle_prompt_key(_type(_prompt(), " feat "), b"\n")
    assert outcome == "submit" and p.text.strip() == "feat"


def test_pr_number_validation():
    assert ov.parse_pr_number("42") == 42
    for bad in ("", "0", "-3", "4x", "١٢", "9" * 5000, "1.5"):
        assert ov.parse_pr_number(bad) is None
    p, outcome = ov.handle_prompt_key(
        _type(_prompt(purpose="new-pr", label="PR number"), "abc"), b"\r"
    )
    assert outcome == "editing" and p.error == "PR number must be a positive whole number"


def test_picker_moves_clamped_and_picks():
    pk = ov.Picker("x", "Pick", (ov.PickerEntry("A", "a"), ov.PickerEntry("B", "b")))
    assert ov.picked(pk) == ov.PickerEntry("A", "a")
    assert ov.move_picker(pk, -1).index == 0
    assert ov.move_picker(ov.move_picker(pk, 1), 1).index == 1
    assert ov.picked(ov.move_picker(pk, 1)) == ov.PickerEntry("B", "b")
    assert ov.picked(ov.Picker("x", "Empty", ())) is None


def test_renderers_show_label_text_error_and_cursor():
    console = Console(width=80, record=True)
    # typing clears the error, so it is set after the text is entered
    console.print(ov.render_prompt(replace(_type(_prompt(), "abc"), error="oops")))
    console.print(ov.render_picker(ov.Picker("x", "Pick one", (ov.PickerEntry("Alpha", "a"),))))
    text = console.export_text()
    for expected in ("New container", "New branch", "abc", "oops", "Pick one", "Alpha"):
        assert expected in text
