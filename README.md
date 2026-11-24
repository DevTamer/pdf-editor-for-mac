# PDF Editor

A small macOS app for editing a PDF the way you'd edit a text document:

- **Edit text.** Click any line and type. The new text keeps the original
  font, size, colour and position.
- **Add text** anywhere on the page.
- **Erase** words, whole sentences, or any rectangle. Real text just
  disappears. Scanned or image content is filled to match its surroundings,
  paper grain included.

Replaced and erased text is actually removed from the file, not just covered
up.

Sister app to [PDF Redactor](https://github.com/DevTamer/pdf-redactor-for-mac).
Both are built on [pdfcore](https://github.com/DevTamer/pdfcore).

> **Status: early (v0.2).** Single-line edits, added text and erasing work
> well, including on real-world PDFs with subset fonts and on scans. See
> [Limitations](#limitations) for what it doesn't do yet.

## Using it

Open PDFs with **File → Open…** (`Cmd+O`, several at once if you like), or
drop them onto the app icon. Each PDF gets its own window, like Preview (or
its own tab, if your Mac is set to prefer tabs), so opening another never
replaces one you're working on. Opening a PDF that's already open just
brings its window to the front.

Then pick a tool from the toolbar or the **Tools** menu:

| Tool | Shortcut | What you do |
|---|---|---|
| ✎ **Edit Text** | `Cmd+1` | Hover: editable text gets a dashed blue outline. Click it, type, then press **Return** to apply or **Esc** to cancel. Clicking elsewhere also applies. Clearing the box deletes the text. |
| + **Add Text** | `Cmd+2` | Click where the new text should start and type. Choose the font (any installed font, or the standard Helvetica, Times and Courier), size, bold, italic and colour in the sidebar. Return adds it. |
| ⌫ **Erase** | `Cmd+3` | Click a word to erase it. Drag from one word to another to erase everything between them, like selecting text. Drag from empty space to erase everything in a rectangle. |

Return and Esc apply or cancel the text box wherever you last clicked in the
window, so clicking Bold, the colour or the History list first is fine.

Every change is one step in the **History** panel. Each window has its own. Use **Undo/Redo**
(`Cmd+Z` / `Cmd+Shift+Z`), or click any entry to jump back to it.

**Save** (`Cmd+S`) or **Save As…** (`Cmd+Shift+S`). The first save suggests
`<name>_edited.pdf`. Your original file is only replaced if you choose to
save over it. Closing a window, or quitting (`Cmd+Q`), asks about unsaved
changes in each window first.

### What erasing leaves behind

- **Real text** simply disappears. Whatever was behind it shows through:
  white paper, a coloured box, a photo. Nothing is painted over it.
- **Scanned pages** (and photos or screenshots) are images, so the letters
  are pixels. Often there's also an invisible OCR text layer on top, and
  that is removed too. The erased spot is filled in the image itself, from
  its surroundings. Pick the fill in the Erase panel:
  - **Content-aware fill** (default) continues gradients, paper tone and
    grain.
  - **Background colour** paints the surrounding colour.

  The image keeps its colour space and stays compact. A JPEG scan stays a
  JPEG of about the same size, and a black-and-white scan stays 1-bit.
  JPEG 2000 scans become JPEG. Other image types are stored deflated,
  which can be larger than the original encoding.
- **Shapes in an erased rectangle** (a logo or stamp drawn as vector
  graphics) are covered with a fill. They're never deleted, so larger
  shapes the rectangle merely sits inside, like a page border, are
  unaffected.

### Fonts, and when you'll see a warning

PDFs usually embed only a *subset* of each font: just the characters the
document already uses. When you type a character the PDF's copy of the
font doesn't have, the app looks for it, in this order:

1. **The PDF's own font.**
2. **The same font elsewhere in the PDF.** Some PDFs embed a separate
   subset of a font on each page.
3. **The same font, installed on your Mac.** "ArialMT" finds Arial,
   "TimesNewRomanPS-BoldMT" finds Times New Roman Bold, and Calibri,
   Cambria or Aptos are found in Microsoft Word if it's installed. This
   also covers PDFs that name a font without embedding it.
4. **The closest-looking installed font.** Fonts of the same kind (serif or
   not, monospaced or not, bold, italic) are compared by x-height, cap
   height and character widths, and the best two dozen glyph by glyph: the
   characters the PDF's font has are drawn in both fonts and compared
   pixel by pixel. Arial finds Helvetica, Times New Roman finds Times,
   Verdana finds MS Reference Sans Serif, Georgia finds PT Serif or
   Charter. If the PDF doesn't embed the font at all, the character widths
   it records are compared instead.
5. **Helvetica, Times or Courier**, only if nothing else can draw it.

Steps 1 to 3 are the same typeface, so there's no warning. Steps 4 and 5
are a lookalike, and you're asked first:

> These characters can't be drawn with the PDF's copy of "Georgia" (PDFs
> usually include only the characters a document already uses), and it
> isn't installed on this Mac, so they'll use PT Serif, the closest match:
> 9 E R

An installed font is never embedded whole, only the characters you typed,
so the file grows by a few KB. Fonts whose licence forbids embedding are
never used. Installed fonts are scanned once, in the background at launch
(about a second), and remembered afterwards.

The text box you type in also shows the matched font, when macOS can
display it (fonts inside Word's app bundle can't be, so a lookalike is
shown while typing).

You'll also be warned if longer text would run into the text that follows
it on the same line. Characters that no available font can draw, such as
most non-Latin scripts for now, are refused rather than written as blank
boxes.

## How an edit works

For each edit, the app:

1. **Removes the old text** with a text-only redaction. Images and vector
   graphics are left alone, and nothing is painted in its place. It covers
   only a thin band through the middle of the line, so text above and below
   is safe even when the lines are set solid.
2. **Writes the new text** at the same baseline, size and colour. It uses
   the PDF's own embedded font for every character that font can actually
   draw, and a matching standard font for the rest.
3. **Verifies the result.** The page's characters are counted before and
   after. Apart from what was replaced, the counts must match exactly (layout
   whitespace aside). The new text must also read back in order where it was
   written, and every annotation and form field must still be there. If
   anything doesn't check out, the page is restored from the last undo
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
  can run into what follows it on the same line (you're warned first). Added
  text is a single line.
- **Horizontal text on unrotated pages only.** Rotated pages and vertical
  text are refused for all three tools. An erase rectangle that contains
  vertical text is refused too.
- **Comments and form fields are off limits.** Text in (or under) a comment
  box, stamp or form field can't be edited or erased yet. A redaction
  there would delete the whole annotation, so the change is refused.
- **Spacing is the font's own.** Justified lines that relied on per-word
  positioning, character spacing or horizontal scaling come back with
  natural spacing.
- **Drawing order.** Edited and added text is drawn on top of the page's
  other content. If a shape used to be drawn *over* that text (a highlight,
  a strike-through line), the text now sits above it.
- **Variable fonts aren't used yet.** SF Pro, New York and other variable
  fonts are skipped when matching, so text in them gets a lookalike.
- **Erasing shapes covers them.** Vector graphics in an erased rectangle are
  covered with a fill rather than removed from the file. Some images are
  also covered with a fill rather than edited, and their original pixels
  stay in the file underneath:
  - images also shown elsewhere (on another page, or in an annotation),
    so their other copies don't change,
  - images with transparency,
  - images in a colour space the editor doesn't handle (for example an
    indexed palette over an ICC profile).
- **Content-aware fill isn't magic.** It's made for backgrounds: paper,
  gradients, noise, flat colour. Erasing across detailed photo content
  leaves a soft smudge.
- **Erasing is not redaction.** Erased text is removed. Erased scan pixels
  are replaced when the scan is edited in place, but not where a fill
  covers them (see above). For removing sensitive content with nothing left
  behind (metadata, hidden layers and so on), use
  [PDF Redactor](https://github.com/DevTamer/pdf-redactor-for-mac).
- **Undo depth depends on file size.** Every undo step keeps a full copy of
  the document in memory, so you get up to 30 steps, fewer for big files
  (at least 5).
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

The suite has six parts:

- `test_erase.py` covers erasing: selections, fills (including paper grain),
  text removal, in-place scan edits (JPEG and 1-bit stay compact), and
  covering leftover shapes.
- `test_fontmatch.py` covers font matching: names, the font index and its
  cache, licences, lookalikes and editing with installed fonts. It uses
  real macOS fonts, made "installed" one at a time so results don't
  depend on the machine.
- `test_textedit.py` covers the editing and add-text engine. It runs headless, on
  generated PDFs plus one real-world PDF made by macOS's own text system
  (`tests/fixtures/coretext_sample.pdf`, regenerated with
  `tests/fixtures/make_coretext_sample.swift`).
- `test_model.py` covers undo, rollback after a failed edit, unsaved-changes
  tracking and saving.
- `test_gui_smoke.py` drives the real window: all three tools, hover, click,
  drag, type, Return and Esc, dialogs, undo, save. It skips itself if Tk can't open a window, and it
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

- [`textedit.py`](textedit.py) is the editing and add-text engine, with no
  Tk. It finds text runs, picks fonts, plans an edit (warnings and errors
  included) and applies it with verification.
- [`fontmatch.py`](fontmatch.py) finds installed fonts: the same font by
  name, or the closest-looking one, and embeds just the characters needed.
  No Tk.
- [`erase.py`](erase.py) is the erase engine, with no Tk. It handles words
  and selections, removing text character by character, the fills, in-place
  image edits and covering patches.
- [`editor.py`](editor.py) is the app:
  - `EditorModel` builds on pdfcore's `DocumentSession`. Every change is an
    undo step with rollback on failure. It also tracks unsaved changes and
    saves atomically.
  - `EditCanvas` builds on pdfcore's `PageView` and handles the mouse for
    each tool and the inline text box.
  - `EditorApp` is the window, the tools, menus and sidebar.

PyMuPDF quirks the code works around, each with a regression test:

- **Saving can blank the open document.** In PyMuPDF 1.24, a
  garbage-collecting save of a document with newly written text can blank
  its pages, so saving always writes from a serialized copy.
- **Replacing an image leaves a copy behind.** `Page.replace_image` leaves a
  second copy of the image in the file, so erased images are rewritten in
  place instead.
- **The image cache goes stale.** MuPDF caches decoded images, so it's
  flushed after an image is rewritten. `page.refresh()` would also work, but
  it fails while anything else holds the page.
