"""Drives the real EditorApp through actual Tk widgets: hover, click to
edit, type, Return/Esc, warnings, undo, saving.

Needs a working Tcl/Tk and a display session; skips itself cleanly when a
Tk root can't be created.
"""

import pymupdf
import pytest

import editor
from conftest import LINE_ABOVE, PAGE_WIDTH, TARGET, TITLE


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
    for name in ("askopenfilename", "asksaveasfilename"):
        monkeypatch.setattr(editor.filedialog, name, unexpected(name))


@pytest.fixture
def app(sample_pdf_path, monkeypatch):
    try:
        instance = editor.EditorApp()
    except Exception as exc:
        pytest.skip(f"Tk isn't usable in this environment: {exc}")
    instance.root.withdraw()
    # A withdrawn window's canvas reports a width of 1px; pretend it's
    # 620px so pages fit at 600px wide (SCALE below) on every refresh.
    monkeypatch.setattr(instance.canvas, "winfo_width", lambda: 620)
    instance.open_path(str(sample_pdf_path))
    yield instance
    instance.model.close_document()
    instance.root.destroy()


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
    assert app.root.title().endswith("— edited")
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
                        lambda title, message: shown.append(message))
    _click(app, TARGET)
    _type(app, "漢字")
    app._commit_inline_edit()

    assert shown and "None of the available fonts" in shown[0]
    assert app.view.entry is not None
    assert TARGET in _page_text(app)


def test_editor_matches_the_text_size_on_screen(app):
    _click(app, TITLE)  # 20pt
    font = app.view.entry.cget("font")
    size_px = abs(int(app.root.tk.call("font", "actual", font, "-size")))
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
    assert "— edited" not in app.root.title()
    assert app.root.title().endswith("saved.pdf")

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


def test_open_documents_apple_event(app, sample_pdf_path):
    app._on_open_documents(str(sample_pdf_path), "/tmp/not-a-pdf.txt")
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
                        lambda title, message: shown.append(message))

    app.open_path(str(path))

    assert shown and "password-protected" in shown[0]
    # The document that was already open is still open and shown.
    assert app.model.file_path.endswith("sample.pdf")
    assert app.root.title().endswith("sample.pdf")
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
    size_px = abs(int(app.root.tk.call("font", "actual",
                                       app.view.entry.cget("font"), "-size")))
    assert size_px == pytest.approx(30 * SCALE, abs=1.5)


def test_add_text_error_keeps_the_box_open(app, monkeypatch):
    shown = []
    monkeypatch.setattr(editor.messagebox, "showerror",
                        lambda title, message: shown.append(message))
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
                        lambda title, message: shown.append(message))
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
                        lambda title, message: shown.append(message))
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
    monkeypatch.setattr(app.root, "focus_get", lambda: app.size_box)
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
