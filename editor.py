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

import contextlib  # noqa: E402
import datetime  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
import tkinter as tk  # noqa: E402  (must follow configure_bundled_tcl_tk)
from tkinter import colorchooser, filedialog, messagebox, ttk  # noqa: E402
from tkinter import font as tkfont  # noqa: E402
from typing import Callable, Dict, List, Optional, Tuple  # noqa: E402

import pymupdf  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

from pdfcore import DocumentSession, PDFRenderer  # noqa: E402
from pdfcore.pageview import PageView  # noqa: E402

from erase import (  # noqa: E402
    FILL_COLOR, FILL_CONTENT, ErasePlan, Word, apply_erase, page_words,
    word_at, words_between,
)
import fontmatch  # noqa: E402
from findreplace import (  # noqa: E402
    Match, ReplaceAllPlan, find_on_page, match_rects, might_contain,
    plan_replace, plan_replace_all, skipped_summary,
)
from textedit import (  # noqa: E402
    FAMILIES, MAX_SIZE, MIN_SIZE, AddPlan, EditError, EditPlan, TextRun,
    TextStyle,
    apply_add_text, apply_edit, apply_edits, page_runs, plan_add_text, plan_edit,
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

# What saving does with the document's metadata (File > Metadata on Save).
METADATA_UPDATE_DATE = "update-date"   # keep it, with today's modification date
METADATA_KEEP = "keep"                 # keep it exactly as it was
METADATA_REMOVE = "remove"             # remove the document info
METADATA_MODES = (METADATA_UPDATE_DATE, METADATA_KEEP, METADATA_REMOVE)
SETTINGS_PATH = os.path.expanduser(
    "~/Library/Application Support/PDF Editor/settings.json")

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
SELECTION_ALPHA = 0.35      # erase selection over words
MARQUEE_ALPHA = 0.15        # erase rectangle
FIND_ALPHA = 0.35           # find matches
FIND_OTHER = "#f5b400"      # outlines of matches
FIND_CURRENT = "#f76b15"    # the current match
FIND_DELAY_MS = 250         # search once typing pauses this long
MIN_MARQUEE = 4  # px: smaller drags count as clicks


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def pdf_date(when: datetime.datetime) -> str:
    """A PDF date string, "D:YYYYMMDDHHmmSS+HH'mm'", with the offset
    worked out from ``when`` itself. (pymupdf.get_pdf_now() always uses
    the daylight-saving offset, so it's an hour out in winter.)"""
    offset = when.utcoffset() or datetime.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"D:{when:%Y%m%d%H%M%S}{sign}{hours:02d}'{mins:02d}'"


def xmp_date(when: datetime.datetime) -> str:
    return when.isoformat(timespec="seconds")


XMP_BASIC_NS = "http://ns.adobe.com/xap/1.0/"
# Sections describing *other* resources (placed images, fonts, earlier
# versions), whose dates aren't the document's.
_XMP_SKIP = re.compile(
    r"<(xmpMM:(?:Pantry|Ingredients|History|DerivedFrom|Manifest))\b.*?"
    r"(?:</\1>|/>)", re.S)


def _xmp_date_pattern(xmp: str) -> Optional["re.Pattern"]:
    """The document's ModifyDate/MetadataDate, as attributes or elements,
    under whichever prefixes this XMP binds to the XMP basic namespace
    (usually "xmp", "xap" in Acrobat 4-5 files), or as unprefixed elements
    declaring it as their default namespace. None if there are none."""
    prefixes = set(re.findall(r'xmlns:([A-Za-z_][\w.-]*)\s*=\s*["\']'
                              + re.escape(XMP_BASIC_NS) + r'["\']', xmp))
    alternatives = []
    if prefixes:
        p = "(?:" + "|".join(re.escape(x) for x in sorted(prefixes)) + ")"
        name = rf"{p}:(?:ModifyDate|MetadataDate)"
        alternatives.append(rf"(?<![\w:.-])(?P<a>{name}\s*=\s*(?P<q>[\"']))[^\"']*(?P=q)")
        alternatives.append(rf"(?P<e><(?P<n>{name})(?:\s[^>]*)?>)[^<]*(?P<ee></(?P=n)>)")
    alternatives.append(
        r"(?P<d><(?P<dn>ModifyDate|MetadataDate)\s+xmlns\s*=\s*[\"']"
        + re.escape(XMP_BASIC_NS) + r"[\"']\s*>)[^<]*(?P<de></(?P=dn)>)")
    return re.compile("|".join(alternatives))


def update_xmp_dates(xmp: str, when: datetime.datetime) -> str:
    """``xmp`` with the document's ModifyDate and MetadataDate set to
    ``when``; everything else, byte for byte, as it was. Dates of other
    resources (in xmpMM:Pantry, Ingredients...) and look-alike names in
    other namespaces ("myxmp:ModifyDate") are left alone."""
    skip = [m.span() for m in _XMP_SKIP.finditer(xmp)]
    stamp = xmp_date(when)

    def replace(m):
        if any(a <= m.start() < b for a, b in skip):
            return m.group(0)
        g = m.groupdict()          # (the prefixed groups exist only if bound)
        if g.get("a"):
            return f"{g['a']}{stamp}{g['q']}"
        if g.get("e"):
            return f"{g['e']}{stamp}{g['ee']}"
        return f"{g['d']}{stamp}{g['de']}"
    return _xmp_date_pattern(xmp).sub(replace, xmp)


def update_modification_date(doc: pymupdf.Document,
                             when: Optional[datetime.datetime] = None) -> None:
    """Set the document's modification date to now, in the Info
    dictionary and the XMP metadata, changing nothing else (Producer,
    Creator, custom fields all stay). A PDF without either gets none, and
    a damaged /Info (not a dictionary) is left alone rather than stopping
    the save."""
    when = when or _now()
    stamp = f"({pdf_date(when)})"
    kind, value = doc.xref_get_key(-1, "Info")
    if kind == "xref":
        info = int(value.split()[0])
        if doc.xref_object(info, compressed=True).lstrip().startswith("<<"):
            doc.xref_set_key(info, "ModDate", stamp)
    elif kind == "dict":
        doc.xref_set_key(-1, "Info/ModDate", stamp)
    xmp = doc.get_xml_metadata()
    if xmp:
        doc.set_xml_metadata(update_xmp_dates(xmp, when))


def remove_document_info(doc: pymupdf.Document) -> None:
    """Remove the document info (title, author, dates, creator, producer
    and any custom fields), all XMP metadata, and applications' private
    data (/PieceInfo, which can hold a copy of the original content, on
    the catalog, pages and embedded graphics alike)."""
    doc.scrub(attached_files=False, clean_pages=False, embedded_files=False,
              hidden_text=False, javascript=False, metadata=True,
              redactions=False, remove_links=False, reset_fields=False,
              reset_responses=False, thumbnails=False, xml_metadata=True)
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "PieceInfo")[0] != "null":
                doc.xref_set_key(xref, "PieceInfo", "null")
        except Exception:
            continue          # a free or damaged object


def keep_encryption(edited: pymupdf.Document,
                    original_bytes: bytes) -> pymupdf.Document:
    """The original encrypted file with ``edited``'s content in it, ready
    to save with encryption=PDF_ENCRYPT_KEEP, so the saved file keeps the
    original's encryption, permissions *and* owner password (re-encrypting
    with a new owner password would lock its owner out of lifting the
    restrictions).

    The editor works on decrypted snapshots, which keep the original's
    object numbers, so each object (dictionary and raw, still-filtered
    stream data) is copied back into the original under its own number.
    Everything else, the /Encrypt dictionary and /ID above all, stays the
    original's.
    """
    target = pymupdf.open(stream=original_bytes, filetype="pdf")
    target_pdf = pymupdf._as_pdf_document(target)
    kind, value = target.xref_get_key(-1, "Encrypt")
    encrypt_xref = int(value.split()[0]) if kind == "xref" else None
    while target.xref_length() < edited.xref_length():
        target.get_new_xref()
    for xref in range(1, edited.xref_length()):
        if xref == encrypt_xref:
            continue
        source = edited.xref_object(xref, compressed=True)
        target.update_object(xref, source if source.strip() else "null")
        if edited.xref_is_stream(xref):
            raw = edited.xref_stream_raw(xref)
            # Raw = still in its filters (Flate, DCT...), which the
            # dictionary just copied names: store it exactly as is.
            pymupdf.mupdf.pdf_update_stream(
                target_pdf, pymupdf.mupdf.pdf_new_indirect(target_pdf, xref, 0),
                pymupdf.mupdf.fz_new_buffer_from_copied_data(raw), 1)
    for key in ("Root", "Info"):
        kind, value = edited.xref_get_key(-1, key)
        target.xref_set_key(-1, key, value if kind != "null" else "null")
    return target


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(changes: dict) -> None:
    """Remember ``changes``, on top of whatever is saved (another copy of
    the app may have saved other settings meanwhile). Best effort: a
    setting that can't be saved just isn't remembered."""
    try:
        directory = os.path.dirname(SETTINGS_PATH)
        os.makedirs(directory, exist_ok=True)
        settings = load_settings()
        settings.update(changes)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        pass


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
        self._search_cache: dict = {}
        self._encrypted_original: Optional[bytes] = None

    # -- Document lifecycle ---------------------------------------------------

    def open_document(self, path: str) -> None:
        # If this fails (unreadable or password-protected file), pdfcore
        # leaves the current document, its history and our state untouched.
        super().open_document(path)
        self._search_cache = {}
        # An encrypted PDF that opened without a password (an owner
        # password protecting its permissions): keep the original file, so
        # saving can keep its encryption and restrictions (keep_encryption).
        if self.doc.metadata.get("encryption"):
            with open(path, "rb") as fh:
                self._encrypted_original = fh.read()
        else:
            self._encrypted_original = None
        # Every undo step holds a full copy of the document: cap the count so
        # a big scan doesn't eat gigabytes of memory.
        size = max(1, len(self.history.current.doc_bytes))
        self.history.limit = max(MIN_HISTORY, min(HISTORY_LIMIT, HISTORY_BUDGET // size))
        self._saved_entry = self.history.current
        self.save_path = None

    @property
    def keeps_restrictions(self) -> bool:
        """Whether saving keeps the original's encryption and permissions."""
        return self._encrypted_original is not None

    def close_document(self) -> None:
        super().close_document()
        self._search_cache = {}
        self._encrypted_original = None
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

    def apply_plan(self, plan: EditPlan, label: Optional[str] = None) -> Optional[str]:
        """Apply a plan from plan_edit(). Returns the new history label, or
        None if the plan changes nothing. Raises EditError (with the
        document restored to how it was) if the edit can't be made."""
        if plan.is_noop:
            return None
        if not plan.can_apply:
            raise EditError(plan.errors[0])
        if label is not None:
            pass
        elif plan.new_text.strip():
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

    # -- Find and replace -----------------------------------------------------

    def _page_cache(self, page_num: int) -> dict:
        """Per-page text and runs for searching, kept while the page's
        content is unchanged. (An edit changes one page; an undo or redo
        loads a whole new document, but most pages come back the same.)"""
        fingerprint = self._page_fingerprint(page_num)
        cache = self._search_cache.get(page_num)
        if cache is None or cache["fingerprint"] != fingerprint:
            cache = self._search_cache[page_num] = {"fingerprint": fingerprint}
        return cache

    def _page_fingerprint(self, page_num: int) -> str:
        """Changes whenever the page's text can have: a hash of its raw
        (still compressed) content streams, those of the form XObjects it
        draws (nested ones too: a redaction can move the page's text into
        one, leaving the page's own stream identical for different edits),
        and its fonts."""
        page = self.get_page(page_num)
        digest = hashlib.sha1()
        for xref in page.get_contents():
            digest.update(self.doc.xref_stream_raw(xref) or b"")
        for xref, *_ in page.get_xobjects():
            digest.update(b"|%d|" % xref)
            digest.update(self.doc.xref_stream_raw(xref) or b"")
        digest.update(repr(page.get_fonts()).encode())
        return digest.hexdigest()

    def find(self, query: str, match_case: bool = False,
             whole_word: bool = False) -> List[Match]:
        """Every match, in reading order, without highlight boxes (see
        match_rects)."""
        matches = []
        for page_num in range(self.page_count):
            cache = self._page_cache(page_num)
            if "text" not in cache:
                cache["text"] = self.get_page(page_num).get_text()
            if not might_contain(cache["text"], query):
                continue
            if "runs" not in cache:
                cache["runs"] = self.runs(page_num)
            matches.extend(find_on_page(self.get_page(page_num), page_num, query,
                                        match_case, whole_word,
                                        runs=cache["runs"], rects=False))
        return matches

    def match_rects(self, matches: List[Match]) -> List[Match]:
        """The matches, all on one page, with their highlight boxes."""
        if not matches:
            return []
        return match_rects(self.get_page(matches[0].page_num), matches)

    def plan_replace(self, match: Match, replacement: str) -> EditPlan:
        return plan_replace(self.doc, match, replacement)

    def apply_replace(self, plan: EditPlan, match: Match, replacement: str) -> str:
        """Apply plan_replace()'s plan as one undo step."""
        return self.apply_plan(
            plan, f"Replace “{_short(match.text)}” → “{_short(replacement)}”")

    def plan_replace_all(self, matches: List[Match], replacement: str,
                         query: str = "") -> ReplaceAllPlan:
        return plan_replace_all(self.doc, matches, replacement, query)

    def apply_replace_all(self, plan: ReplaceAllPlan) -> str:
        """Make every replacement as one undo step. If any line can't be
        changed and verified, none are: the document is restored and
        EditError says which line."""
        if not plan.plans:
            raise EditError("There's nothing to replace.")

        by_page: Dict[int, List[EditPlan]] = {}
        for edit in plan.plans:
            by_page.setdefault(edit.run.page_num, []).append(edit)

        def apply_all():
            # One pass per page: removal and verification for all its lines
            # at once (one at a time is ~30 ms a line).
            for page_num, edits in sorted(by_page.items()):
                try:
                    apply_edits(self.get_page(page_num), edits)
                except EditError as exc:
                    raise EditError(
                        f"Nothing was replaced: page {page_num + 1} couldn't "
                        f"be changed. {exc}") from exc

        count = plan.count
        return self._change(
            apply_all, f"Replace all “{_short(plan.query)}” → "
                       f"“{_short(plan.replacement)}” ({count})")

    def erase(self, plan: ErasePlan) -> str:
        """Erase words or an area (see erase.py). Raises EditError if
        there's nothing to erase or it can't be done safely."""
        return self._change(lambda: apply_erase(self.doc, plan.page_num, plan),
                            plan.label)

    # -- Saving ---------------------------------------------------------------

    def save_document(self, path: str,
                      metadata: str = METADATA_UPDATE_DATE) -> None:
        """Write the current state to ``path``.

        ``metadata`` says what happens to the document's metadata: keep
        it with today's modification date (the default), keep it exactly,
        or remove it (see METADATA_MODES). The open document isn't
        changed either way.

        Saves from a copy, never from the live document: in PyMuPDF
        1.24, a garbage-collecting save of a document with newly written
        text can leave the live document's pages blank. Writes to a
        temporary file next to ``path`` and renames it into place, so
        saving over the file that's open works, and a failed save never
        leaves a half-written file behind.
        """
        if metadata not in METADATA_MODES:
            raise ValueError(metadata)
        copy = pymupdf.open(stream=self.doc.tobytes(), filetype="pdf")
        if metadata == METADATA_UPDATE_DATE:
            update_modification_date(copy)
        elif metadata == METADATA_REMOVE:
            remove_document_info(copy)
        encryption = pymupdf.PDF_ENCRYPT_NONE
        if self._encrypted_original is not None:
            edited, copy = copy, keep_encryption(copy, self._encrypted_original)
            edited.close()
            encryption = pymupdf.PDF_ENCRYPT_KEEP
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
            copy.save(tmp_path, garbage=3, deflate=True, encryption=encryption)
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
        self._tints: dict = {}            # translucent images, by size and colour
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

    def tint(self, box, color: str, alpha: float, tag: str) -> None:
        """A see-through coloured rectangle over the page.

        Tk on macOS ignores stipple patterns, so a "half-filled" rectangle
        is drawn solid and hides the text under it. A translucent image
        doesn't."""
        x0, y0, x1, y1 = (round(v) for v in box)
        size = (max(1, x1 - x0), max(1, y1 - y0))
        key = (size, color, alpha)
        image = self._tints.get(key)
        if image is None:
            red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            image = ImageTk.PhotoImage(
                Image.new("RGBA", size, (red, green, blue, round(alpha * 255))),
                master=self.canvas)
            if len(self._tints) > 500:
                # Sizes change as the user drags: let go of images no longer
                # drawn (Tk blanks an image the moment it's let go).
                in_use = {self.canvas.itemcget(i, "image")
                          for i in self.canvas.find_all()
                          if self.canvas.type(i) == "image"}
                self._tints = {k: v for k, v in self._tints.items()
                               if str(v) in in_use}
            self._tints[key] = image
        self.canvas.create_image(x0, y0, anchor="nw", image=image, tags=(tag,))

    def _draw_selection(self, xy) -> None:
        self.canvas.delete("erase_sel")
        if self._anchor_word is not None:
            for word in self.selection:
                self.tint(self._canvas_box(word.bbox, 1), ERASE_COLOR,
                          SELECTION_ALPHA, "erase_sel")
        else:
            box = self._marquee(xy)
            if box is not None:
                self.tint(box, ERASE_COLOR, MARQUEE_ALPHA, "erase_sel")
                self.canvas.create_rectangle(
                    *box, outline=ERASE_COLOR, dash=(4, 3), width=1.5,
                    tags=("erase_sel",))

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
        self._build_find_bar()
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
        win.bind("<Command-f>", self.show_find_bar)
        win.bind("<Command-g>", self._find_next)
        win.bind("<Command-Shift-g>", self._find_prev)
        win.bind("<Command-Shift-G>", self._find_prev)
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
        metadata_menu = tk.Menu(file_menu, tearoff=0)
        for mode, label in (
                (METADATA_UPDATE_DATE, "Keep, with Today's Modification Date"),
                (METADATA_KEEP, "Keep Exactly as It Was"),
                (METADATA_REMOVE, "Remove Document Info")):
            metadata_menu.add_radiobutton(label=label, value=mode,
                                          variable=self.app.metadata_var)
        file_menu.add_cascade(label="Metadata on Save", menu=metadata_menu)
        self.metadata_menu = metadata_menu
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
        self.edit_menu.add_separator()
        self.edit_menu.add_command(label="Find & Replace…",
                                   command=self.show_find_bar, accelerator="Cmd+F")
        self.edit_menu.add_command(label="Find Next", command=self._find_next,
                                   accelerator="Cmd+G")
        self.edit_menu.add_command(label="Find Previous", command=self._find_prev,
                                   accelerator="Cmd+Shift+G")

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
        self.toolbar = toolbar

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

    # -- Find and replace bar ----------------------------------------------------

    def _build_find_bar(self) -> None:
        """Hidden until Edit > Find & Replace (Cmd+F)."""
        bar = ttk.Frame(self.window, padding=(5, 2))
        self.find_bar = bar
        self.find_var = tk.StringVar(self.window)
        self.replace_var = tk.StringVar(self.window)
        self.match_case_var = tk.BooleanVar(self.window, value=False)
        self.whole_word_var = tk.BooleanVar(self.window, value=False)
        self.matches: List[Match] = []
        self.match_index = -1
        self._find_job = None
        # After a Replace: carry on searching after the inserted text, so
        # a replacement that contains the search text isn't found again.
        self._resume_at: Optional[tuple] = None

        ttk.Label(bar, text="Find").pack(side="left")
        self.find_entry = ttk.Entry(bar, textvariable=self.find_var, width=22)
        self.find_entry.pack(side="left", padx=(4, 8))
        ttk.Label(bar, text="Replace with").pack(side="left")
        self.replace_entry = ttk.Entry(bar, textvariable=self.replace_var, width=22)
        self.replace_entry.pack(side="left", padx=(4, 8))
        ttk.Checkbutton(bar, text="Match case", variable=self.match_case_var,
                        command=lambda: self._refind(jump=True)).pack(side="left")
        ttk.Checkbutton(bar, text="Whole words", variable=self.whole_word_var,
                        command=lambda: self._refind(jump=True)).pack(side="left",
                                                                      padx=(4, 8))
        self.find_prev_btn = ttk.Button(bar, text="◀", width=2, command=self._find_prev)
        self.find_prev_btn.pack(side="left")
        self.find_next_btn = ttk.Button(bar, text="▶", width=2, command=self._find_next)
        self.find_next_btn.pack(side="left", padx=(0, 8))
        self.replace_btn = ttk.Button(bar, text="Replace", command=self._replace_current)
        self.replace_btn.pack(side="left")
        self.replace_all_btn = ttk.Button(bar, text="Replace All",
                                          command=self._replace_all)
        self.replace_all_btn.pack(side="left", padx=(4, 8))
        self.find_count = ttk.Label(bar, text="", width=16)
        self.find_count.pack(side="left")
        ttk.Button(bar, text="Done", command=self.hide_find_bar,
                   width=5).pack(side="right")

        # These fields handle their own Return and Esc ("break"), so the
        # window's Return/Esc (for the inline text box) never sees them.
        for widget in (self.find_entry, self.replace_entry):
            widget.bind("<Escape>", self._find_escape)
        for key in ("<Return>", "<KP_Enter>"):
            self.find_entry.bind(key, self._find_return)
            self.replace_entry.bind(key, self._replace_return)
        self.find_entry.bind("<Shift-Return>", self._find_shift_return)
        self.find_var.trace_add("write", lambda *a: self._schedule_find())
        self._update_find_controls()

    @property
    def find_bar_shown(self) -> bool:
        return bool(self.find_bar.winfo_manager())

    def show_find_bar(self, event=None) -> None:
        if not self.find_bar_shown:
            self.find_bar.pack(side="top", fill="x", after=self.toolbar)
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")
        self._refind()

    def hide_find_bar(self) -> None:
        if self._find_job:
            self.window.after_cancel(self._find_job)
            self._find_job = None
        self.find_bar.pack_forget()
        self.matches, self.match_index = [], -1
        self.canvas.delete("find")
        self.canvas.focus_set()

    def _find_return(self, event=None) -> str:
        self._find_next()
        return "break"

    def _find_shift_return(self, event=None) -> str:
        self._find_prev()
        return "break"

    def _replace_return(self, event=None) -> str:
        self._replace_current()
        return "break"

    def _find_escape(self, event=None) -> str:
        self.hide_find_bar()
        return "break"

    def _schedule_find(self) -> None:
        """Search as you type, once typing pauses."""
        if self._find_job:
            self.window.after_cancel(self._find_job)
        self._find_job = self.window.after(FIND_DELAY_MS, self._run_scheduled_find)

    def _run_scheduled_find(self) -> None:
        self._find_job = None
        # Don't leave the page while a text box is open: going to another
        # page would apply the half-typed edit. (Return or Next, asked for
        # explicitly, still go.)
        self._refind(jump=self.view.entry is None)

    def _refind(self, jump: bool = False) -> None:
        """Search again (the text, options or document changed). Keeps
        the current match where it was, or picks the first one from the
        current page on; ``jump`` also shows it."""
        query = self.find_var.get()
        if not (self.find_bar_shown and self.model.doc and query):
            self.matches, self.match_index = [], -1
            self._update_find_controls()
            self._draw_find_highlights()
            return
        previous = (self.matches[self.match_index]
                    if 0 <= self.match_index < len(self.matches) else None)
        with self._busy("Searching…" if self.model.page_count > 20 else None):
            self.matches = self.model.find(query, self.match_case_var.get(),
                                           self.whole_word_var.get())
        if self._resume_at is not None:
            here, self._resume_at = self._resume_at, None
        elif previous is not None and not jump:
            here = previous.order
        else:
            here = (self.model.current_page,)
        self.match_index = next(
            (i for i, m in enumerate(self.matches) if m.order >= here),
            0 if self.matches else -1)
        self._update_find_controls()
        if jump and self.matches:
            self._show_match()
        else:
            self._draw_find_highlights()

    def _update_find_controls(self) -> None:
        n = len(self.matches)
        if not self.find_var.get():
            text = ""
        elif n == 0:
            text = "No matches"
        else:
            text = f"{self.match_index + 1} of {n}"
        self.find_count.config(text=text)
        state = "normal" if n else "disabled"
        for button in (self.find_prev_btn, self.find_next_btn,
                       self.replace_btn, self.replace_all_btn):
            button.config(state=state)

    def _find_next(self, event=None) -> None:
        self._step_match(+1)

    def _find_prev(self, event=None) -> None:
        self._step_match(-1)

    def _flush_find(self) -> bool:
        """Run a search still waiting for typing to pause, now, so a quick
        Return isn't undone by it a moment later. True if one ran."""
        if not self._find_job:
            return False
        self.window.after_cancel(self._find_job)
        self._find_job = None
        self._refind(jump=True)
        return True

    def _step_match(self, step: int) -> None:
        if not self.find_bar_shown:
            self.show_find_bar()
            return
        if self._flush_find():
            return                  # it just showed the first match
        if not self.matches:
            self._refind(jump=True)
            return
        self.match_index = (self.match_index + step) % len(self.matches)
        self._update_find_controls()
        self._show_match()

    def _show_match(self) -> None:
        """Go to the current match's page and scroll it into view."""
        match = self.matches[self.match_index]
        if match.page_num != self.model.current_page:
            self._go_to_page(match.page_num)
            if self.model.current_page != match.page_num:
                return                     # the open text box couldn't be applied
        self._draw_find_highlights()
        x0, y0, x1, y1 = self.view._canvas_box(self._rect_of(self.match_index))
        height = self.view.display_size[1] or 1
        top, bottom = (f * height for f in self.canvas.yview())
        if y0 < top or y1 > bottom:
            self.canvas.yview_moveto(max(0.0, (y0 - 60) / height))

    def _draw_find_highlights(self) -> None:
        """Highlight the matches on the page shown (translucent, so the
        text stays readable); the current one is also outlined."""
        self.canvas.delete("find")
        if not self.find_bar_shown or not self.model.doc:
            return
        for i, match in enumerate(self.matches):
            if match.page_num != self.model.current_page:
                continue
            box = self.view._canvas_box(self._rect_of(i), 1)
            self.view.tint(box, FIND_OTHER, FIND_ALPHA, "find")
            if i == self.match_index:
                self.canvas.create_rectangle(*box, outline=FIND_CURRENT, width=2,
                                             tags=("find",))

    @contextlib.contextmanager
    def _busy(self, status: Optional[str]):
        """Show a wait cursor (and ``status``) during something slow."""
        if status is None:
            yield
            return
        previous = self.status_label["text"]
        self._set_status(status)
        self.window.configure(cursor="watch")
        self.window.update_idletasks()
        try:
            yield
        finally:
            self.window.configure(cursor="")
            if self.status_label["text"] == status:
                self._set_status(previous)

    def _rect_of(self, index: int):
        """A match's highlight box. Boxes are worked out a page at a time,
        when that page is shown (they're slow to get for a whole PDF)."""
        match = self.matches[index]
        if match.rect is None:
            page = [i for i, m in enumerate(self.matches)
                    if m.page_num == match.page_num and m.rect is None]
            for i, boxed in zip(page, self.model.match_rects(
                    [self.matches[i] for i in page])):
                self.matches[i] = boxed
        return self.matches[index].rect

    def _replace_current(self) -> None:
        if self.view.entry is not None and not self._commit_inline_edit():
            return
        if self._flush_find():
            return                  # show the first match before replacing it
        if not (0 <= self.match_index < len(self.matches)):
            return
        match = self.matches[self.match_index]
        if match.page_num != self.model.current_page:
            self._show_match()             # show it first; Replace again replaces
            return
        replacement = self.replace_var.get()
        plan = self.model.plan_replace(match, replacement)
        if plan.is_noop:
            self._find_next()
            return
        if not plan.can_apply:
            self._show_error("Can't Replace This", "\n\n".join(plan.errors))
            return
        if plan.warnings and not messagebox.askokcancel(
                "Replace", "\n\n".join(plan.warnings) + "\n\nReplace anyway?",
                icon="warning", parent=self.window):
            return
        try:
            label = self.model.apply_replace(plan, match, replacement)
        except EditError as exc:
            self._after_failure(exc, "Replace Failed")
            return
        page, y, x, start = match.order
        self._resume_at = (page, y, x, start + len(replacement))
        self._after_change(label, match.page_num)   # finds again, from there on
        if self.matches:
            self._show_match()

    def _replace_all(self) -> None:
        if self.view.entry is not None and not self._commit_inline_edit():
            return
        if self._find_job:
            self.window.after_cancel(self._find_job)
            self._find_job = None
        self._refind()
        query, replacement = self.find_var.get(), self.replace_var.get()
        if not self.matches:
            return
        with self._busy("Checking each replacement…"):
            plan = self.model.plan_replace_all(self.matches, replacement, query)
        skipped = skipped_summary(plan)
        if not plan.plans:
            self._show_error("Can't Replace",
                             "None of the matches can be replaced:\n\n"
                             + "\n".join(skipped))
            return
        pages = len(plan.pages)
        message = (f"Replace {plan.count} occurrence{'s' if plan.count != 1 else ''}"
                   f" of “{query}” with “{replacement}” on {pages} "
                   f"page{'s' if pages != 1 else ''}?")
        if plan.warnings:
            message += "\n\n" + "\n\n".join(plan.warnings)
        if skipped:
            message += ("\n\nThese will be left as they are:\n"
                        + "\n".join(skipped))
        message += "\n\nThis is one step: Undo puts everything back."
        if not messagebox.askokcancel("Replace All", message, parent=self.window,
                                      icon="warning" if plan.warnings else "info"):
            return
        try:
            with self._busy(f"Replacing {plan.count}…"):
                label = self.model.apply_replace_all(plan)
        except EditError as exc:
            self._after_failure(exc, "Replace All Failed")
            return
        self.renderer.invalidate()                   # several pages changed
        self._after_change(label, self.model.current_page)

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
        self._draw_find_highlights()
        self._update_ui_state()

    def _after_change(self, label: Optional[str], page_num: int) -> None:
        self.renderer.invalidate(page_num)
        self._refresh_page()
        self._update_history_list()
        self._set_status(label or "No change")
        self._refind()

    def _after_failure(self, exc: Exception, title: str) -> None:
        self.renderer.invalidate()
        self._refresh_page()
        self._update_history_list()
        self._refind()
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
        if self.model.keeps_restrictions:
            self._set_status(f"Opened {os.path.basename(path)} — it has "
                             "security restrictions, and saving keeps them")
        else:
            self._set_status(f"Opened {os.path.basename(path)} — click any "
                             "text to edit it")
        self._refind()
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
        mode = self.app.metadata_var.get()
        try:
            self.model.save_document(path, metadata=mode)
        except Exception as exc:
            self._show_error("Error", f"Could not save PDF:\n{exc}")
            return False
        note = {METADATA_UPDATE_DATE: "", METADATA_KEEP: " (metadata kept as it was)",
                METADATA_REMOVE: " (document info removed)"}[mode]
        self._set_status(f"Saved to {os.path.basename(path)}{note}")
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
        self._refind()
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
        if self._find_job:
            self.window.after_cancel(self._find_job)
            self._find_job = None
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
        self._refind()
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
        # File > Metadata on Save: one choice for every window, remembered
        # between launches.
        self.settings = load_settings()
        mode = self.settings.get("metadata")
        self.metadata_var = tk.StringVar(
            self.root, value=mode if mode in METADATA_MODES else METADATA_UPDATE_DATE)
        self.metadata_var.trace_add("write", self._on_metadata_mode)
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

    def _on_metadata_mode(self, *args) -> None:
        self.settings["metadata"] = self.metadata_var.get()
        save_settings({"metadata": self.settings["metadata"]})

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
