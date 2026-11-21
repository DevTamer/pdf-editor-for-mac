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
    # The previously open document is gone from the window too.
    assert app.root.title() == "PDF Editor"
    assert app.canvas.find_all() == ()
    assert app.history_list.size() == 0
    assert str(app.undo_btn["state"]) == "disabled"
