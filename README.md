# PDF Editor

A small macOS app for editing the text in a PDF the way you'd edit a text
document. Click any line and type, then press Return. The new text keeps the
original font, size, colour and position. The old text is actually removed
from the file, not just covered up.

Sister app to [PDF Redactor](https://github.com/DevTamer/pdf-redactor-for-mac).
Both are built on [pdfcore](https://github.com/DevTamer/pdfcore).

> **Status: early (v0.1).** Single-line edits work well, including on
> real-world PDFs with subset fonts. See [Limitations](#limitations) for what
> it doesn't do yet.

## Using it

1. **File → Open…** (`Cmd+O`), or drop a PDF onto the app icon.
2. Hover over text: editable text gets a dashed blue outline.
3. Click it, type, then press **Return** to apply or **Esc** to cancel.
   Clicking elsewhere also applies. Clearing the box and pressing Return
   deletes that text.
4. **Undo/Redo** (`Cmd+Z` / `Cmd+Shift+Z`), or click any entry in the
   **History** panel to jump back to it.
5. **Save** (`Cmd+S`) or **Save As…** (`Cmd+Shift+S`). The first save
   suggests `<name>_edited.pdf`. Your original file is only replaced if you
   choose to save over it.

### Fonts, and when you'll see a warning

PDFs usually embed only a *subset* of each font: just the characters the
document already uses. If you type a character the PDF's copy of the font
doesn't have, the app draws that character with the closest standard font
(Helvetica, Times or Courier, matching bold/italic) and asks you first:

> These characters can't be drawn with the PDF's copy of
> "TimesNewRomanPS-BoldMT" (PDFs usually include only the characters a
> document already uses), so they'll use Times Bold instead: 9 E R

Everything else stays in the original font. You'll also be warned if the
original font isn't embedded at all, or if longer text would run into the
text that follows it on the same line. Characters that no available font
can draw, such as most non-Latin scripts in v0.1, are refused rather than
written as blank boxes.

## How an edit works

For each edit, the app:

1. **Removes the old text** with a text-only redaction. Images and vector
   graphics are left alone, and nothing is painted in its place. It covers
   only a thin band through the middle of the line, so text above and below
   is safe even when the lines are set solid.
2. **Writes the new text** at the same baseline, size and colour. It uses
   the PDF's own embedded font for every character that font can actually
   draw, and a matching standard font for the rest.
3. **Verifies the result.** Every other character on the page must be
   unchanged, and the new text must read back in order where it was written.
   If anything doesn't check out, the page is restored from the last undo
   snapshot and you get an error instead of a damaged document.

Deciding whether a font "can draw" a character takes three checks, because
subsetting tools differ:

- the font's character map must include the character,
- drawing it must actually produce ink (some subsetters keep the map but
  blank out unused glyphs), and
- writing it and extracting it again must give back the same character
  (fonts often map space and no-break space, or hyphen and soft hyphen, to
  one glyph, which would otherwise copy and search as the wrong character).

The details are in the docstring at the top of [`textedit.py`](textedit.py).

## Limitations

- **One line at a time.** Text doesn't reflow onto other lines. Longer text
  can run into what follows it on the same line (you're warned first).
- **Horizontal text on unrotated pages only.** Rotated pages and vertical
  text are refused.
- **Spacing is the font's own.** Justified lines that relied on per-word
  positioning, character spacing or horizontal scaling come back with
  natural spacing.
- **Drawing order.** Edited text is drawn on top of the page's other
  content. If a shape used to be drawn *over* that text (a highlight, a
  strike-through line), the text now sits above it.
- **Substitute fonts are standard fonts.** A missing character uses
  Helvetica, Times or Courier, so in a distinctive typeface it will look
  different.
- **No new text.** You can't yet add text where there was none, or change a
  run's font, size or colour.
- The app is unsigned and not notarized. The first time, open it with
  right-click → **Open** → **Open**.

## Developer setup

Requires [Homebrew](https://brew.sh). macOS's built-in Python links against an
old Tcl/Tk that crashes on modern macOS, so `setup.sh` uses Homebrew's
`python-tk@3.11`.

```bash
git clone https://github.com/DevTamer/pdf-editor-for-mac.git
cd pdf-editor-for-mac
./setup.sh
source venv/bin/activate && python editor.py [some.pdf]
```

### Running the tests

```bash
source venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The suite has four parts:

- `test_textedit.py` covers the editing engine. It runs headless, on
  generated PDFs plus one real-world PDF made by macOS's own text system
  (`tests/fixtures/coretext_sample.pdf`, regenerated with
  `tests/fixtures/make_coretext_sample.swift`).
- `test_model.py` covers undo, rollback after a failed edit, unsaved-changes
  tracking and saving.
- `test_gui_smoke.py` drives the real window: hover, click, type, Return and
  Esc, dialogs, undo, save. It skips itself if Tk can't open a window, and it
  fails rather than hangs if any unexpected dialog would appear.
- `test_startup.py` covers startup ordering and import hygiene.

The manual checklist is in
[`docs/MANUAL_TESTING.md`](docs/MANUAL_TESTING.md).

### Working on pdfcore at the same time

Page rendering, coordinate mapping, the undo/redo snapshots and the canvas
page view come from [pdfcore](https://github.com/DevTamer/pdfcore), pinned to
an exact tag in `requirements.txt`. To change pdfcore and see the effect here
right away:

```bash
git clone https://github.com/DevTamer/pdfcore.git ../pdfcore
./setup.sh --with-local-pdfcore     # or: pip install -e ../pdfcore
```

A plain `pip install -r requirements.txt` afterwards swaps the pinned release
back in.

### Building the app / DMG

```bash
./packaging/build_app.sh
```

The script builds in a fresh venv from the exact pinned requirements, runs
the test suite there, then builds `packaging/dist/PDF Editor.app` and
`packaging/PDF Editor.dmg`. The icon is generated by `packaging/make_icon.py`.

## Architecture

- [`textedit.py`](textedit.py) is the editing engine, with no Tk. It finds
  text runs, picks fonts, plans an edit (warnings and errors included) and
  applies it with verification.
- [`editor.py`](editor.py) is the app:
  - `EditorModel` builds on pdfcore's `DocumentSession`. It adds edits as
    undo steps, rollback on failure, unsaved-changes tracking and atomic
    saving.
  - `EditCanvas` builds on pdfcore's `PageView`. It handles the hover
    outline, click hit-testing and the inline text box.
  - `EditorApp` is the window, menus and sidebar.

Saving always writes from a serialized copy of the document. In PyMuPDF
1.24, a garbage-collecting save of a document that has newly written text
can blank the live document's pages; `test_live_document_survives_saving`
guards against this.
