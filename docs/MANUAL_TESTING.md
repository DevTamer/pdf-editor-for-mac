# Manual testing checklist

The automated suite covers the editing, add-text and erase engines, the
model and the app's handlers, using simulated events. It can't press real
keys, see real dialogs, or judge how an edit *looks*. Run this checklist against the built
app (`packaging/PDF Editor.dmg`) before a release, after changes to
`EditCanvas` / `EditorApp`, and after bumping the pinned pdfcore version.

Use a few real PDFs:

- one exported from Word or Pages,
- one from a browser's "Save as PDF",
- one scanned-then-OCR'd, for the Erase tool,
- and `tests/fixtures/coretext_sample.pdf`.

## Launch and open

- [ ] The app launches from /Applications (right-click → Open the first
      time).
- [ ] File → Open shows the first page fitted to the window.
- [ ] Double-clicking a PDF that's set to open with PDF Editor, or dropping
      it on the Dock icon, opens it.
- [ ] Resizing the window re-fits the page.
- [ ] Opening a second PDF (File → Open, or from Finder) opens it in a new
      window or tab and leaves the first one as it was, unsaved edits
      included. Selecting several PDFs in File → Open, or dropping several
      on the Dock icon, opens all of them.
- [ ] Opening a PDF that's already open just brings its window forward.
- [ ] Cmd+W closes a window. With only one window left, it closes the
      document and keeps the empty window.
- [ ] Cmd+Q with unsaved edits in two windows asks about each. Cancel
      really cancels quitting.
- [ ] Going to the next page shows its top, not wherever the last page was
      scrolled to.

## Hover and click

- [ ] Hovering over text shows a dashed blue outline and an I-beam cursor.
- [ ] Hovering over empty space shows neither.
- [ ] Clicking a line opens a text box over it, in a similar font and size,
      with the cursor near where you clicked.
- [ ] The sidebar shows the font name, whether it's embedded, and the size.

## Editing

- [ ] Change a word and press Return. It's replaced in the same font, size,
      colour and position, and nothing else on the page moves.
- [ ] Esc cancels with no change.
- [ ] Click into the box, then click a toolbar button, Bold or the History
      list, then press Return: the edit is still applied. Esc still
      cancels.
- [ ] Switch to another app and back, then press Return: applied.
- [ ] Clicking another line applies the current edit and starts editing the
      clicked line.
- [ ] Clearing the box and pressing Return deletes the line.
- [ ] In a PDF from Word (Arial, Calibri, Times New Roman), typing
      characters the document never used gives no warning, and they look
      like the rest of the line (same font, from this Mac or Word).
- [ ] In a PDF whose font isn't installed (e.g. one using Georgia, after
      disabling Georgia in Font Book), the warning names a sensible
      lookalike, and the result looks close. Cancel keeps the box open; OK
      applies.
- [ ] The text box you type in shows a font that looks like the line's.
- [ ] A much longer replacement next to other text warns about the overlap.
- [ ] Typing e.g. 漢字 shows an error and keeps the box open.
- [ ] Edit the same line twice in a row. The second edit shouldn't warn
      unless you add new characters.
- [ ] Copy the edited text from the saved file in Preview and paste it
      somewhere: it should be exactly what you typed, with normal spaces.

## Tricky documents

- [ ] A PDF with comments (a Preview "Text" box, a sticky note) and links:
      editing ordinary text keeps every comment and link. Clicking the
      comment box's own text gives a clear "can't change" message and
      nothing changes.
- [ ] A page cropped in Preview (Tools → Crop): editing and adding text land
      exactly where expected.
- [ ] A colour scan, or a PDF from a print shop (spot colours / ICC
      profiles): erasing part of an image leaves the rest of its colours
      untouched.

## Tools

- [ ] The toolbar buttons, the Tools menu and Cmd+1/2/3 all switch tools,
      and the sidebar shows that tool's panel.
- [ ] Switching tools while a text box is open applies it first.

## Add Text

- [ ] Clicking on the page opens an empty box there, in the chosen font,
      size and colour. Changing them in the sidebar updates the box.
- [ ] Return adds the text with its top-left where you clicked. Clicking
      somewhere else adds it and opens a new box.
- [ ] Bold, Italic and Colour… all show up in the added text.
- [ ] The Font menu lists installed fonts after Helvetica, Times and
      Courier. Text added in one of them looks right in Preview, and the
      file grows by only a few KB.
- [ ] Added text can be edited afterwards with the Edit tool, with no font
      warning.
- [ ] Typing e.g. 漢字 shows an error and keeps the box open.

## Erase

- [ ] Hovering a word outlines it in red. Clicking it erases just that word.
- [ ] Dragging from one word to another highlights everything between them,
      across lines, and erases it on release. The highlight is see-through:
      you can still read the words you're selecting.
- [ ] Dragging from empty space draws a dashed rectangle and erases what's
      inside. A tiny accidental drag does nothing.
- [ ] Erasing real text shows exactly what was behind it: white paper, or a
      coloured box or photo. No box is painted.
- [ ] On a scanned, OCR'd PDF: erasing a word removes it from the scan
      image, and the fill blends with the paper (grain included). The saved
      file is about the same size as before.
- [ ] Background colour fill paints a flat colour instead.
- [ ] Erasing a rectangle over a logo or stamp hides it. A page border
      around it stays.
- [ ] Erasing empty space shows "There was nothing to erase there."

## Find & Replace

- [ ] Cmd+F opens the bar with the cursor in Find. Typing highlights the
      matches (see-through: the text stays readable) and shows "1 of N".
- [ ] Return / Cmd+G and Shift+Return / Cmd+Shift+G step through matches,
      changing page when needed and scrolling the match into view.
- [ ] Replace changes the highlighted match only, in the line's own font,
      and moves to the next. Cmd+Z undoes it.
- [ ] Replace All asks first (count, pages, warnings, skipped matches),
      then changes everything; one Cmd+Z undoes all of it.
- [ ] Esc or Done closes the bar and removes the highlights.
- [ ] On a 100+ page PDF, the first search shows "Searching…" and later
      ones are quick.

## Undo, history, saving

- [ ] Cmd+Z / Cmd+Shift+Z undo and redo edits. Clicking a History entry
      jumps to it.
- [ ] Cmd+Z while the text box is open cancels the typing only.
- [ ] The window title shows "— edited" with unsaved changes.
- [ ] Cmd+S the first time asks where to save (suggests `<name>_edited.pdf`).
      Later it saves to the same file without asking.
- [ ] Saving over the file you opened works, and the app keeps working
      afterwards.
- [ ] Closing or quitting with unsaved edits asks first. Cancel really
      cancels.
- [ ] The saved file opens correctly in Preview and in a browser, and shows
      the edits.
- [ ] File → Metadata on Save: with the default, Preview's Inspector (Cmd+I)
      shows today's modification date and the original title, author and
      creator. With "Remove Document Info" it shows none of them. The
      choice is still selected after quitting and relaunching.
