"""
PDF Editor — edit, add and erase text in a PDF like a text document.

Usage:
    source venv/bin/activate && python editor.py [file.pdf]
"""

import os
import sys

# When bundled as a standalone .app (py2app), Tcl/Tk needs to be pointed at
# its script library bundled inside the app itself. This must happen before
# `import tkinter` triggers Tcl's init; it's a no-op for a normal dev run.
# (pdfcore's package import never pulls in tkinter, so importing it first
# is safe.)
from pdfcore.frozen import configure_bundled_tcl_tk

configure_bundled_tcl_tk()

import math  # noqa: E402
import tempfile  # noqa: E402
import tkinter as tk  # noqa: E402  (must follow configure_bundled_tcl_tk)
from tkinter import colorchooser, filedialog, messagebox, ttk  # noqa: E402
from tkinter import font as tkfont  # noqa: E402
from typing import Callable, List, Optional, Tuple  # noqa: E402

import pymupdf  # noqa: E402

from pdfcore import DocumentSession, PDFRenderer  # noqa: E402
from pdfcore.pageview import PageView  # noqa: E402

from erase import (  # noqa: E402
    FILL_COLOR, FILL_CONTENT, ErasePlan, Word, apply_erase, page_words,
    word_at, words_between,
)
import fontmatch  # noqa: E402
from textedit import (  # noqa: E402
    FAMILIES, MAX_SIZE, MIN_SIZE, AddPlan, EditError, EditPlan, TextRun,
    TextStyle,
    apply_add_text, apply_edit, page_runs, plan_add_text, plan_edit,
    resolve_original_font, run_at,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "PDF Editor"
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1200, 800
DEFAULT_WINDOW_SIZE = f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}"
MIN_WINDOW_WIDTH = 900
MIN_WINDOW_HEIGHT = 600
SIDEBAR_WIDTH = 280
HISTORY_LIMIT = 30  # Max snapshots kept in the undo/redo history...
HISTORY_BUDGET = 400 * 1024 * 1024  # ...and roughly the memory they may use
MIN_HISTORY = 5     # but always at least this many, however big the file

TOOL_EDIT = "edit"
TOOL_ADD = "add"
TOOL_ERASE = "erase"
TOOLS = (TOOL_EDIT, TOOL_ADD, TOOL_ERASE)

HOVER_OUTLINE = "#2f7ae5"
HOVER_DASH = (3, 2)
EDIT_BACKGROUND = "#fffbe6"
EDIT_BORDER = "#2f7ae5"
MIN_EDITOR_WIDTH = 160  # px
EDITOR_EXTRA_WIDTH = 60  # px of room to type beyond the original text
ERASE_COLOR = "#e5484d"
MIN_MARQUEE = 4  # px: smaller drags count as clicks


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _short(text: str, limit: int = 24) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _hex(color: Tuple[float, float, float]) -> str:
    red, green, blue = (round(max(0.0, min(1.0, c)) * 255) for c in color)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _tk_font(family: str, size_px: float, bold: bool, italic: bool):
    style = " ".join(s for s, on in (("bold", bold), ("italic", italic)) if on)
    if not math.isfinite(size_px):
        size_px = 16  # a nonsense size typed in the sidebar; the add is refused later
    return (family, -max(8, min(400, round(size_px))), style or "normal")


# ---------------------------------------------------------------------------
# EditorModel — document state + changes
# ---------------------------------------------------------------------------

class EditorModel(DocumentSession):
    """The open document, its history, and saving.

    Every successful change (edit, added text, erase) is one undo step. A
    failed change is rolled back by reloading the last snapshot, so the
    live document is never left half-changed.
    """

    def __init__(self):
        super().__init__(history_limit=HISTORY_LIMIT)
        self._saved_entry = None
        self.save_path: Optional[str] = None

    # -- Document lifecycle ---------------------------------------------------

    def open_document(self, path: str) -> None:
        # If this fails (unreadable or password-protected file), pdfcore
        # leaves the current document, its history and our state untouched.
        super().open_document(path)
        # Every undo step holds a full copy of the document: cap the count so
        # a big scan doesn't eat gigabytes of memory.
        size = max(1, len(self.history.current.doc_bytes))
        self.history.limit = max(MIN_HISTORY, min(HISTORY_LIMIT, HISTORY_BUDGET // size))
        self._saved_entry = self.history.current
        self.save_path = None

    def close_document(self) -> None:
        super().close_document()
        self._saved_entry = None
        self.save_path = None

    @property
    def is_dirty(self) -> bool:
        """Whether there are changes that haven't been saved. Undoing back
        to the last saved state counts as clean again."""
        return self.doc is not None and self.history.current is not self._saved_entry

    # -- Reading --------------------------------------------------------------

    def runs(self, page_num: int) -> List[TextRun]:
        return page_runs(self.get_page(page_num), page_num)

    def run_at(self, page_num: int, x: float, y: float) -> Optional[TextRun]:
        return run_at(self.runs(page_num), x, y)

    def words(self, page_num: int) -> List[Word]:
        return page_words(self.get_page(page_num))

    def display_family(self, run: TextRun) -> Optional[str]:
        """The installed family that looks like ``run``'s font, to show the
        text box in: the same font if it's installed. (Only the quick
        name lookup: the slower lookalike search waits for the edit.)"""
        face = fontmatch.default_index().exact(run.font, run.bold, run.italic)
        return face.family if face is not None else None

    def describe_font(self, run: TextRun) -> str:
        page = self.get_page(run.page_num)
        choice, reason = resolve_original_font(self.doc, page, run)
        if choice is None:
            return f"{run.font} — {reason}"
        if choice.check_ink:
            return f"{run.font} (embedded)"
        return f"{run.font} (standard PDF font)"

    # -- Changes --------------------------------------------------------------

    def _change(self, apply: Callable[[], None], label: str) -> str:
        """Run ``apply``; record it as one undo step, or restore the last
        snapshot and raise EditError if it fails in any way."""
        try:
            apply()
        except Exception as exc:
            self._restore_current_snapshot()
            if isinstance(exc, EditError):
                raise
            raise EditError(f"The change failed: {exc}") from exc
        self.record_snapshot(label)
        return label

    def _restore_current_snapshot(self) -> None:
        page = self.current_page
        self.doc.close()
        self.doc = self.history.current.open()
        self.current_page = page

    def plan_edit(self, run: TextRun, new_text: str) -> EditPlan:
        page = self.get_page(run.page_num)
        return plan_edit(self.doc, page, run, new_text)

    def apply_plan(self, plan: EditPlan) -> Optional[str]:
        """Apply a plan from plan_edit(). Returns the new history label, or
        None if the plan changes nothing. Raises EditError (with the
        document restored to how it was) if the edit can't be made."""
        if plan.is_noop:
            return None
        if not plan.can_apply:
            raise EditError(plan.errors[0])
        if plan.new_text.strip():
            label = f"Edit “{_short(plan.run.text)}” → “{_short(plan.new_text)}”"
        else:
            label = f"Delete “{_short(plan.run.text)}”"
        return self._change(
            lambda: apply_edit(self.get_page(plan.run.page_num), plan), label)

    def plan_add_text(self, page_num: int, top_left: Tuple[float, float],
                      text: str, style: TextStyle) -> AddPlan:
        return plan_add_text(self.get_page(page_num), page_num, top_left,
                             text, style)

    def apply_add(self, plan: AddPlan) -> str:
        """Write planned new text. Raises EditError (with the document
        restored) if it can't be added."""
        if not plan.can_apply:
            raise EditError(plan.errors[0] if plan.errors
                            else "There's no text to add.")
        return self._change(
            lambda: apply_add_text(self.get_page(plan.page_num), plan),
            f"Add “{_short(plan.text)}”")

    def add_text(self, page_num: int, top_left: Tuple[float, float], text: str,
                 style: TextStyle) -> str:
        """Write new text with its top-left corner at ``top_left`` (PDF
        points). Raises EditError if it can't be added."""
        return self.apply_add(self.plan_add_text(page_num, top_left, text, style))

    def erase(self, plan: ErasePlan) -> str:
        """Erase words or an area (see erase.py). Raises EditError if
        there's nothing to erase or it can't be done safely."""
        return self._change(lambda: apply_erase(self.doc, plan.page_num, plan),
                            plan.label)

    # -- Saving ---------------------------------------------------------------

    def save_document(self, path: str) -> None:
        """Write the current state to ``path``.

        Saves from a copy, never from the live document: in PyMuPDF
        1.24, a garbage-collecting save of a document with newly written
        text can leave the live document's pages blank. Writes to a
        temporary file next to ``path`` and renames it into place, so
        saving over the file that's open works, and a failed save never
        leaves a half-written file behind.
        """
        copy = pymupdf.open(stream=self.doc.tobytes(), filetype="pdf")
        directory = os.path.dirname(os.path.abspath(path))
        # Keep an existing file's permissions; give a new file the usual
        # ones (mkstemp creates its temp file readable by the owner only).
        if os.path.exists(path):
            mode = os.stat(path).st_mode & 0o777
        else:
            mode = 0o666 & ~_current_umask()
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=directory)
        os.close(fd)
        try:
            copy.save(tmp_path, garbage=3, deflate=True)
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        finally:
            copy.close()
        self._saved_entry = self.history.current
        self.save_path = path


# ---------------------------------------------------------------------------
# EditCanvas — page view + per-tool mouse handling + inline text box
# ---------------------------------------------------------------------------

class EditCanvas(PageView):
    """Shows the page and handles the mouse for the current tool:

    - edit: hover outlines text; clicking it opens the inline text box.
    - add: clicking anywhere opens an empty text box there.
    - erase: hover outlines words; click erases a word, dragging from a
      word selects words like a text selection, dragging from empty space
      draws a rectangle. Releasing the mouse erases the selection.
    """

    def __init__(self, canvas: tk.Canvas, model: EditorModel,
                 renderer: PDFRenderer, on_click_run, on_click_blank,
                 on_add_click=None, on_erase=None):
        super().__init__(canvas, renderer)
        self.model = model
        self.on_click_run = on_click_run
        self.on_click_blank = on_click_blank
        self.on_add_click = on_add_click or (lambda pdf_xy: None)
        self.on_erase = on_erase or (lambda words, area: None)
        self.tool = TOOL_EDIT
        self._runs: List[TextRun] = []
        self._words: List[Word] = []
        self._hover = None
        self.editing_run: Optional[TextRun] = None
        # (page_num, top-left in PDF points) while an Add Text box is open.
        self.adding_at: Optional[Tuple[int, Tuple[float, float]]] = None
        self.entry: Optional[tk.Entry] = None
        self._tk_families: Optional[set] = None
        self._on_commit = self._on_cancel = lambda: None
        # Erase drag state
        self._anchor_word: Optional[Word] = None
        self._drag_start: Optional[Tuple[float, float]] = None
        self.selection: List[Word] = []

        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", lambda e: self._set_hover(None))
        canvas.bind("<ButtonPress-1>", self._on_click)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)

    # -- Page display ---------------------------------------------------------

    def display_page(self, page_num: int, fit_width: Optional[int] = None) -> None:
        if not self.model.doc:
            return
        self.cancel_inline_edit()
        self.show_page(self.model.get_page(page_num), page_num, fit_width)
        self._runs = self.model.runs(page_num)
        self._words = self.model.words(page_num)
        self._hover = None
        self._reset_drag()

    def set_tool(self, tool: str) -> None:
        if tool not in TOOLS:
            raise ValueError(tool)
        self.tool = tool
        self._set_hover(None)
        self._reset_drag()
        self.canvas.configure(cursor={TOOL_EDIT: "", TOOL_ADD: "xterm",
                                      TOOL_ERASE: "crosshair"}[tool])

    def run_at_canvas(self, cx: float, cy: float) -> Optional[TextRun]:
        px, py = self.canvas_to_pdf(cx, cy)
        return run_at(self._runs, px, py)

    def word_at_canvas(self, cx: float, cy: float) -> Optional[Word]:
        px, py = self.canvas_to_pdf(cx, cy)
        return word_at(self._words, px, py)

    def _canvas_box(self, bbox, pad: float = 0.0):
        x0, y0 = self.pdf_to_canvas(bbox[0], bbox[1])
        x1, y1 = self.pdf_to_canvas(bbox[2], bbox[3])
        return x0 - pad, y0 - pad, x1 + pad, y1 + pad

    # -- Hover ----------------------------------------------------------------

    def _on_motion(self, event) -> None:
        if not self.model.doc:
            return
        xy = self.event_coords(event)
        if self.tool == TOOL_EDIT:
            self._set_hover(self.run_at_canvas(*xy))
        elif self.tool == TOOL_ERASE:
            self._set_hover(self.word_at_canvas(*xy))

    def _set_hover(self, item) -> None:
        if item is self._hover:
            return
        self._hover = item
        self.canvas.delete("hover")
        if self.tool == TOOL_ADD:
            return
        if item is None:
            self.canvas.configure(cursor="crosshair" if self.tool == TOOL_ERASE else "")
            return
        if self.tool == TOOL_EDIT:
            self.canvas.configure(cursor="xterm")
            if item is not self.editing_run:
                self.canvas.create_rectangle(
                    *self._canvas_box(item.bbox, 2), outline=HOVER_OUTLINE,
                    dash=HOVER_DASH, tags=("hover",))
        else:
            self.canvas.configure(cursor="crosshair")
            self.canvas.create_rectangle(
                *self._canvas_box(item.bbox, 1), outline=ERASE_COLOR,
                width=1.5, tags=("hover",))

    # -- Mouse buttons ----------------------------------------------------------

    def _on_click(self, event) -> None:
        if not self.model.doc:
            return
        xy = self.event_coords(event)
        if self.tool == TOOL_EDIT:
            run = self.run_at_canvas(*xy)
            if run is not None:
                self.on_click_run(run, xy)
            else:
                self.on_click_blank()
        elif self.tool == TOOL_ADD:
            self.on_add_click(self.canvas_to_pdf(*xy))
        else:
            self._begin_erase_drag(xy)

    def _on_drag(self, event) -> None:
        if self.tool == TOOL_ERASE and self._drag_start is not None:
            self._update_erase_drag(self.event_coords(event))

    def _on_release(self, event) -> None:
        if self.tool == TOOL_ERASE and self._drag_start is not None:
            self._finish_erase_drag(self.event_coords(event))

    # -- Erase selection ------------------------------------------------------

    def _reset_drag(self) -> None:
        self._anchor_word = None
        self._drag_start = None
        self.selection = []
        self.canvas.delete("erase_sel")

    def _begin_erase_drag(self, xy) -> None:
        self._reset_drag()
        self._drag_start = xy
        self._anchor_word = self.word_at_canvas(*xy)
        if self._anchor_word is not None:
            self.selection = [self._anchor_word]
        self._draw_selection(xy)

    def _update_erase_drag(self, xy) -> None:
        if self._anchor_word is not None:
            word = self.word_at_canvas(*xy)
            if word is not None:
                self.selection = words_between(self._words, self._anchor_word, word)
        self._draw_selection(xy)

    def _marquee(self, xy) -> Optional[Tuple[float, float, float, float]]:
        x0, y0 = self._drag_start
        x1, y1 = xy
        if abs(x1 - x0) < MIN_MARQUEE or abs(y1 - y0) < MIN_MARQUEE:
            return None
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def _draw_selection(self, xy) -> None:
        self.canvas.delete("erase_sel")
        if self._anchor_word is not None:
            for word in self.selection:
                self.canvas.create_rectangle(
                    *self._canvas_box(word.bbox, 1), fill=ERASE_COLOR,
                    stipple="gray50", outline="", tags=("erase_sel",))
        else:
            box = self._marquee(xy)
            if box is not None:
                self.canvas.create_rectangle(
                    *box, outline=ERASE_COLOR, dash=(4, 3), width=1.5,
                    fill=ERASE_COLOR, stipple="gray25", tags=("erase_sel",))

    def _finish_erase_drag(self, xy) -> None:
        words = list(self.selection)
        box = None if self._anchor_word is not None else self._marquee(xy)
        self._reset_drag()
        if words:
            self.on_erase(words, None)
        elif box is not None:
            x0, y0 = self.canvas_to_pdf(box[0], box[1])
            x1, y1 = self.canvas_to_pdf(box[2], box[3])
            self.on_erase([], pymupdf.Rect(x0, y0, x1, y1))

    # -- Inline text box ------------------------------------------------------

    def _open_entry(self, x: float, y: float, width: float, height: float,
                    font, fg: str, text: str, cursor_index, on_commit,
                    on_cancel) -> tk.Entry:
        self.cancel_inline_edit()
        self.canvas.delete("hover")
        entry = tk.Entry(self.canvas, font=font, relief="solid", bd=1,
                         bg=EDIT_BACKGROUND, fg=fg, highlightthickness=1,
                         highlightcolor=EDIT_BORDER, insertbackground="black")
        entry.insert(0, text)
        entry.icursor(cursor_index)
        self.canvas.create_window(x, y, anchor="nw", window=entry, width=width,
                                  height=height, tags=("editor",))
        self._on_commit, self._on_cancel = on_commit, on_cancel
        entry.bind("<Return>", self._entry_commit)
        entry.bind("<KP_Enter>", self._entry_commit)
        entry.bind("<Escape>", self._entry_cancel)
        entry.focus_set()
        self.entry = entry
        return entry

    def begin_inline_edit(self, run: TextRun, on_commit, on_cancel,
                          click_canvas_x: Optional[float] = None) -> tk.Entry:
        """Put a text box over ``run``, prefilled with its text."""
        x0, y0, x1, y1 = self._canvas_box(run.bbox)
        font = _tk_font(self.screen_family(self.model.display_family(run), run),
                        run.size * self.mapper.total_scale, run.bold, run.italic)
        if click_canvas_x is not None and x1 > x0:
            fraction = max(0.0, min(1.0, (click_canvas_x - x0) / (x1 - x0)))
            cursor = round(fraction * len(run.text))
        else:
            cursor = "end"
        entry = self._open_entry(
            x0 - 3, y0 - 4, max(x1 - x0 + EDITOR_EXTRA_WIDTH, MIN_EDITOR_WIDTH),
            max(y1 - y0 + 8, 18), font, _hex(run.color), run.text, cursor,
            on_commit, on_cancel)
        self.editing_run = run
        return entry

    def begin_add_text(self, page_num: int, pdf_xy: Tuple[float, float],
                       style: TextStyle, on_commit, on_cancel) -> tk.Entry:
        """Put an empty text box with its top-left corner at ``pdf_xy``."""
        cx, cy = self.pdf_to_canvas(*pdf_xy)
        size_px = style.size * self.mapper.total_scale
        height = size_px * 1.4 + 6 if math.isfinite(size_px) else 24
        entry = self._open_entry(
            cx - 3, cy - 3, MIN_EDITOR_WIDTH + 80, max(min(height, 600), 18),
            _tk_font(self.screen_family(style.family), size_px, style.bold,
                     style.italic),
            _hex(style.color), "", "end", on_commit, on_cancel)
        self.adding_at = (page_num, pdf_xy)   # only once the box exists
        return entry

    def screen_family(self, family: Optional[str],
                      run: Optional[TextRun] = None) -> str:
        """A family Tk can draw the text box in: ``family`` if this Mac
        has it registered (fonts inside Microsoft Office's app bundle
        aren't), else the generic look of ``run``."""
        if self._tk_families is None:
            self._tk_families = set(tkfont.families(self.canvas))
        if family and family in self._tk_families:
            return family
        if run is not None:
            return ("Courier" if run.monospaced
                    else "Times" if run.serif else "Helvetica")
        return "Helvetica"

    def restyle_entry(self, style: TextStyle) -> None:
        """Update an open add-text box to show the new style."""
        if self.entry is not None and self.adding_at is not None:
            self.entry.configure(
                font=_tk_font(self.screen_family(style.family),
                              style.size * self.mapper.total_scale,
                              style.bold, style.italic),
                fg=_hex(style.color))

    def _entry_commit(self, event=None) -> str:
        self._on_commit()
        return "break"

    def _entry_cancel(self, event=None) -> str:
        self._on_cancel()
        return "break"

    def end_inline_edit(self) -> Optional[str]:
        """Remove the text box. Returns what was typed (None if there was
        no text box open)."""
        if self.entry is None:
            return None
        text = self.entry.get()
        self.canvas.delete("editor")
        self.entry.destroy()
        self.entry = None
        self.editing_run = None
        self.adding_at = None
        return text

    def cancel_inline_edit(self) -> None:
        self.end_inline_edit()


# ---------------------------------------------------------------------------
# EditorWindow — one window per open PDF
# ---------------------------------------------------------------------------

class EditorWindow:
    """A document window: one PDF, its tools, sidebar and history.

    Windows are created by EditorApp, which opens each PDF in its own
    window (like Preview) and handles quitting.
    """

    def __init__(self, app: "EditorApp", geometry: str = DEFAULT_WINDOW_SIZE):
        self.app = app
        self.cascade_step = 0
        self.window = tk.Toplevel(app.root)
        self.window.title(APP_NAME)
        self.window.geometry(geometry)
        self.window.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        self.model = EditorModel()
        self.renderer = PDFRenderer()
        self.tool_var = tk.StringVar(value=TOOL_EDIT)
        self.fill_var = tk.StringVar(value=FILL_CONTENT)
        self.family_var = tk.StringVar(value="Helvetica")
        self.size_var = tk.StringVar(value="12")
        self.bold_var = tk.BooleanVar(value=False)
        self.italic_var = tk.BooleanVar(value=False)
        self.text_color: Tuple[float, float, float] = (0.0, 0.0, 0.0)

        self._build_menu()
        self._build_toolbar()
        self._build_main_area()
        self._build_status_bar()

        self.view = EditCanvas(self.canvas, self.model, self.renderer,
                               on_click_run=self._on_click_run,
                               on_click_blank=self._on_click_blank,
                               on_add_click=self._on_add_click,
                               on_erase=self._on_erase)

        self._resize_job = None
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        win = self.window
        win.bind("<Command-o>", lambda e: self._on_open())
        win.bind("<Command-s>", lambda e: self._on_save())
        win.bind("<Command-Shift-s>", lambda e: self._on_save_as())
        win.bind("<Command-Shift-S>", lambda e: self._on_save_as())
        win.bind("<Command-w>", lambda e: self._on_close_doc())
        win.bind("<Command-z>", lambda e: self._shortcut(self._on_undo))
        win.bind("<Command-Shift-z>", lambda e: self._shortcut(self._on_redo))
        win.bind("<Command-Shift-Z>", lambda e: self._shortcut(self._on_redo))
        for number, tool in enumerate(TOOLS, start=1):
            win.bind(f"<Command-Key-{number}>",
                     lambda e, t=tool: self.set_tool(t))
        # Return and Esc apply or cancel the open text box wherever the
        # keyboard focus is in this window. Clicking a button, checkbox or
        # the History list moves the focus off the box, and Return would
        # otherwise do nothing. Text fields with their own Return (Go to
        # page, the size box) handle it and stop it reaching these.
        win.bind("<Return>", self._on_return_key)
        win.bind("<KP_Enter>", self._on_return_key)
        win.bind("<Escape>", self._on_escape_key)
        win.protocol("WM_DELETE_WINDOW", self._on_close_window)

        self._show_tool_panel()
        self._update_ui_state()

    # -- Dialogs, attached to this window --------------------------------------

    def _show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self.window)

    def _show_warning(self, title: str, message: str) -> None:
        messagebox.showwarning(title, message, parent=self.window)

    # -- Keyboard ---------------------------------------------------------------

    def _on_return_key(self, event=None) -> Optional[str]:
        if self.view.entry is None:
            return None
        self._commit_inline_edit()
        return "break"

    def _on_escape_key(self, event=None) -> Optional[str]:
        if self.view.entry is None:
            return None
        self._cancel_inline_edit()
        return "break"

    # -- Menu / toolbar / layout -----------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.window)
        self.window.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open...", command=self._on_open,
                              accelerator="Cmd+O")
        file_menu.add_command(label="Save", command=self._on_save,
                              accelerator="Cmd+S")
        file_menu.add_command(label="Save As...", command=self._on_save_as,
                              accelerator="Cmd+Shift+S")
        file_menu.add_separator()
        file_menu.add_command(label="Close", command=self._on_close_doc,
                              accelerator="Cmd+W")
        self.file_menu = file_menu

        self.edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=self.edit_menu)
        self.edit_menu.add_command(label="Undo", command=self._on_undo,
                                   accelerator="Cmd+Z")
        self.edit_menu.add_command(label="Redo", command=self._on_redo,
                                   accelerator="Cmd+Shift+Z")

        tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=tools_menu)
        for number, (tool, label) in enumerate(
                ((TOOL_EDIT, "Edit Text"), (TOOL_ADD, "Add Text"),
                 (TOOL_ERASE, "Erase")), start=1):
            tools_menu.add_radiobutton(label=label, variable=self.tool_var,
                                       value=tool, accelerator=f"Cmd+{number}",
                                       command=lambda t=tool: self.set_tool(t))

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self.window, padding=(5, 3))
        toolbar.pack(side="top", fill="x")

        ttk.Button(toolbar, text="Open", command=self._on_open,
                   width=6).pack(side="left", padx=(0, 2))
        self.save_btn = ttk.Button(toolbar, text="Save", command=self._on_save,
                                   width=6)
        self.save_btn.pack(side="left", padx=(0, 8))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y",
                                                       padx=5, pady=2)
        self.tool_buttons = {}
        for tool, label in ((TOOL_EDIT, "✎ Edit Text"), (TOOL_ADD, "+ Add Text"),
                            (TOOL_ERASE, "⌫ Erase")):
            button = ttk.Radiobutton(toolbar, text=label, value=tool,
                                     variable=self.tool_var, style="Toolbutton",
                                     command=lambda t=tool: self.set_tool(t))
            button.pack(side="left", padx=1)
            self.tool_buttons[tool] = button

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y",
                                                       padx=8, pady=2)
        self.prev_btn = ttk.Button(toolbar, text="◀", command=self._prev_page,
                                   width=3)
        self.prev_btn.pack(side="left", padx=(0, 3))
        self.page_label = ttk.Label(toolbar, text="No document", width=16,
                                    anchor="center")
        self.page_label.pack(side="left", padx=3)
        self.next_btn = ttk.Button(toolbar, text="▶", command=self._next_page,
                                   width=3)
        self.next_btn.pack(side="left", padx=(3, 0))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y",
                                                       padx=8, pady=2)
        ttk.Label(toolbar, text="Go to:").pack(side="left", padx=(0, 3))
        self.page_entry = ttk.Entry(toolbar, width=5)
        self.page_entry.pack(side="left", padx=(0, 3))
        self.page_entry.bind("<Return>", self._on_page_jump)
        self.page_entry.bind("<KP_Enter>", self._on_page_jump)

    def _build_main_area(self) -> None:
        main = ttk.PanedWindow(self.window, orient="horizontal")
        main.pack(fill="both", expand=True, padx=2, pady=2)

        canvas_frame = ttk.Frame(main)
        main.add(canvas_frame, weight=3)
        self.canvas = tk.Canvas(canvas_frame, bg="#e0e0e0", highlightthickness=0)
        v_scroll = ttk.Scrollbar(canvas_frame, orient="vertical",
                                 command=self.canvas.yview)
        h_scroll = ttk.Scrollbar(canvas_frame, orient="horizontal",
                                 command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=v_scroll.set,
                              xscrollcommand=h_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        sidebar = ttk.Frame(main, width=SIDEBAR_WIDTH)
        main.add(sidebar, weight=0)

        # One panel per tool; only the current tool's is shown.
        self.tool_panels = {}
        self.tool_panel_holder = ttk.Frame(sidebar)
        self.tool_panel_holder.pack(fill="x")
        self.tool_panels[TOOL_EDIT] = self._build_edit_panel(self.tool_panel_holder)
        self.tool_panels[TOOL_ADD] = self._build_add_panel(self.tool_panel_holder)
        self.tool_panels[TOOL_ERASE] = self._build_erase_panel(self.tool_panel_holder)

        history_frame = ttk.LabelFrame(sidebar, text="History", padding=8)
        history_frame.pack(fill="both", expand=True, padx=5, pady=3)
        list_row = ttk.Frame(history_frame)
        list_row.pack(fill="both", expand=True)
        self.history_list = tk.Listbox(list_row, height=8, exportselection=False,
                                       activestyle="none")
        history_scroll = ttk.Scrollbar(list_row, orient="vertical",
                                       command=self.history_list.yview)
        self.history_list.configure(yscrollcommand=history_scroll.set)
        self.history_list.pack(side="left", fill="both", expand=True)
        history_scroll.pack(side="right", fill="y")
        self.history_list.bind("<<ListboxSelect>>", self._on_history_select)

        btn_row = ttk.Frame(history_frame)
        btn_row.pack(fill="x", pady=(5, 0))
        self.undo_btn = ttk.Button(btn_row, text="Undo", command=self._on_undo,
                                   width=8)
        self.undo_btn.pack(side="left", padx=(0, 5))
        self.redo_btn = ttk.Button(btn_row, text="Redo", command=self._on_redo,
                                   width=8)
        self.redo_btn.pack(side="left")

    def _build_edit_panel(self, parent) -> ttk.Frame:
        panel = ttk.Frame(parent)
        help_frame = ttk.LabelFrame(panel, text="Edit Text", padding=8)
        help_frame.pack(fill="x", padx=5, pady=(5, 3))
        ttk.Label(help_frame,
                  text="Click any text to edit it. Press Return to apply "
                       "or Esc to cancel. Clearing the text deletes it.",
                  wraplength=240, justify="left").pack(fill="x")
        info_frame = ttk.LabelFrame(panel, text="Selected Text", padding=8)
        info_frame.pack(fill="x", padx=5, pady=3)
        self.info_label = ttk.Label(info_frame, text="Nothing selected",
                                    foreground="gray", wraplength=240,
                                    justify="left")
        self.info_label.pack(fill="x")
        return panel

    def _build_add_panel(self, parent) -> ttk.Frame:
        panel = ttk.Frame(parent)
        frame = ttk.LabelFrame(panel, text="Add Text", padding=8)
        frame.pack(fill="x", padx=5, pady=(5, 3))
        ttk.Label(frame, text="Click anywhere on the page and type. Press "
                              "Return to add it or Esc to cancel.",
                  wraplength=240, justify="left").pack(fill="x", pady=(0, 6))

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Font").pack(side="left")
        self.family_box = ttk.Combobox(row, textvariable=self.family_var,
                                       values=list(FAMILIES), state="readonly",
                                       width=11, height=20,
                                       postcommand=self._fill_families)
        self.family_box.pack(side="left", padx=(6, 6))
        # After picking a style, typing carries on in the open text box.
        self.family_box.bind("<<ComboboxSelected>>", self._refocus_entry)
        ttk.Label(row, text="Size").pack(side="left")
        self.size_box = ttk.Spinbox(row, textvariable=self.size_var,
                                    from_=MIN_SIZE, to=MAX_SIZE, increment=1,
                                    width=4, command=self._refocus_entry)
        self.size_box.pack(side="left", padx=(6, 0))
        # Return in the size box takes you back to the text box (a second
        # Return then adds the text).
        self.size_box.bind("<Return>", self._size_entered)
        self.size_box.bind("<KP_Enter>", self._size_entered)

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=2)
        ttk.Checkbutton(row, text="Bold", variable=self.bold_var,
                        command=self._refocus_entry).pack(side="left")
        ttk.Checkbutton(row, text="Italic", variable=self.italic_var,
                        command=self._refocus_entry).pack(
            side="left", padx=(8, 8))
        self.color_swatch = tk.Label(row, width=2, bg=_hex(self.text_color),
                                     relief="solid", bd=1)
        self.color_swatch.pack(side="left")
        ttk.Button(row, text="Colour…", command=self._choose_color,
                   width=8).pack(side="left", padx=(4, 0))

        for var in (self.family_var, self.size_var, self.bold_var, self.italic_var):
            var.trace_add("write", lambda *a: self._on_style_change())
        return panel

    def _build_erase_panel(self, parent) -> ttk.Frame:
        panel = ttk.Frame(parent)
        frame = ttk.LabelFrame(panel, text="Erase", padding=8)
        frame.pack(fill="x", padx=5, pady=(5, 3))
        ttk.Label(frame,
                  text="Click a word to erase it. Drag across words to erase "
                       "a phrase or sentence, or drag from empty space to "
                       "erase everything in a rectangle.",
                  wraplength=240, justify="left").pack(fill="x", pady=(0, 6))
        ttk.Label(frame, text="Fill what's left behind with:").pack(anchor="w")
        ttk.Radiobutton(frame, text="Content-aware fill", value=FILL_CONTENT,
                        variable=self.fill_var).pack(anchor="w")
        ttk.Radiobutton(frame, text="Background colour", value=FILL_COLOR,
                        variable=self.fill_var).pack(anchor="w")
        ttk.Label(frame,
                  text="Real text just disappears, showing what's behind it. "
                       "The fill is used for scanned or image content and "
                       "for shapes in an erased rectangle.",
                  foreground="gray", wraplength=240, justify="left",
                  font=("TkDefaultFont", 10)).pack(fill="x", pady=(4, 0))
        return panel

    def _build_status_bar(self) -> None:
        status_frame = ttk.Frame(self.window, relief="sunken", padding=(5, 2))
        status_frame.pack(side="bottom", fill="x")
        self.status_label = ttk.Label(status_frame,
                                      text="Ready — open a PDF to begin")
        self.status_label.pack(side="left")

    # -- Tools ----------------------------------------------------------------

    def set_tool(self, tool: str) -> None:
        if self.view.entry is not None and not self._commit_inline_edit():
            self.tool_var.set(self.view.tool)  # stay put; the box is still open
            return
        self.tool_var.set(tool)
        self.view.set_tool(tool)
        self._show_tool_panel()
        self._set_status({TOOL_EDIT: "Edit Text — click any text to edit it",
                          TOOL_ADD: "Add Text — click where the new text goes",
                          TOOL_ERASE: "Erase — click a word, or drag to select"}[tool])

    def _show_tool_panel(self) -> None:
        for tool, panel in self.tool_panels.items():
            if tool == self.tool_var.get():
                panel.pack(fill="x")
            else:
                panel.pack_forget()

    def current_style(self) -> TextStyle:
        try:
            size = float(self.size_var.get())
        except ValueError:
            size = 12.0
        return TextStyle(family=self.family_var.get(), size=size,
                         bold=self.bold_var.get(), italic=self.italic_var.get(),
                         color=self.text_color)

    def _fill_families(self) -> None:
        """The standard fonts first, then every installed font family."""
        installed = [f for f in fontmatch.default_index().families()
                     if f not in FAMILIES]
        self.family_box.configure(values=list(FAMILIES) + installed)

    def _on_style_change(self) -> None:
        if self.view.adding_at is not None:
            try:
                self.view.restyle_entry(self.current_style())
            except (tk.TclError, KeyError, ValueError):
                pass

    def _choose_color(self) -> None:
        _rgb, hex_color = colorchooser.askcolor(color=_hex(self.text_color),
                                                title="Text Colour",
                                                parent=self.window)
        if not hex_color:
            self._refocus_entry()
            return
        hex_color = hex_color.lstrip("#")
        self.text_color = tuple(int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
        self.color_swatch.configure(bg=_hex(self.text_color))
        self._on_style_change()
        self._refocus_entry()

    def _size_entered(self, event=None) -> str:
        self._refocus_entry()
        return "break"

    # -- UI state -------------------------------------------------------------

    def _update_ui_state(self) -> None:
        has_doc = self.model.doc is not None
        state_doc = "normal" if has_doc else "disabled"
        state_undo = "normal" if has_doc and self.model.can_undo() else "disabled"
        state_redo = "normal" if has_doc and self.model.can_redo() else "disabled"

        for widget in (self.save_btn, self.prev_btn, self.next_btn):
            widget.config(state=state_doc)
        self.undo_btn.config(state=state_undo)
        self.redo_btn.config(state=state_redo)
        self.edit_menu.entryconfig("Undo", state=state_undo)
        self.edit_menu.entryconfig("Redo", state=state_redo)
        self.file_menu.entryconfig("Save", state=state_doc)
        self.file_menu.entryconfig("Save As...", state=state_doc)

        if has_doc:
            p, n = self.model.current_page, self.model.page_count
            self.page_label.config(text=f"Page {p + 1} / {n}")
            name = os.path.basename(self.model.save_path or self.model.file_path)
            dirty = " — edited" if self.model.is_dirty else ""
            self.window.title(f"{APP_NAME} — {name}{dirty}")
        else:
            self.page_label.config(text="No document")
            self.window.title(APP_NAME)

    def _refresh_page(self) -> None:
        if not self.model.doc:
            return
        self.view.display_page(self.model.current_page)
        self._update_ui_state()

    def _after_change(self, label: Optional[str], page_num: int) -> None:
        self.renderer.invalidate(page_num)
        self._refresh_page()
        self._update_history_list()
        self._set_status(label or "No change")

    def _after_failure(self, exc: Exception, title: str) -> None:
        self.renderer.invalidate()
        self._refresh_page()
        self._update_history_list()
        self._show_error(title, str(exc))

    def _set_status(self, text: str) -> None:
        self.status_label.config(text=text)

    # -- Edit tool ------------------------------------------------------------

    def _on_click_run(self, run: TextRun, canvas_xy) -> None:
        if self.view.entry is not None:
            if self.view.editing_run is not None and run.same_as(self.view.editing_run):
                return  # clicking inside the text being edited
            # Committing re-renders the page (possibly at a new scale), so
            # remember the click in PDF points, not screen pixels.
            px, py = self.view.canvas_to_pdf(*canvas_xy)
            if not self._commit_inline_edit():
                return
            run = self.model.run_at(self.model.current_page, px, py)
            if run is None:
                return
            canvas_xy = self.view.pdf_to_canvas(px, py)
        self._begin_edit(run, canvas_xy[0])

    def _on_click_blank(self) -> None:
        if self.view.entry is not None:
            self._commit_inline_edit()

    def _begin_edit(self, run: TextRun, click_x: Optional[float] = None) -> None:
        self.view.begin_inline_edit(run, on_commit=self._commit_inline_edit,
                                    on_cancel=self._cancel_inline_edit,
                                    click_canvas_x=click_x)
        self.info_label.config(
            text=f"{self.model.describe_font(run)}\n{run.size:.1f} pt",
            foreground="")
        self._set_status("Editing — Return to apply, Esc to cancel")

    def _cancel_inline_edit(self) -> None:
        self.view.cancel_inline_edit()
        self._set_status("Cancelled")

    def _commit_inline_edit(self) -> bool:
        """Apply what's in the inline text box (an edit or new text).
        Returns False if the user backed out at a warning or there was an
        error; the box stays open in that case."""
        if self.view.entry is None:
            return True
        if self.view.adding_at is not None:
            return self._commit_add_text()
        run = self.view.editing_run
        new_text = self.view.entry.get()
        plan = self.model.plan_edit(run, new_text)
        if plan.is_noop:
            self.view.end_inline_edit()
            self._set_status("No change")
            return True
        if not plan.can_apply:
            self._show_error("Can't Make This Edit", "\n\n".join(plan.errors))
            self._refocus_entry()
            return False
        if plan.warnings and not messagebox.askokcancel(
                "Edit Text", "\n\n".join(plan.warnings)
                + "\n\nApply the edit anyway?", icon="warning",
                parent=self.window):
            self._refocus_entry()
            return False

        self.view.end_inline_edit()
        try:
            label = self.model.apply_plan(plan)
        except EditError as exc:
            self._after_failure(exc, "Edit Failed")
            return True
        self._after_change(label, run.page_num)
        return True

    def _refocus_entry(self, event=None) -> None:
        if self.view.entry is not None:
            self.view.entry.focus_set()

    # -- Add Text tool --------------------------------------------------------

    def _on_add_click(self, pdf_xy: Tuple[float, float]) -> None:
        if self.view.entry is not None:
            # Committing may re-render the page; pdf_xy stays valid.
            if not self._commit_inline_edit():
                return
        self.view.begin_add_text(self.model.current_page, pdf_xy,
                                 self.current_style(),
                                 on_commit=self._commit_inline_edit,
                                 on_cancel=self._cancel_inline_edit)
        self._set_status("Adding text — Return to add, Esc to cancel")

    def _commit_add_text(self) -> bool:
        page_num, top_left = self.view.adding_at
        text = self.view.entry.get()
        if not text.strip():
            self.view.end_inline_edit()
            self._set_status("Nothing added")
            return True
        plan = self.model.plan_add_text(page_num, top_left, text,
                                        self.current_style())
        if plan.errors:
            # Nothing was changed: let the user fix the text and try again.
            self._show_error("Can't Add This Text", "\n\n".join(plan.errors))
            self._refocus_entry()
            return False
        self.view.end_inline_edit()
        try:
            label = self.model.apply_add(plan)
        except EditError as exc:
            self._after_failure(exc, "Add Text Failed")
            return True
        self._after_change(label, page_num)
        return True

    # -- Erase tool -----------------------------------------------------------

    def _on_erase(self, words: List[Word], area: Optional[pymupdf.Rect]) -> None:
        if self.view.entry is not None and not self._commit_inline_edit():
            return
        page_num = self.model.current_page
        plan = ErasePlan(page_num=page_num, words=list(words), area=area,
                         fill=self.fill_var.get())
        try:
            label = self.model.erase(plan)
        except EditError as exc:
            self._after_failure(exc, "Can't Erase That")
            return
        self._after_change(label, page_num)

    # -- File operations ------------------------------------------------------

    def _confirm_discard(self) -> bool:
        """Ask before throwing away unsaved changes. True = go ahead.

        Text being typed counts as a change: it's applied first, so it's
        included if the user chooses to save (and asked about if not)."""
        if self.view.entry is not None and not self._commit_inline_edit():
            return False
        if not self.model.is_dirty:
            return True
        name = os.path.basename(self.model.save_path or self.model.file_path)
        answer = messagebox.askyesnocancel(
            "Unsaved Changes",
            f"“{name}” has changes that haven't been saved.\n\n"
            "Save them first?", parent=self.window)
        if answer is None:
            return False
        if answer:
            return self._on_save()
        return True

    @property
    def is_empty(self) -> bool:
        """No document open and nothing being typed: a new PDF can open
        here instead of in a new window."""
        return self.model.doc is None and self.view.entry is None

    def shows(self, path: str) -> bool:
        """Whether this window has ``path`` open (or was saved to it)."""
        target = os.path.realpath(path)
        return any(p and os.path.realpath(p) == target
                   for p in (self.model.file_path, self.model.save_path))

    def open_path(self, path: str) -> bool:
        """Open ``path`` in this window, replacing what's shown (callers
        check for unsaved changes first). Returns whether it opened."""
        try:
            self.model.open_document(path)
        except Exception as exc:
            # Whatever was open before stays open (see EditorModel).
            self._show_error("Error", f"Could not open PDF:\n{exc}")
            return False
        self.renderer.invalidate()
        self._update_history_list()
        self._scroll_to_top()
        self._refresh_page()
        self.info_label.config(text="Nothing selected", foreground="gray")
        self._set_status(f"Opened {os.path.basename(path)} — click any text "
                         "to edit it")
        return True

    def _on_open(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Open PDF", parent=self.window,
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if paths:
            # Each PDF gets its own window; this one is used if it's empty.
            self.app.open_paths(*self.window.tk.splitlist(paths), prefer=self)

    def bring_to_front(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def _on_save(self) -> bool:
        if not self.model.doc:
            return False
        if self.model.save_path:
            return self._save_to(self.model.save_path)
        return self._on_save_as()

    def _on_save_as(self) -> bool:
        if not self.model.doc:
            return False
        if self.view.entry is not None and not self._commit_inline_edit():
            return False
        base = os.path.splitext(os.path.basename(self.model.file_path))[0]
        path = filedialog.asksaveasfilename(
            title="Save Edited PDF", defaultextension=".pdf", parent=self.window,
            initialfile=f"{base}_edited.pdf",
            initialdir=os.path.dirname(self.model.file_path),
            filetypes=[("PDF files", "*.pdf")])
        if not path:
            return False
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        if self.view.entry is not None and not self._commit_inline_edit():
            return False
        other = next((w for w in self.app.windows
                      if w is not self and w.shows(path)), None)
        if other is not None:
            self._show_error(
                "File Is Open",
                f"“{os.path.basename(path)}” is open in another window. "
                "Close it there first, or save under another name.")
            return False
        try:
            self.model.save_document(path)
        except Exception as exc:
            self._show_error("Error", f"Could not save PDF:\n{exc}")
            return False
        self._set_status(f"Saved to {os.path.basename(path)}")
        self._update_ui_state()
        return True

    def _on_close_doc(self) -> None:
        """File → Close (Cmd+W): close this document. Its window closes
        too, unless it's the only window, which stays open and empty."""
        if len(self.app.windows) > 1:
            self._on_close_window()
            return
        if not self.model.doc or not self._confirm_discard():
            return
        self.model.close_document()
        self.renderer.invalidate()
        self.canvas.delete("all")
        self._update_history_list()
        self.info_label.config(text="Nothing selected", foreground="gray")
        self._set_status("Ready — open a PDF to begin")
        self._update_ui_state()

    def _on_close_window(self) -> bool:
        """The window's close button: close the window (the app quits with
        its last window). Returns False if the user cancelled."""
        if not self._confirm_discard():
            return False
        self.destroy()
        return True

    def destroy(self) -> None:
        """Close the window without asking (callers have already asked)."""
        if self._resize_job:
            self.window.after_cancel(self._resize_job)
            self._resize_job = None
        self.view.cancel_inline_edit()
        self.model.close_document()
        self.window.destroy()
        self.app.window_closed(self)

    # -- Page navigation ------------------------------------------------------

    def _go_to_page(self, page: int) -> None:
        if self.view.entry is not None and not self._commit_inline_edit():
            return
        self.model.current_page = page
        self._scroll_to_top()
        self._refresh_page()

    def _scroll_to_top(self) -> None:
        """A newly shown page or document starts at its top-left, not at
        wherever the previous one was scrolled to."""
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def _prev_page(self) -> None:
        if self.model.doc and self.model.current_page > 0:
            self._go_to_page(self.model.current_page - 1)

    def _next_page(self) -> None:
        if self.model.doc and self.model.current_page < self.model.page_count - 1:
            self._go_to_page(self.model.current_page + 1)

    def _on_page_jump(self, event=None) -> str:
        # "break": this Return is handled, so it doesn't also apply an open
        # text box (see _on_return_key).
        if not self.model.doc:
            return "break"
        try:
            page = int(self.page_entry.get()) - 1
        except ValueError:
            self._show_warning("Invalid Input", "Enter a page number.")
            return "break"
        if 0 <= page < self.model.page_count:
            self.page_entry.delete(0, "end")
            self._go_to_page(page)
        else:
            self._show_warning("Invalid Page",
                               f"Page must be 1-{self.model.page_count}")
        return "break"

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(-1 * event.delta, "units")

    def _on_canvas_configure(self, event) -> None:
        if self._resize_job:
            self.window.after_cancel(self._resize_job)
        self._resize_job = self.window.after(200, self._do_resize)

    def _do_resize(self) -> None:
        self._resize_job = None
        if self.model.doc and self.view.entry is None:
            self._refresh_page()

    # -- Undo / redo ----------------------------------------------------------

    def _history_move(self, move, verb: str) -> None:
        if not self.model.doc:
            return
        if self.view.entry is not None:
            # Cmd+Z (or a History click) while typing cancels the typing
            # rather than changing the document underneath it.
            self._cancel_inline_edit()
            self._update_history_list()  # undo any History selection
            return
        label = move()
        self.renderer.invalidate()
        self._refresh_page()
        self._update_history_list()
        self._set_status(f"{verb} — now at: {label}")

    def _typing_elsewhere(self) -> bool:
        """Whether the keyboard focus is in a text field other than the
        inline text box (the size box, Go to page...). Cmd+Z there must not
        undo document changes."""
        try:
            focus = self.window.focus_get()
        except (KeyError, tk.TclError):
            return False
        return (isinstance(focus, (tk.Entry, ttk.Entry))
                and focus is not self.view.entry)

    def _shortcut(self, action) -> None:
        """Run a document-level keyboard shortcut, unless the user is typing
        in another text field (the menu items and buttons always work)."""
        if not self._typing_elsewhere():
            action()

    def _on_undo(self) -> None:
        if self.model.doc and self.model.can_undo():
            self._history_move(self.model.undo, "Undo")
        elif self.view.entry is not None:
            self._cancel_inline_edit()

    def _on_redo(self) -> None:
        if self.model.doc and self.model.can_redo():
            self._history_move(self.model.redo, "Redo")

    def _on_history_select(self, event=None) -> None:
        selection = self.history_list.curselection()
        if not selection or selection[0] == self.model.history_index:
            return
        index = selection[0]
        self._history_move(lambda: self.model.jump_to_history(index),
                           "Jumped")

    def _update_history_list(self) -> None:
        self.history_list.delete(0, "end")
        for i, label in enumerate(self.model.history_entries()):
            marker = "▸ " if i == self.model.history_index else "   "
            self.history_list.insert("end", f"{marker}{label}")
        self.history_list.selection_clear(0, "end")
        if 0 <= self.model.history_index < self.history_list.size():
            self.history_list.selection_set(self.model.history_index)
            self.history_list.see(self.model.history_index)
        self._update_ui_state()



# ---------------------------------------------------------------------------
# EditorApp — the application: its windows, opening files, quitting
# ---------------------------------------------------------------------------

class EditorApp:
    """Owns a hidden Tk root and one EditorWindow per open PDF.

    Each PDF opens in its own window, like Preview: opening several at
    once, or one after another, never replaces a document you're working
    on. A PDF that's already open just brings its window to the front.
    Quitting asks about every window's unsaved changes; the app quits when
    its last window closes.
    """

    CASCADE = 28  # px each new window is offset from the previous one

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.windows: List[EditorWindow] = []
        self._quitting = False
        self._asking = False
        # Find the installed fonts now, so the first edit that needs one
        # doesn't wait (a second or so the very first time; cached after).
        fontmatch.default_index().load_in_background()
        # Finder "Open With", double-clicking a PDF and dropping PDFs on the
        # Dock icon deliver files through this Apple Event, not argv.
        self.root.createcommand("::tk::mac::OpenDocument", self.open_paths)
        # Cmd+Q and PDF Editor → Quit. Without this Tk just exits, without
        # asking about unsaved changes.
        self.root.createcommand("::tk::mac::Quit", self.quit)
        # Clicking the Dock icon when no window is open.
        self.root.createcommand("::tk::mac::ReopenApplication", self._on_reopen)

    def new_window(self) -> EditorWindow:
        # Cascade, so a new window never sits exactly on top of another.
        # (Positions are worked out here, not read back from the windows:
        # a window that hasn't been drawn yet reports 0, 0.)
        taken = {w.cascade_step for w in self.windows}
        step = next(i for i in range(len(taken) + 1) if i not in taken)
        offset = self.CASCADE * (step % 5)
        # Leave room on screen to cascade into: macOS pushes a window that
        # doesn't fit back onto the screen, on top of the previous one.
        max_w, max_h = self.root.wm_maxsize()
        room = self.CASCADE * 4
        width = max(MIN_WINDOW_WIDTH, min(DEFAULT_WIDTH, max_w - 60 - room))
        height = max(MIN_WINDOW_HEIGHT, min(DEFAULT_HEIGHT, max_h - 40 - room))
        win = EditorWindow(self, f"{width}x{height}+{60 + offset}+{40 + offset}")
        win.cascade_step = step
        self.windows.append(win)
        return win

    def open_paths(self, *paths, prefer: Optional[EditorWindow] = None) -> None:
        """Open each PDF in its own window: the one it's already open in,
        else ``prefer`` or another empty window, else a new one."""
        for path in paths:
            if not str(path).lower().endswith(".pdf"):
                continue
            existing = next((w for w in self.windows if w.shows(path)), None)
            if existing is not None:
                existing.bring_to_front()
                continue
            empty = [w for w in self.windows if w.is_empty]
            if prefer is not None and prefer in empty:
                win, created = prefer, False
            elif empty:
                win, created = empty[0], False
            else:
                win, created = self.new_window(), True
            if not win.open_path(path) and created:
                win.destroy()   # the error was shown; don't leave a blank window
            elif win.model.doc is not None:
                win.bring_to_front()

    def window_closed(self, win: EditorWindow) -> None:
        if win in self.windows:
            self.windows.remove(win)
        if not self.windows and not self._quitting:
            self._destroy_root()

    def quit(self, *args) -> None:
        """Ask about each window's unsaved changes, then quit. Cancelling
        any of them cancels quitting."""
        # Dialogs are sheets, so Quit can be chosen again while one of
        # ours is still asking: ignore that.
        if self._asking:
            return
        self._asking = True
        try:
            for win in list(self.windows):
                if win not in self.windows:
                    continue                      # closed meanwhile
                if win.model.is_dirty or win.view.entry is not None:
                    win.bring_to_front()
                if not win._confirm_discard():
                    return
        finally:
            self._asking = False
        self._quitting = True
        for win in list(self.windows):
            win.destroy()
        self._destroy_root()

    def _on_reopen(self, *args) -> None:
        if not self.windows:
            self.new_window()

    def _destroy_root(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass   # already gone

    def run(self) -> None:
        self.root.mainloop()


def main(argv: List[str]) -> None:
    # Tight text boxes: better click targets and hover outlines.
    pymupdf.TOOLS.set_small_glyph_heights(True)
    app = EditorApp()
    app.new_window()
    paths = [p for p in argv[1:] if os.path.isfile(p)]
    if paths:
        app.root.after(100, lambda: app.open_paths(*paths))
    app.run()


if __name__ == "__main__":
    main(sys.argv)
