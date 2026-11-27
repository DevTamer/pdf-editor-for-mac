"""Drives the real EditorApp through actual Tk widgets: hover, click to
edit, type, Return/Esc, warnings, undo, saving.

Needs a working Tcl/Tk and a display session; skips itself cleanly when a
Tk root can't be created.
"""

import json
import os

import pymupdf
import pytest

import editor
from conftest import LINE_ABOVE, LINE_BELOW, PAGE_WIDTH, TARGET, TITLE


class _Event:
    def __init__(self, x, y):
        self.x, self.y = x, y


@pytest.fixture(autouse=True)
def no_real_dialogs(monkeypatch):
    """A real dialog would block the test run waiting for a click. Any
    dialog a test doesn't explicitly expect (by monkeypatching it again)
    fails the test instead, naming the dialog."""
    def unexpected(name):
        def fail(*args, **kwargs):
            raise AssertionError(f"unexpected dialog {name}: {args} {kwargs}")
        return fail
    for name in ("showinfo", "showwarning", "showerror", "askyesno",
                 "askyesnocancel", "askokcancel", "askquestion"):
        monkeypatch.setattr(editor.messagebox, name, unexpected(name))
    for name in ("askopenfilename", "askopenfilenames", "asksaveasfilename"):
        monkeypatch.setattr(editor.filedialog, name, unexpected(name))


@pytest.fixture
def editor_app(monkeypatch):
    """The application, with every window it opens kept off screen."""
    try:
        instance = editor.EditorApp()
    except Exception as exc:
        pytest.skip(f"Tk isn't usable in this environment: {exc}")
    real_new_window = instance.new_window

    def hidden_window():
        win = real_new_window()
        win.window.withdraw()
        # A withdrawn window's canvas reports a width of 1px; pretend it's
        # 620px so pages fit at 600px wide (SCALE below) on every refresh.
        monkeypatch.setattr(win.canvas, "winfo_width", lambda: 620)
        # Showing a window would put it on screen.
        monkeypatch.setattr(win, "bring_to_front",
                            lambda: win.__dict__.__setitem__("fronted", True))
        return win

    monkeypatch.setattr(instance, "new_window", hidden_window)
    yield instance
    instance._quitting = True
    for win in list(instance.windows):
        win.model.close_document()
    instance._destroy_root()


@pytest.fixture
def app(editor_app, sample_pdf_path):
    """One window with the sample PDF open."""
    instance = editor_app.new_window()
    assert instance.open_path(str(sample_pdf_path))
    return instance


SCALE = 600 / PAGE_WIDTH  # canvas px per PDF point at fit_width=600


def _center_of(app, text):
    """Canvas position of a run's centre, computed from its PDF position
    and the expected scale (not from the code under test)."""
    run = next(r for r in app.model.runs(0) if r.text.strip() == text)
    return ((run.bbox[0] + run.bbox[2]) / 2 * SCALE,
            (run.bbox[1] + run.bbox[3]) / 2 * SCALE)


def _click(app, text):
    x, y = _center_of(app, text)
    app.view._on_click(_Event(x, y))


def _type(app, new_text):
    app.view.entry.delete(0, "end")
    app.view.entry.insert(0, new_text)


def _page_text(app):
    return app.model.get_page(0).get_text()


def _press(app, key):
    """Key events aren't delivered to a withdrawn window, so check the
    binding is wired to the right handler, then call that handler."""
    script = app.view.entry.bind(key)
    handler = {"<Return>": "_entry_commit", "<KP_Enter>": "_entry_commit",
               "<Escape>": "_entry_cancel"}[key]
    assert handler in script, script
    getattr(app.view, handler)()


def test_page_fits_at_the_test_scale(app):
    assert app.view.mapper.total_scale == pytest.approx(SCALE, rel=0.01)


# -- Hover -----------------------------------------------------------------------

def test_hover_outlines_text_and_changes_cursor(app):
    x, y = _center_of(app, TARGET)
    app.view._on_motion(_Event(x, y))
    assert len(app.canvas.find_withtag("hover")) == 1
    assert str(app.canvas.cget("cursor")) == "xterm"

    app.view._on_motion(_Event(450 * SCALE, 500 * SCALE))  # blank area
    assert app.canvas.find_withtag("hover") == ()
    assert str(app.canvas.cget("cursor")) == ""


def test_hover_outline_surrounds_the_right_text(app):
    x, y = _center_of(app, LINE_ABOVE)
    app.view._on_motion(_Event(x, y))
    x0, y0, x1, y1 = app.canvas.coords(app.canvas.find_withtag("hover")[0])
    run = next(r for r in app.model.runs(0) if r.text.strip() == LINE_ABOVE)
    assert x0 < run.bbox[0] * SCALE < run.bbox[2] * SCALE < x1
    assert y0 < run.bbox[1] * SCALE < run.bbox[3] * SCALE < y1


# -- Click, type, apply ----------------------------------------------------------

def test_click_opens_editor_with_the_text(app):
    _click(app, TARGET)
    assert app.view.entry is not None
    assert app.view.entry.get() == TARGET
    assert app.view.editing_run.text == TARGET
    assert "Helvetica" in app.info_label["text"]
    assert "12.0 pt" in app.info_label["text"]


def test_return_applies_the_edit(app):
    _click(app, TARGET)
    _type(app, "Edited through the GUI")
    _press(app, "<Return>")

    assert app.view.entry is None
    assert "Edited through the GUI" in _page_text(app)
    assert TARGET not in _page_text(app)
    assert app.history_list.size() == 2
    assert "Edit “Target line to edit”" in app.status_label["text"]
    assert app.window.title().endswith("— edited")
    assert str(app.undo_btn["state"]) == "normal"


def test_keypad_enter_applies_too(app):
    _click(app, TARGET)
    _type(app, "Keypad")
    _press(app, "<KP_Enter>")
    assert "Keypad" in _page_text(app)


def test_escape_cancels(app):
    _click(app, TARGET)
    _type(app, "Never applied")
    _press(app, "<Escape>")

    assert app.view.entry is None
    assert TARGET in _page_text(app)
    assert app.history_list.size() == 1


def test_clicking_other_text_commits_then_edits_that(app):
    _click(app, TARGET)
    _type(app, "Committed by clicking away")
    _click(app, LINE_ABOVE)

    assert "Committed by clicking away" in _page_text(app)
    assert app.view.entry is not None
    assert app.view.entry.get() == LINE_ABOVE


def test_clicking_blank_area_commits(app):
    _click(app, TARGET)
    _type(app, "Committed by clicking blank")
    app.view._on_click(_Event(450 * SCALE, 500 * SCALE))
    assert app.view.entry is None
    assert "Committed by clicking blank" in _page_text(app)


def test_warning_can_be_declined_or_accepted(app, monkeypatch):
    answers = []
    monkeypatch.setattr(editor.messagebox, "askokcancel",
                        lambda *a, **k: answers.pop(0))
    _click(app, TITLE)
    _type(app, "Quiz Report")  # 'i' and 'z' aren't in the embedded subset

    answers.append(False)
    app._commit_inline_edit()
    assert app.view.entry is not None          # still editing
    assert TITLE in _page_text(app)

    answers.append(True)
    app._commit_inline_edit()
    assert app.view.entry is None
    assert "Quiz Report" in _page_text(app)


def test_error_keeps_the_editor_open(app, monkeypatch):
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message, **k: shown.append(message))
    _click(app, TARGET)
    _type(app, "漢字")
    app._commit_inline_edit()

    assert shown and "None of the available fonts" in shown[0]
    assert app.view.entry is not None
    assert TARGET in _page_text(app)


def test_editor_matches_the_text_size_on_screen(app):
    _click(app, TITLE)  # 20pt
    font = app.view.entry.cget("font")
    size_px = abs(int(app.window.tk.call("font", "actual", font, "-size")))
    assert size_px == pytest.approx(20 * SCALE, abs=1.5)


# -- Undo ------------------------------------------------------------------------

def test_undo_while_typing_cancels_the_edit_only(app):
    _click(app, TARGET)
    _type(app, "First")
    app._commit_inline_edit()
    _click(app, "First")
    _type(app, "Second (unfinished)")

    app._on_undo()

    assert app.view.entry is None                 # typing cancelled...
    assert "First" in _page_text(app)             # ...but the doc wasn't undone
    app._on_undo()
    assert TARGET in _page_text(app)


def test_undo_redo_buttons(app):
    _click(app, TARGET)
    _type(app, "Edited")
    app._commit_inline_edit()

    app._on_undo()
    assert TARGET in _page_text(app)
    assert "Undo" in app.status_label["text"]
    app._on_redo()
    assert "Edited" in _page_text(app)


# -- Unsaved changes / saving ----------------------------------------------------

def test_closing_with_unsaved_edits_asks_first(app, monkeypatch):
    _click(app, TARGET)
    _type(app, "Edited")
    app._commit_inline_edit()

    monkeypatch.setattr(editor.messagebox, "askyesnocancel", lambda *a, **k: None)
    app._on_close_doc()
    assert app.model.doc is not None  # cancelled

    monkeypatch.setattr(editor.messagebox, "askyesnocancel", lambda *a, **k: False)
    app._on_close_doc()
    assert app.model.doc is None      # discarded


def test_save_as_writes_file_and_clears_dirty(app, tmp_path, monkeypatch):
    out = tmp_path / "saved.pdf"
    monkeypatch.setattr(editor.filedialog, "asksaveasfilename",
                        lambda **k: str(out))
    _click(app, TARGET)
    _type(app, "Saved edit")
    # Saving with the editor still open applies what was typed first.
    assert app._on_save()

    saved = pymupdf.open(str(out))
    assert "Saved edit" in saved[0].get_text()
    saved.close()
    assert not app.model.is_dirty
    assert "— edited" not in app.window.title()
    assert app.window.title().endswith("saved.pdf")

    # Plain Save now goes to the same file without asking.
    monkeypatch.setattr(editor.filedialog, "asksaveasfilename",
                        lambda **k: pytest.fail("shouldn't ask again"))
    _click(app, "Saved edit")
    _type(app, "Saved again")
    app._commit_inline_edit()
    assert app._on_save()
    saved = pymupdf.open(str(out))
    assert "Saved again" in saved[0].get_text()
    saved.close()


def test_open_documents_apple_event_reuses_the_window(app, editor_app,
                                                     sample_pdf_path):
    # Finder opening a PDF that's already open just brings its window up.
    editor_app.open_paths(str(sample_pdf_path), "/tmp/not-a-pdf.txt")
    assert editor_app.windows == [app]
    assert app.model.file_path == str(sample_pdf_path)


def test_history_click_while_typing_only_cancels_typing(app):
    _click(app, TARGET)
    _type(app, "Edited")
    app._commit_inline_edit()
    _click(app, "Edited")

    app.history_list.selection_clear(0, "end")
    app.history_list.selection_set(0)       # click "Original"
    app._on_history_select()

    assert app.view.entry is None
    assert "Edited" in _page_text(app)      # didn't jump
    assert app.history_list.curselection() == (1,)  # selection reset


def test_password_protected_pdf_shows_an_error(app, tmp_path, monkeypatch):
    path = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             user_pw="pw", owner_pw="pw")
    doc.close()
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message, **k: shown.append(message))

    app.open_path(str(path))

    assert shown and "password-protected" in shown[0]
    # The document that was already open is still open and shown.
    assert app.model.file_path.endswith("sample.pdf")
    assert app.window.title().endswith("sample.pdf")
    assert app.canvas.find_withtag("page_image")


# -- Tools -----------------------------------------------------------------------

def _press_move_release(app, start, end=None, steps=3):
    view = app.view
    view._on_click(_Event(*start))
    if end is not None:
        for i in range(1, steps + 1):
            x = start[0] + (end[0] - start[0]) * i / steps
            y = start[1] + (end[1] - start[1]) * i / steps
            view._on_drag(_Event(x, y))
    view._on_release(_Event(*(end or start)))


def _word_center(app, text, baseline=None):
    words = [w for w in app.model.words(0) if w.text == text
             and (baseline is None or abs(w.chars[0].origin[1] - baseline) < 0.5)]
    assert len(words) == 1, [w.text for w in words]
    x0, y0, x1, y1 = words[0].bbox
    return ((x0 + x1) / 2 * SCALE, (y0 + y1) / 2 * SCALE)


def test_tool_switching_shows_the_right_panel(app):
    for tool in ("add", "erase", "edit"):
        app.set_tool(tool)
        assert app.view.tool == tool
        assert app.tool_var.get() == tool
        shown = [t for t, panel in app.tool_panels.items() if panel.winfo_manager()]
        assert shown == [tool]
    app.set_tool("erase")
    assert str(app.canvas.cget("cursor")) == "crosshair"
    app.set_tool("add")
    assert str(app.canvas.cget("cursor")) == "xterm"


def test_switching_tool_applies_an_open_edit(app):
    _click(app, TARGET)
    _type(app, "Applied on tool switch")
    app.set_tool("erase")
    assert app.view.entry is None
    assert "Applied on tool switch" in _page_text(app)


def test_add_text_tool(app):
    app.set_tool("add")
    app.family_var.set("Times")
    app.size_var.set("20")
    app.bold_var.set(True)
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    assert app.view.entry is not None and app.view.entry.get() == ""
    _type(app, "Brand new line")
    _press(app, "<Return>")

    run = next(r for r in app.model.runs(0) if r.text == "Brand new line")
    assert run.font == "NimbusRoman-Bold" and run.size == pytest.approx(20)
    assert run.origin[0] == pytest.approx(60, abs=1)
    assert app.history_list.size() == 2
    assert "Add “Brand new line”" in app.status_label["text"]


def test_add_text_colour_picker(app, monkeypatch):
    monkeypatch.setattr(editor.colorchooser, "askcolor",
                        lambda **k: ((0, 128, 255), "#0080ff"))
    app.set_tool("add")
    app._choose_color()
    assert str(app.color_swatch.cget("bg")) == "#0080ff"
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    assert str(app.view.entry.cget("fg")) == "#0080ff"
    _type(app, "Blue text")
    app._commit_inline_edit()
    run = next(r for r in app.model.runs(0) if r.text == "Blue text")
    assert run.color == pytest.approx((0, 128 / 255, 1), abs=0.01)


def test_style_change_restyles_the_open_box(app):
    app.set_tool("add")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    app.size_var.set("30")
    size_px = abs(int(app.window.tk.call("font", "actual",
                                       app.view.entry.cget("font"), "-size")))
    assert size_px == pytest.approx(30 * SCALE, abs=1.5)


def test_add_text_error_keeps_the_box_open(app, monkeypatch):
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message, **k: shown.append(message))
    app.set_tool("add")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _type(app, "漢字")
    assert app._commit_inline_edit() is False
    assert shown and "can't draw" in shown[0]
    assert app.view.entry is not None
    assert app.history_list.size() == 1


def test_add_text_empty_or_escape_adds_nothing(app):
    app.set_tool("add")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _press(app, "<Return>")                       # empty
    assert app.view.entry is None
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _type(app, "never mind")
    _press(app, "<Escape>")
    assert app.view.entry is None
    assert app.history_list.size() == 1


def test_clicking_again_in_add_mode_commits_and_starts_new_text(app):
    app.set_tool("add")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _type(app, "First note")
    app.view._on_click(_Event(60 * SCALE, 340 * SCALE))
    assert "First note" in _page_text(app)
    assert app.view.entry is not None and app.view.entry.get() == ""


def test_erase_hover_outlines_words(app):
    app.set_tool("erase")
    app.view._on_motion(_Event(*_word_center(app, "Report")))
    assert len(app.canvas.find_withtag("hover")) == 1
    app.view._on_motion(_Event(450 * SCALE, 500 * SCALE))
    assert app.canvas.find_withtag("hover") == ()


def test_erase_click_erases_one_word(app):
    app.set_tool("erase")
    _press_move_release(app, _word_center(app, "Report"))
    text = _page_text(app)
    assert "Report" not in text and "Quarterly" in text
    assert app.history_list.size() == 2
    assert "Erase “Report”" in app.status_label["text"]


def test_erase_drag_across_words_erases_the_phrase(app):
    app.set_tool("erase")
    start = _word_center(app, "line", baseline=162)   # "Target line to edit"
    end = _word_center(app, "to", baseline=162)
    # Show the selection while dragging, before release.
    app.view._on_click(_Event(*start))
    app.view._on_drag(_Event(*end))
    assert [w.text for w in app.view.selection] == ["line", "to"]
    assert len(app.canvas.find_withtag("erase_sel")) == 2
    app.view._on_release(_Event(*end))

    words = " ".join(_page_text(app).split())
    assert "Target edit" in words
    assert app.canvas.find_withtag("erase_sel") == ()


def test_erase_rectangle_from_empty_space(app):
    app.set_tool("erase")
    run = next(r for r in app.model.runs(0) if r.text.strip() == "APPROVED")
    x0, y0, x1, y1 = run.bbox
    _press_move_release(app, ((x0 - 3) * SCALE, (y0 - 3) * SCALE),
                        ((x1 + 3) * SCALE, (y1 + 3) * SCALE))
    assert "APPROVED" not in _page_text(app)
    assert "Status:" in _page_text(app)


def test_tiny_drag_on_empty_space_does_nothing(app):
    app.set_tool("erase")
    _press_move_release(app, (450 * SCALE, 500 * SCALE),
                        (450 * SCALE + 2, 500 * SCALE + 2))
    assert app.history_list.size() == 1


def test_erasing_empty_area_shows_a_message(app, monkeypatch):
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message, **k: shown.append(message))
    app.set_tool("erase")
    _press_move_release(app, (300 * SCALE, 400 * SCALE), (400 * SCALE, 450 * SCALE))
    assert shown and "nothing to erase" in shown[0]
    assert app.history_list.size() == 1


def test_erase_uses_the_chosen_fill(app, monkeypatch):
    seen = []
    monkeypatch.setattr(app.model, "erase",
                        lambda plan: seen.append(plan.fill) or "Erase")
    app.set_tool("erase")
    app.fill_var.set("color")
    _press_move_release(app, _word_center(app, "Report"))
    assert seen == ["color"]


def test_undo_after_erase(app):
    app.set_tool("erase")
    _press_move_release(app, _word_center(app, "Report"))
    app._on_undo()
    assert "Quarterly Report" in _page_text(app)


# -- Review regressions ------------------------------------------------------------

def test_nonsense_size_does_not_leak_add_state_into_edits(app, monkeypatch):
    """A size like "nan" used to crash opening the add box while leaving
    the add state behind, so the next *edit* was applied as an added line."""
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message, **k: shown.append(message))
    app.set_tool("add")
    app.size_var.set("nan")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _type(app, "Should not be added")
    assert app._commit_inline_edit() is False             # refused, box kept
    assert shown and "between" in shown[0]
    _press(app, "<Escape>")
    assert app.view.adding_at is None

    app.size_var.set("12")
    app.set_tool("edit")
    _click(app, TARGET)
    _type(app, "Target line EDITED")
    _press(app, "<Return>")
    assert app.model.history_entries()[-1].startswith("Edit “Target line to edit”")
    assert "Target line EDITED" in _page_text(app)
    assert TARGET not in _page_text(app)


def test_add_then_edit_is_an_edit(app):
    app.set_tool("add")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _type(app, "Fresh line")
    _press(app, "<Return>")
    assert app.view.adding_at is None
    app.set_tool("edit")
    _click(app, "Fresh line")
    _type(app, "Fresh line, edited")
    _press(app, "<Return>")
    assert app.model.history_entries()[-1].startswith("Edit “Fresh line”")


def test_typed_text_is_saved_when_closing_with_save(app, tmp_path, monkeypatch):
    out = tmp_path / "closed.pdf"
    monkeypatch.setattr(editor.messagebox, "askyesnocancel", lambda *a, **k: True)
    monkeypatch.setattr(editor.filedialog, "asksaveasfilename", lambda **k: str(out))
    _click(app, TARGET)
    _type(app, "Typed but not applied")
    app._on_close_doc()
    assert app.model.doc is None
    saved = pymupdf.open(str(out))
    assert "Typed but not applied" in saved[0].get_text()
    saved.close()


def test_closing_a_clean_document_with_typing_asks(app, monkeypatch):
    asked = []
    monkeypatch.setattr(editor.messagebox, "askyesnocancel",
                        lambda *a, **k: asked.append(1))   # None = cancel
    _click(app, TARGET)
    _type(app, "Would be lost")
    app._on_close_doc()
    assert asked and app.model.doc is not None            # asked, then cancelled


def test_undo_shortcut_leaves_other_text_fields_alone(app, monkeypatch):
    _click(app, TARGET)
    _type(app, "Edited")
    _press(app, "<Return>")
    monkeypatch.setattr(app.window, "focus_get", lambda: app.size_box)
    app._shortcut(app._on_undo)                    # Cmd+Z typed in the size box
    assert "Edited" in _page_text(app)
    app._on_undo()                                 # the Undo button still works
    assert TARGET in _page_text(app)


def test_clicking_inside_the_text_being_edited_keeps_typing(app):
    _click(app, TARGET)
    _type(app, "Half typed")
    _click(app, TARGET)
    assert app.view.entry is not None and app.view.entry.get() == "Half typed"
    assert app.history_list.size() == 1


def test_resize_with_a_box_open_keeps_it(app):
    _click(app, TARGET)
    _type(app, "Still typing")
    app._do_resize()
    assert app.view.entry is not None and app.view.entry.get() == "Still typing"


def test_changing_page_applies_the_open_box(app):
    _click(app, TARGET)
    _type(app, "Applied by paging")
    app._next_page()
    assert app.view.entry is None
    assert app.model.current_page == 1
    assert "Applied by paging" in app.model.get_page(0).get_text()


def test_redisplaying_the_page_closes_any_open_box(app):
    """show_page clears the canvas; a box left 'open' behind it would be
    orphaned while the app still thought it was editing."""
    _click(app, TARGET)
    app.view.display_page(0)
    assert app.view.entry is None and app.view.editing_run is None
    assert app.canvas.find_withtag("editor") == ()


# -- Return and Esc wherever the focus is -------------------------------------------

def test_return_anywhere_in_the_window_applies_the_box(app):
    """Clicking a button, checkbox or the History list takes the keyboard
    focus off the text box; Return must still apply it."""
    assert "_on_return_key" in app.window.bind("<Return>")
    assert "_on_return_key" in app.window.bind("<KP_Enter>")
    _click(app, TARGET)
    _type(app, "Applied from elsewhere")
    assert app._on_return_key() == "break"
    assert app.view.entry is None
    assert "Applied from elsewhere" in _page_text(app)


def test_return_anywhere_does_nothing_without_a_box(app):
    assert app._on_return_key() is None
    assert app.history_list.size() == 1


def test_escape_anywhere_in_the_window_cancels_the_box(app):
    assert "_on_escape_key" in app.window.bind("<Escape>")
    _click(app, TARGET)
    _type(app, "Never applied")
    assert app._on_escape_key() == "break"
    assert app.view.entry is None
    assert TARGET in _page_text(app)


def test_return_in_go_to_page_doesnt_apply_the_box(app):
    _click(app, TARGET)
    _type(app, "Applied by paging")
    app.page_entry.insert(0, "2")
    # Go to page handles its own Return and stops it there...
    assert app._on_page_jump() == "break"
    assert app.model.current_page == 1     # (paging applies the box anyway)
    app.page_entry.insert(0, "abc")
    warned = []
    app._show_warning = lambda *a: warned.append(a)
    assert app._on_page_jump() == "break"
    assert warned


def test_return_in_the_size_box_goes_back_to_the_text_box(app, monkeypatch):
    app.set_tool("add")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _type(app, "Not yet")
    refocused = []
    monkeypatch.setattr(app, "_refocus_entry", lambda: refocused.append(1))
    assert "_size_entered" in app.size_box.bind("<Return>")
    assert app._size_entered() == "break"
    assert refocused and app.view.entry is not None   # not added yet
    assert app.history_list.size() == 1


def test_style_controls_hand_typing_back_to_the_box(app):
    app.set_tool("add")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    for widget in (app.size_box,):
        assert "_refocus_entry" in str(widget.cget("command"))
    boxes = [w for w in _descendants(app.tool_panels["add"])
             if isinstance(w, editor.ttk.Checkbutton)]
    assert len(boxes) == 2
    for box in boxes:
        assert "_refocus_entry" in str(box.cget("command"))
    assert "_refocus_entry" in app.family_box.bind("<<ComboboxSelected>>")


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


# -- One window per PDF ---------------------------------------------------------------

def _other_pdf(tmp_path, name, text):
    path = tmp_path / name
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text, fontsize=14)
    doc.save(str(path))
    doc.close()
    return str(path)


def test_opening_several_pdfs_gives_each_its_own_window(app, editor_app, tmp_path):
    _click(app, TARGET)
    _type(app, "Unsaved work")
    _press(app, "<Return>")
    b = _other_pdf(tmp_path, "b.pdf", "Document B")
    c = _other_pdf(tmp_path, "c.pdf", "Document C")

    editor_app.open_paths(b, c)

    assert len(editor_app.windows) == 3
    assert "Unsaved work" in _page_text(app)              # untouched
    assert app.model.is_dirty
    titles = [w.window.title() for w in editor_app.windows]
    assert titles[1].endswith("b.pdf") and titles[2].endswith("c.pdf")
    assert "Document C" in editor_app.windows[2].model.get_page(0).get_text()
    # Each window has its own history.
    assert editor_app.windows[1].history_list.size() == 1


def test_open_dialog_takes_several_files_and_fills_an_empty_window_first(
        editor_app, tmp_path, monkeypatch):
    empty = editor_app.new_window()
    b = _other_pdf(tmp_path, "b.pdf", "Document B")
    c = _other_pdf(tmp_path, "c.pdf", "Document C")
    monkeypatch.setattr(editor.filedialog, "askopenfilenames",
                        lambda **k: (b, c))
    empty._on_open()
    assert len(editor_app.windows) == 2
    assert empty.model.file_path == b
    assert editor_app.windows[1].model.file_path == c


def test_opening_one_after_another_never_replaces_a_document(app, editor_app,
                                                             tmp_path, monkeypatch):
    for name in ("b.pdf", "c.pdf", "d.pdf"):
        path = _other_pdf(tmp_path, name, name)
        monkeypatch.setattr(editor.filedialog, "askopenfilenames",
                            lambda **k: (path,))
        editor_app.windows[-1]._on_open()
    assert [os.path.basename(w.model.file_path) for w in editor_app.windows] == \
        ["sample.pdf", "b.pdf", "c.pdf", "d.pdf"]


def test_opening_an_open_pdf_brings_its_window_forward(app, editor_app,
                                                       sample_pdf_path, tmp_path):
    b = _other_pdf(tmp_path, "b.pdf", "Document B")
    editor_app.open_paths(b)
    editor_app.open_paths(str(sample_pdf_path))
    assert len(editor_app.windows) == 2
    assert app.__dict__.get("fronted")


def test_a_pdf_that_wont_open_leaves_no_blank_window(app, editor_app, tmp_path,
                                                     monkeypatch):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf")
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message, **k: shown.append(message))
    editor_app.open_paths(str(bad))
    assert shown
    assert editor_app.windows == [app]


def test_close_with_other_windows_open_closes_the_window(app, editor_app, tmp_path):
    editor_app.open_paths(_other_pdf(tmp_path, "b.pdf", "B"))
    second = editor_app.windows[1]
    second._on_close_doc()
    assert editor_app.windows == [app]
    # The last window stays open, empty, on Close.
    app._on_close_doc()
    assert editor_app.windows == [app] and app.model.doc is None


def test_closing_the_last_window_quits(editor_app, app):
    app._on_close_window()
    assert editor_app.windows == []
    with pytest.raises(editor.tk.TclError):
        editor_app.root.winfo_exists()


def test_quit_asks_about_each_unsaved_window_and_can_be_cancelled(
        app, editor_app, tmp_path, monkeypatch):
    editor_app.open_paths(_other_pdf(tmp_path, "b.pdf", "Document B"))
    second = editor_app.windows[1]
    _click(app, TARGET)
    _type(app, "Unsaved")
    _press(app, "<Return>")
    asked = []
    monkeypatch.setattr(editor.messagebox, "askyesnocancel",
                        lambda title, message, **k: asked.append(message))  # None = cancel
    editor_app.quit()
    assert len(asked) == 1 and "sample.pdf" in asked[0]
    assert editor_app.windows == [app, second]     # still running
    assert editor_app.root.winfo_exists()

    monkeypatch.setattr(editor.messagebox, "askyesnocancel",
                        lambda *a, **k: False)     # don't save
    editor_app.quit()
    assert editor_app.windows == []


def test_quit_and_open_apple_events_are_handled(editor_app):
    commands = editor_app.root.tk.call("info", "commands", "::tk::mac::*")
    names = set(editor_app.root.tk.splitlist(commands))
    assert {"::tk::mac::Quit", "::tk::mac::OpenDocument",
            "::tk::mac::ReopenApplication"} <= names


def test_a_new_page_starts_scrolled_to_the_top(app):
    app.canvas.configure(height=100)
    app.canvas.yview_moveto(0.5)
    app._next_page()
    assert app.canvas.yview()[0] == 0.0


def test_new_windows_cascade_instead_of_stacking(editor_app, tmp_path):
    editor_app.open_paths(*(_other_pdf(tmp_path, f"{n}.pdf", n) for n in "abc"))
    steps = [w.cascade_step for w in editor_app.windows]
    assert steps == [0, 1, 2]
    editor_app.windows[1]._on_close_window()
    editor_app.new_window()          # reuses the free spot
    assert sorted(w.cascade_step for w in editor_app.windows) == [0, 1, 2]


# -- Installed fonts ------------------------------------------------------------------

def test_add_text_font_menu_lists_installed_fonts(app, installed_fonts):
    installed_fonts("Supplemental/Georgia.ttf", "Helvetica.ttc")
    app._fill_families()
    values = list(app.window.tk.splitlist(app.family_box.cget("values")))
    assert values[:3] == ["Helvetica", "Times", "Courier"]
    assert "Georgia" in values and values.count("Helvetica") == 1


def test_text_box_is_shown_in_the_matched_installed_font(app, installed_fonts):
    installed_fonts("Supplemental/Georgia.ttf")
    run = next(r for r in app.model.runs(0) if r.text.strip() == TARGET)
    assert app.model.display_family(run) is None        # Helvetica isn't installed
    installed_fonts("Helvetica.ttc")
    assert app.model.display_family(run) == "Helvetica"
    # A family Tk doesn't know falls back to the run's generic look.
    assert app.view.screen_family("No Such Family", run) == "Helvetica"
    assert app.view.screen_family("Georgia") == "Georgia"


def test_adding_text_in_an_installed_font(app, installed_fonts):
    installed_fonts("Supplemental/Georgia.ttf")
    app.set_tool("add")
    app.family_var.set("Georgia")
    app.view._on_click(_Event(60 * SCALE, 300 * SCALE))
    _type(app, "In Georgia")
    _press(app, "<Return>")
    assert "In Georgia" in _page_text(app)
    fonts = {s["font"] for b in app.model.get_page(0).get_text("dict")["blocks"]
             if b["type"] == 0 for line in b["lines"] for s in line["spans"]}
    assert "Georgia" in fonts


def test_return_reaches_the_window_from_every_clickable_widget(app):
    """Return is handled on the window: it arrives there from any widget
    that can take the focus, unless that widget's own class binding
    handles Return first."""
    top = str(app.window)
    widgets = [app.undo_btn, app.save_btn, app.history_list, app.family_box,
               app.canvas] + list(app.tool_buttons.values())
    widgets += [w for w in _descendants(app.window)
                if w.winfo_class() in ("TCheckbutton", "TRadiobutton")]
    for widget in widgets:
        assert top in widget.bindtags(), widget
        assert "break" not in app.window.bind_class(widget.winfo_class(),
                                                    "<Return>"), widget


def test_quit_isnt_reentered_while_asking(app, editor_app, monkeypatch):
    _click(app, TARGET)
    _type(app, "Unsaved")
    _press(app, "<Return>")
    calls = []

    def ask(*a, **k):
        calls.append(1)
        editor_app.quit()             # Quit chosen again while the sheet is up
        return None                   # then Cancel
    monkeypatch.setattr(editor.messagebox, "askyesnocancel", ask)
    editor_app.quit()
    assert calls == [1] and editor_app.windows == [app]


def test_save_as_onto_a_file_open_in_another_window_is_refused(
        app, editor_app, tmp_path, monkeypatch):
    other = _other_pdf(tmp_path, "b.pdf", "Document B")
    editor_app.open_paths(other)
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message, **k: shown.append(message))
    monkeypatch.setattr(editor.filedialog, "asksaveasfilename", lambda **k: other)
    assert not app._on_save_as()
    assert shown and "open in another window" in shown[0]
    assert "Document B" in pymupdf.open(other)[0].get_text()


# -- Metadata on Save -----------------------------------------------------------------

def test_metadata_menu_offers_the_three_choices(app, editor_app):
    menu = app.metadata_menu
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)]
    assert labels == ["Keep, with Today's Modification Date",
                      "Keep Exactly as It Was", "Remove Document Info"]
    assert editor_app.metadata_var.get() == editor.METADATA_UPDATE_DATE


def test_metadata_choice_is_used_when_saving_and_remembered(app, editor_app,
                                                            tmp_path, monkeypatch):
    menu = app.metadata_menu
    menu.invoke(2)                                  # Remove Document Info
    assert editor_app.metadata_var.get() == editor.METADATA_REMOVE
    assert json.load(open(editor.SETTINGS_PATH)) == {"metadata": editor.METADATA_REMOVE}

    out = tmp_path / "saved.pdf"
    monkeypatch.setattr(editor.filedialog, "asksaveasfilename", lambda **k: str(out))
    used = []
    real = app.model.save_document
    monkeypatch.setattr(app.model, "save_document",
                        lambda path, metadata: used.append(metadata) or real(path, metadata))
    assert app._on_save_as()
    assert used == [editor.METADATA_REMOVE]
    assert "document info removed" in app.status_label["text"]

    # A new launch starts with the remembered choice.
    again = editor.EditorApp()
    try:
        assert again.metadata_var.get() == editor.METADATA_REMOVE
    finally:
        again._destroy_root()


def test_all_windows_share_the_metadata_choice(app, editor_app, tmp_path):
    second = editor_app.new_window()
    second.metadata_menu.invoke(1)                  # Keep Exactly as It Was
    assert editor_app.metadata_var.get() == editor.METADATA_KEEP
    assert str(app.metadata_menu.entrycget(1, "variable")) == \
        str(second.metadata_menu.entrycget(1, "variable"))


# -- Find & Replace -------------------------------------------------------------------

def _search(app, query, **options):
    app.show_find_bar()
    app.match_case_var.set(options.get("match_case", False))
    app.whole_word_var.set(options.get("whole_word", False))
    app.find_var.set(query)
    assert app._flush_find()          # as if typing paused: the search runs


def test_cmd_f_opens_the_find_bar(app):
    assert "show_find_bar" in app.window.bind("<Command-f>")
    assert not app.find_bar_shown
    app.show_find_bar()
    assert app.find_bar_shown


def test_typing_finds_and_outlines_matches(app):
    _search(app, "line")
    assert app.find_count["text"] == "1 of 3"
    items = app.canvas.find_withtag("find")
    kinds = sorted(app.canvas.type(i) for i in items)
    assert kinds == ["image"] * 3 + ["rectangle"]      # 3 tints, 1 outline
    _assert_see_through(app, "find")


def test_no_matches_disables_replace(app):
    _search(app, "zebra")
    assert app.find_count["text"] == "No matches"
    assert str(app.replace_all_btn["state"]) == "disabled"


def test_next_and_previous_wrap_and_change_page(app):
    _search(app, "e", match_case=True)
    total = len(app.matches)
    last_on_page_2 = max(i for i, m in enumerate(app.matches) if m.page_num == 1)
    for _ in range(last_on_page_2):
        app._find_next()
    assert app.model.current_page == 1
    assert app.find_count["text"] == f"{last_on_page_2 + 1} of {total}"
    app.match_index = total - 1
    app._find_next()                                    # wraps to the first
    assert app.match_index == 0 and app.model.current_page == 0
    app._find_prev()
    assert app.match_index == total - 1


def test_find_fields_handle_their_own_keys(app):
    app.show_find_bar()
    assert "_find_return" in app.find_entry.bind("<Return>")
    assert "_find_shift_return" in app.find_entry.bind("<Shift-Return>")
    assert "_replace_return" in app.replace_entry.bind("<Return>")
    assert "_find_escape" in app.find_entry.bind("<Escape>")
    assert app._find_return() == "break" and app._find_escape() == "break"
    assert not app.find_bar_shown and app.canvas.find_withtag("find") == ()


def test_replace_replaces_the_current_match_then_moves_on(app):
    _search(app, "line")
    app.replace_var.set("row")
    app._replace_current()
    assert "The row above the target" in _page_text(app)
    assert app.history_list.size() == 2
    assert app.find_count["text"] == "1 of 2"
    assert app.matches[app.match_index].run.text.strip() == TARGET
    assert app.model.history_entries()[-1] == "Replace “line” → “row”"


def test_replace_all_asks_then_replaces_everything_as_one_step(app, monkeypatch):
    _search(app, "the")
    app.replace_var.set("a")
    asked = []
    monkeypatch.setattr(editor.messagebox, "askokcancel",
                        lambda title, message, **k: asked.append(message) or True)
    app._replace_all()
    assert "Replace 5 occurrences of “the” with “a” on 2 pages?" in asked[0]
    assert "Undo puts everything back" in asked[0]
    assert app.find_count["text"] == "No matches"
    assert app.history_list.size() == 2
    app._on_undo()
    assert app.find_count["text"].endswith("of 5")     # found again after undo


def test_cancelled_replace_all_changes_nothing(app, monkeypatch):
    _search(app, "the")
    app.replace_var.set("a")
    monkeypatch.setattr(editor.messagebox, "askokcancel", lambda *a, **k: False)
    app._replace_all()
    assert app.history_list.size() == 1 and LINE_ABOVE in _page_text(app)


def test_replace_applies_an_open_text_box_first(app):
    _click(app, TARGET)
    _type(app, "Target line typed")
    _search(app, "typed")
    assert app.view.entry is not None
    app.replace_var.set("done")
    app._replace_current()
    assert app.view.entry is None
    assert "Target line done" in _page_text(app)


def test_matches_follow_document_changes(app):
    _search(app, "line")
    app.canvas.yview_moveto(0)    # (showing a match scrolled the tiny hidden canvas)
    _click(app, LINE_BELOW)
    _type(app, "The row below the target")
    _press(app, "<Return>")
    assert app.find_count["text"].endswith("of 2")


def _assert_see_through(app, tag):
    """Highlights must let the text show through: no filled rectangles
    (Tk on macOS draws stippled fills solid), and translucent images."""
    for item in app.canvas.find_withtag(tag):
        if app.canvas.type(item) == "rectangle":
            assert app.canvas.itemcget(item, "fill") == "", tag
        else:
            name = app.canvas.itemcget(item, "image")
            # (Tk 8.6 can't read partial alpha back, so look the image up.)
            (_size, _color, alpha), = [key for key, image in app.view._tints.items()
                                       if str(image) == name]
            assert 0 < alpha < 1, tag


def test_erase_selection_lets_the_text_show_through(app):
    app.set_tool("erase")
    _press_move_release_without_release(app)
    assert app.canvas.find_withtag("erase_sel")
    _assert_see_through(app, "erase_sel")


def test_erase_rectangle_lets_the_text_show_through(app):
    app.set_tool("erase")
    app.view._on_click(_Event(5, 5))                  # empty space: a rectangle
    app.view._on_drag(_Event(300, 200))
    kinds = sorted(app.canvas.type(i) for i in app.canvas.find_withtag("erase_sel"))
    assert kinds == ["image", "rectangle"]
    _assert_see_through(app, "erase_sel")


def _press_move_release_without_release(app):
    start = _word_center(app, "line", baseline=150)
    end = _word_center(app, "edit")
    app.view._on_click(_Event(*start))
    app.view._on_drag(_Event(*end))


def test_a_quick_return_after_typing_isnt_undone_by_the_pending_search(app):
    app.show_find_bar()
    app.find_var.set("line")              # search pending (typing pause)
    assert app._find_job is not None
    app._find_return()                    # Return before the pause ends
    assert app._find_job is None and app.match_index == 0
    app._find_return()
    assert app.match_index == 1           # and nothing resets it later
    app.window.update()
    assert app.match_index == 1


def test_replace_all_uses_the_text_just_typed(app, monkeypatch):
    app.show_find_bar()
    app.find_var.set("line")
    app.replace_var.set("row")
    monkeypatch.setattr(editor.messagebox, "askokcancel", lambda *a, **k: True)
    app._replace_all()                    # before the typing pause ended
    assert app._find_job is None
    assert "The row above the target" in _page_text(app)


def test_replace_all_redraws_every_changed_page(app, monkeypatch):
    app._next_page()                       # page 2 is now rendered and cached
    app._prev_page()
    assert 1 in app.renderer._cache
    _search(app, "the")
    app.replace_var.set("a")
    monkeypatch.setattr(editor.messagebox, "askokcancel", lambda *a, **k: True)
    app._replace_all()
    assert 1 not in app.renderer._cache   # page 2 changed: drawn again when shown


def test_an_unrelated_edit_keeps_the_current_match(app):
    _search(app, "line")
    app._find_next()
    app._find_next()
    assert app.matches[app.match_index].run.text.strip() == LINE_BELOW
    app.canvas.yview_moveto(0)
    _click(app, TITLE)
    _type(app, "Quarterly Reporter")     # (letters the subset font has)
    _press(app, "<Return>")
    assert app.matches[app.match_index].run.text.strip() == LINE_BELOW


# -- Find & Replace review findings ---------------------------------------------------

def test_replace_with_text_containing_the_query_moves_on(app):
    _search(app, "line")
    app.replace_var.set("lines")
    for _ in range(3):
        app._replace_current()
    text = _page_text(app)
    assert "The lines above the target" in text
    assert "Target lines to edit" in text
    assert "The lines below the target" in text
    assert "liness" not in text


def test_typing_a_search_doesnt_apply_a_half_typed_edit(app):
    _click(app, TARGET)
    _type(app, "Target line half-ty")
    app.show_find_bar()
    app.find_var.set("Second")                     # only on page 2
    app._run_scheduled_find()                      # typing paused
    assert app.view.entry is not None              # still typing in the box
    assert app.model.current_page == 0
    assert app.history_list.size() == 1
    assert app.find_count["text"] == "1 of 1"


def test_closing_a_window_cancels_its_pending_search(app, editor_app):
    second = editor_app.new_window()
    second.show_find_bar()
    second.find_var.set("x")                       # search pending
    job = second._find_job
    assert job
    second._on_close_window()
    pending = app.window.tk.splitlist(app.window.tk.call("after", "info"))
    assert job not in pending


def test_many_drag_sizes_dont_blank_highlights_on_screen(app):
    _search(app, "line")
    names = {app.canvas.itemcget(i, "image") for i in app.canvas.find_withtag("find")
             if app.canvas.type(i) == "image"}
    for size in range(510):                        # an erase drag, many sizes
        app.view.tint((0, 0, 10 + size, 10), editor.ERASE_COLOR, 0.2, "scratch")
        app.canvas.delete("scratch")
    alive = set(app.window.tk.splitlist(app.window.tk.call("image", "names")))
    assert names <= alive
