"""
PDF Editor — click any text in a PDF and edit it in place.

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

import tempfile  # noqa: E402
import tkinter as tk  # noqa: E402  (must follow configure_bundled_tcl_tk)
from tkinter import ttk, filedialog, messagebox  # noqa: E402
from typing import List, Optional  # noqa: E402

import pymupdf  # noqa: E402

from pdfcore import DocumentSession, PDFRenderer  # noqa: E402
from pdfcore.pageview import PageView  # noqa: E402

from textedit import (  # noqa: E402
    EditError, EditPlan, TextRun, apply_edit, page_runs, plan_edit,
    resolve_original_font, run_at,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "PDF Editor"
DEFAULT_WINDOW_SIZE = "1200x800"
MIN_WINDOW_WIDTH = 900
MIN_WINDOW_HEIGHT = 600
SIDEBAR_WIDTH = 280
HISTORY_LIMIT = 30  # Max snapshots kept in the undo/redo history

HOVER_OUTLINE = "#2f7ae5"
HOVER_DASH = (3, 2)
EDIT_BACKGROUND = "#fffbe6"
EDIT_BORDER = "#2f7ae5"
MIN_EDITOR_WIDTH = 160  # px
EDITOR_EXTRA_WIDTH = 60  # px of room to type beyond the original text


def _short(text: str, limit: int = 24) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------
# EditorModel — document state + text edits
# ---------------------------------------------------------------------------

class EditorModel(DocumentSession):
    """The open document, its edit history, and saving.

    Every successful edit is one undo step. A failed edit is rolled back
    by reloading the last snapshot, so the live document is never left
    half-edited.
    """

    def __init__(self):
        super().__init__(history_limit=HISTORY_LIMIT)
        self._saved_entry = None
        self.save_path: Optional[str] = None

    # -- Document lifecycle ---------------------------------------------------

    def open_document(self, path: str) -> None:
        super().open_document(path)
        self._saved_entry = self.history.current
        self.save_path = None

    def close_document(self) -> None:
        super().close_document()
        self._saved_entry = None
        self.save_path = None

    @property
    def is_dirty(self) -> bool:
        """Whether there are edits that haven't been saved. Undoing back to
        the last saved state counts as clean again."""
        return self.doc is not None and self.history.current is not self._saved_entry

    # -- Text -----------------------------------------------------------------

    def runs(self, page_num: int) -> List[TextRun]:
        return page_runs(self.get_page(page_num), page_num)

    def run_at(self, page_num: int, x: float, y: float) -> Optional[TextRun]:
        return run_at(self.runs(page_num), x, y)

    def describe_font(self, run: TextRun) -> str:
        page = self.get_page(run.page_num)
        choice, reason = resolve_original_font(self.doc, page, run)
        if choice is None:
            return f"{run.font} — {reason}"
        if choice.check_ink:
            return f"{run.font} (embedded)"
        return f"{run.font} (standard PDF font)"

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
        try:
            apply_edit(self.get_page(plan.run.page_num), plan)
        except Exception as exc:
            self._restore_current_snapshot()
            if isinstance(exc, EditError):
                raise
            raise EditError(f"The edit failed: {exc}") from exc
        if plan.new_text.strip():
            label = f"Edit “{_short(plan.run.text)}” → “{_short(plan.new_text)}”"
        else:
            label = f"Delete “{_short(plan.run.text)}”"
        self.record_snapshot(label)
        return label

    def _restore_current_snapshot(self) -> None:
        page = self.current_page
        self.doc.close()
        self.doc = self.history.current.open()
        self.current_page = page

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
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=directory)
        os.close(fd)
        try:
            copy.save(tmp_path, garbage=3, deflate=True)
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
# EditCanvas — hover, click-to-edit, inline editor
# ---------------------------------------------------------------------------

class EditCanvas(PageView):
    """Shows the page, highlights the text under the pointer, and hosts
    the inline editor."""

    def __init__(self, canvas: tk.Canvas, model: EditorModel,
                 renderer: PDFRenderer, on_click_run, on_click_blank):
        super().__init__(canvas, renderer)
        self.model = model
        self.on_click_run = on_click_run
        self.on_click_blank = on_click_blank
        self._runs: List[TextRun] = []
        self._hover: Optional[TextRun] = None
        self.editing_run: Optional[TextRun] = None
        self.entry: Optional[tk.Entry] = None
        self._entry_window: Optional[int] = None
        self._on_commit = self._on_cancel = lambda: None

        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", lambda e: self._set_hover(None))
        canvas.bind("<ButtonPress-1>", self._on_click)

    # -- Page display ---------------------------------------------------------

    def display_page(self, page_num: int, fit_width: Optional[int] = None) -> None:
        if not self.model.doc:
            return
        self.cancel_inline_edit()
        self.show_page(self.model.get_page(page_num), page_num, fit_width)
        self._runs = self.model.runs(page_num)
        self._hover = None

    def run_at_canvas(self, cx: float, cy: float) -> Optional[TextRun]:
        px, py = self.canvas_to_pdf(cx, cy)
        return run_at(self._runs, px, py)

    # -- Hover ----------------------------------------------------------------

    def _on_motion(self, event) -> None:
        if not self.model.doc:
            return
        self._set_hover(self.run_at_canvas(*self.event_coords(event)))

    def _set_hover(self, run: Optional[TextRun]) -> None:
        if run is self._hover:
            return
        self._hover = run
        self.canvas.delete("hover")
        self.canvas.configure(cursor="xterm" if run else "")
        if run is not None and run is not self.editing_run:
            x0, y0 = self.pdf_to_canvas(run.bbox[0], run.bbox[1])
            x1, y1 = self.pdf_to_canvas(run.bbox[2], run.bbox[3])
            self.canvas.create_rectangle(x0 - 2, y0 - 1, x1 + 2, y1 + 1,
                                         outline=HOVER_OUTLINE, dash=HOVER_DASH,
                                         tags=("hover",))

    # -- Clicking -------------------------------------------------------------

    def _on_click(self, event) -> None:
        if not self.model.doc:
            return
        run = self.run_at_canvas(*self.event_coords(event))
        if run is not None:
            self.on_click_run(run, self.event_coords(event))
        else:
            self.on_click_blank()

    # -- Inline editor --------------------------------------------------------

    def begin_inline_edit(self, run: TextRun, on_commit, on_cancel,
                          click_canvas_x: Optional[float] = None) -> tk.Entry:
        """Put a text box over ``run``, prefilled with its text."""
        self.cancel_inline_edit()
        self.canvas.delete("hover")
        self.editing_run = run
        scale = self.mapper.total_scale
        x0, y0 = self.pdf_to_canvas(run.bbox[0], run.bbox[1])
        x1, y1 = self.pdf_to_canvas(run.bbox[2], run.bbox[3])

        family = ("Courier" if run.monospaced
                  else "Times" if run.serif else "Helvetica")
        style = " ".join(s for s, on in (("bold", run.bold),
                                         ("italic", run.italic)) if on)
        font = (family, -max(8, round(run.size * scale)), style or "normal")
        red, green, blue = (round(c * 255) for c in run.color)

        entry = tk.Entry(self.canvas, font=font, relief="solid", bd=1,
                         bg=EDIT_BACKGROUND, fg=f"#{red:02x}{green:02x}{blue:02x}",
                         highlightthickness=1, highlightcolor=EDIT_BORDER,
                         insertbackground="black")
        entry.insert(0, run.text)
        if click_canvas_x is not None and x1 > x0:
            fraction = (click_canvas_x - x0) / (x1 - x0)
            entry.icursor(round(max(0.0, min(1.0, fraction)) * len(run.text)))
        else:
            entry.icursor("end")
        width = max(x1 - x0 + EDITOR_EXTRA_WIDTH, MIN_EDITOR_WIDTH)
        height = max(y1 - y0 + 8, 18)
        self._entry_window = self.canvas.create_window(
            x0 - 3, y0 - 4, anchor="nw", window=entry, width=width,
            height=height, tags=("editor",))
        self._on_commit, self._on_cancel = on_commit, on_cancel
        entry.bind("<Return>", self._entry_commit)
        entry.bind("<KP_Enter>", self._entry_commit)
        entry.bind("<Escape>", self._entry_cancel)
        entry.focus_set()
        self.entry = entry
        return entry

    def _entry_commit(self, event=None) -> str:
        self._on_commit()
        return "break"

    def _entry_cancel(self, event=None) -> str:
        self._on_cancel()
        return "break"

    def end_inline_edit(self) -> Optional[str]:
        """Remove the text box. Returns what was typed (None if there was
        no edit in progress)."""
        if self.entry is None:
            return None
        text = self.entry.get()
        self.canvas.delete("editor")
        self.entry.destroy()
        self.entry = None
        self._entry_window = None
        self.editing_run = None
        return text

    def cancel_inline_edit(self) -> None:
        self.end_inline_edit()


# ---------------------------------------------------------------------------
# EditorApp — main window
# ---------------------------------------------------------------------------

class EditorApp:
    """Main PDF Editor window."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry(DEFAULT_WINDOW_SIZE)
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        self.model = EditorModel()
        self.renderer = PDFRenderer()

        self._build_menu()
        self._build_toolbar()
        self._build_main_area()
        self._build_status_bar()

        self.view = EditCanvas(self.canvas, self.model, self.renderer,
                               on_click_run=self._on_click_run,
                               on_click_blank=self._on_click_blank)

        self._resize_job = None
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.root.bind("<Command-o>", lambda e: self._on_open())
        self.root.bind("<Command-s>", lambda e: self._on_save())
        self.root.bind("<Command-Shift-s>", lambda e: self._on_save_as())
        self.root.bind("<Command-Shift-S>", lambda e: self._on_save_as())
        self.root.bind("<Command-w>", lambda e: self._on_close_doc())
        self.root.bind("<Command-z>", lambda e: self._on_undo())
        self.root.bind("<Command-Shift-z>", lambda e: self._on_redo())
        self.root.bind("<Command-Shift-Z>", lambda e: self._on_redo())
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)
        # Finder "Open With" / double-clicking a PDF delivers files through
        # this Apple Event hook rather than argv.
        self.root.createcommand("::tk::mac::OpenDocument", self._on_open_documents)

        self._update_ui_state()

    def _on_open_documents(self, *paths) -> None:
        pdfs = [p for p in paths if p.lower().endswith(".pdf")]
        if pdfs and self._confirm_discard():
            self.open_path(pdfs[0])

    # -- Menu / toolbar / layout -----------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open...", command=self._on_open,
                              accelerator="Cmd+O")
        file_menu.add_command(label="Save", command=self._on_save,
                              accelerator="Cmd+S")
        file_menu.add_command(label="Save As...", command=self._on_save_as,
                              accelerator="Cmd+Shift+S")
        file_menu.add_separator()
        file_menu.add_command(label="Close Document", command=self._on_close_doc,
                              accelerator="Cmd+W")
        self.file_menu = file_menu

        self.edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=self.edit_menu)
        self.edit_menu.add_command(label="Undo", command=self._on_undo,
                                   accelerator="Cmd+Z")
        self.edit_menu.add_command(label="Redo", command=self._on_redo,
                                   accelerator="Cmd+Shift+Z")

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(5, 3))
        toolbar.pack(side="top", fill="x")

        ttk.Button(toolbar, text="Open", command=self._on_open,
                   width=6).pack(side="left", padx=(0, 2))
        self.save_btn = ttk.Button(toolbar, text="Save", command=self._on_save,
                                   width=6)
        self.save_btn.pack(side="left", padx=(0, 8))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y",
                                                       padx=5, pady=2)
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

    def _build_main_area(self) -> None:
        main = ttk.PanedWindow(self.root, orient="horizontal")
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

        help_frame = ttk.LabelFrame(sidebar, text="Editing", padding=8)
        help_frame.pack(fill="x", padx=5, pady=(5, 3))
        ttk.Label(help_frame,
                  text="Click any text to edit it. Press Return to apply "
                       "or Esc to cancel. Clearing the text deletes it.",
                  wraplength=240, justify="left").pack(fill="x")

        info_frame = ttk.LabelFrame(sidebar, text="Selected Text", padding=8)
        info_frame.pack(fill="x", padx=5, pady=3)
        self.info_label = ttk.Label(info_frame, text="Nothing selected",
                                    foreground="gray", wraplength=240,
                                    justify="left")
        self.info_label.pack(fill="x")

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

    def _build_status_bar(self) -> None:
        status_frame = ttk.Frame(self.root, relief="sunken", padding=(5, 2))
        status_frame.pack(side="bottom", fill="x")
        self.status_label = ttk.Label(status_frame,
                                      text="Ready — open a PDF to begin")
        self.status_label.pack(side="left")

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
            self.root.title(f"{APP_NAME} — {name}{dirty}")
        else:
            self.page_label.config(text="No document")
            self.root.title(APP_NAME)

    def _refresh_page(self) -> None:
        if not self.model.doc:
            return
        self.view.display_page(self.model.current_page)
        self._update_ui_state()

    def _set_status(self, text: str) -> None:
        self.status_label.config(text=text)

    # -- Editing --------------------------------------------------------------

    def _on_click_run(self, run: TextRun, canvas_xy) -> None:
        if self.view.entry is not None:
            if run.same_as(self.view.editing_run):
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
        self._set_status("Edit cancelled")

    def _commit_inline_edit(self) -> bool:
        """Apply what's in the inline editor. Returns False if the user
        backed out at a warning (the editor stays open in that case)."""
        run = self.view.editing_run
        if run is None or self.view.entry is None:
            return True
        new_text = self.view.entry.get()
        plan = self.model.plan_edit(run, new_text)
        if plan.is_noop:
            self.view.end_inline_edit()
            self._set_status("No change")
            return True
        if not plan.can_apply:
            messagebox.showerror("Can't Make This Edit", "\n\n".join(plan.errors))
            if self.view.entry is not None:
                self.view.entry.focus_set()
            return False
        if plan.warnings and not messagebox.askokcancel(
                "Edit Text", "\n\n".join(plan.warnings)
                + "\n\nApply the edit anyway?", icon="warning"):
            if self.view.entry is not None:
                self.view.entry.focus_set()
            return False

        self.view.end_inline_edit()
        try:
            label = self.model.apply_plan(plan)
        except EditError as exc:
            self.renderer.invalidate()
            self._refresh_page()
            self._update_history_list()
            messagebox.showerror("Edit Failed", str(exc))
            return True
        self.renderer.invalidate(run.page_num)
        self._refresh_page()
        self._update_history_list()
        self._set_status(label or "No change")
        return True

    # -- File operations ------------------------------------------------------

    def _confirm_discard(self) -> bool:
        """Ask before throwing away unsaved edits. True = go ahead."""
        if self.view.entry is not None:
            self._cancel_inline_edit()
        if not self.model.is_dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved Changes",
            "This document has edits that haven't been saved.\n\n"
            "Save them first?")
        if answer is None:
            return False
        if answer:
            return self._on_save()
        return True

    def open_path(self, path: str) -> None:
        try:
            self.model.open_document(path)
        except Exception as exc:
            messagebox.showerror("Error", f"Could not open PDF:\n{exc}")
            return
        self.renderer.invalidate()
        self._update_history_list()
        self._refresh_page()
        self.info_label.config(text="Nothing selected", foreground="gray")
        self._set_status(f"Opened {os.path.basename(path)} — click any text "
                         "to edit it")

    def _on_open(self) -> None:
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="Open PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if path:
            self.open_path(path)

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
            title="Save Edited PDF", defaultextension=".pdf",
            initialfile=f"{base}_edited.pdf",
            initialdir=os.path.dirname(self.model.file_path),
            filetypes=[("PDF files", "*.pdf")])
        if not path:
            return False
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        if self.view.entry is not None and not self._commit_inline_edit():
            return False
        try:
            self.model.save_document(path)
        except Exception as exc:
            messagebox.showerror("Error", f"Could not save PDF:\n{exc}")
            return False
        self._set_status(f"Saved to {os.path.basename(path)}")
        self._update_ui_state()
        return True

    def _on_close_doc(self) -> None:
        if not self.model.doc or not self._confirm_discard():
            return
        self.model.close_document()
        self.renderer.invalidate()
        self.canvas.delete("all")
        self._update_history_list()
        self.info_label.config(text="Nothing selected", foreground="gray")
        self._set_status("Ready — open a PDF to begin")
        self._update_ui_state()

    def _on_quit(self) -> None:
        if self._confirm_discard():
            self.root.destroy()

    # -- Page navigation ------------------------------------------------------

    def _go_to_page(self, page: int) -> None:
        if self.view.entry is not None and not self._commit_inline_edit():
            return
        self.model.current_page = page
        self._refresh_page()

    def _prev_page(self) -> None:
        if self.model.doc and self.model.current_page > 0:
            self._go_to_page(self.model.current_page - 1)

    def _next_page(self) -> None:
        if self.model.doc and self.model.current_page < self.model.page_count - 1:
            self._go_to_page(self.model.current_page + 1)

    def _on_page_jump(self, event=None) -> None:
        if not self.model.doc:
            return
        try:
            page = int(self.page_entry.get()) - 1
        except ValueError:
            messagebox.showwarning("Invalid Input", "Enter a page number.")
            return
        if 0 <= page < self.model.page_count:
            self.page_entry.delete(0, "end")
            self._go_to_page(page)
        else:
            messagebox.showwarning("Invalid Page",
                                   f"Page must be 1-{self.model.page_count}")

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(-1 * event.delta, "units")

    def _on_canvas_configure(self, event) -> None:
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(200, self._do_resize)

    def _do_resize(self) -> None:
        self._resize_job = None
        if self.model.doc and self.view.entry is None:
            self._refresh_page()

    # -- Undo / redo ----------------------------------------------------------

    def _history_move(self, move, verb: str) -> None:
        if not self.model.doc:
            return
        if self.view.entry is not None:
            # Cmd+Z while typing cancels the edit rather than undoing the
            # document underneath it.
            self._cancel_inline_edit()
            return
        label = move()
        self.renderer.invalidate()
        self._refresh_page()
        self._update_history_list()
        self._set_status(f"{verb} — now at: {label}")

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

    def run(self) -> None:
        self.root.mainloop()


def main(argv: List[str]) -> None:
    # Tight text boxes: better click targets and hover outlines.
    pymupdf.TOOLS.set_small_glyph_heights(True)
    app = EditorApp()
    if len(argv) > 1 and os.path.isfile(argv[1]):
        app.root.after(100, lambda: app.open_path(argv[1]))
    app.run()


if __name__ == "__main__":
    main(sys.argv)
