# Manual testing checklist

The automated suite covers the editing engine, the model and the app's
handlers, using simulated events. It can't press real keys, see real
dialogs, or judge how an edit *looks*. Run this checklist against the built
app (`packaging/PDF Editor.dmg`) before a release, after changes to
`EditCanvas` / `EditorApp`, and after bumping the pinned pdfcore version.

Use a few real PDFs:

- one exported from Word or Pages,
- one from a browser's "Save as PDF",
- one scanned-then-OCR'd if you have it,
- and `tests/fixtures/coretext_sample.pdf`.

## Launch and open

- [ ] The app launches from /Applications (right-click → Open the first
      time).
- [ ] File → Open shows the first page fitted to the window.
- [ ] Double-clicking a PDF that's set to open with PDF Editor, or dropping
      it on the Dock icon, opens it.
- [ ] Resizing the window re-fits the page.

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
- [ ] Clicking another line applies the current edit and starts editing the
      clicked line.
- [ ] Clearing the box and pressing Return deletes the line.
- [ ] Typing a character the font doesn't have shows the substitution
      warning. Cancel keeps the box open; OK applies.
- [ ] A much longer replacement next to other text warns about the overlap.
- [ ] Typing e.g. 漢字 shows an error and keeps the box open.
- [ ] Edit the same line twice in a row. The second edit shouldn't warn
      unless you add new characters.
- [ ] Copy the edited text from the saved file in Preview and paste it
      somewhere: it should be exactly what you typed, with normal spaces.

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
