"""Tests for EditorModel: edits as undo steps, rollback on failure,
unsaved-changes tracking, and saving."""

import os
import stat

import pymupdf
import pytest

import textedit as te
from editor import EditorModel
from conftest import LINE_ABOVE, LINE_BELOW, SECOND_PAGE_TEXT, TARGET, TITLE


@pytest.fixture
def model(sample_pdf_path):
    m = EditorModel()
    m.open_document(str(sample_pdf_path))
    yield m
    m.close_document()


def _run(model, text, page_num=0):
    return next(r for r in model.runs(page_num) if r.text.strip() == text)


def _edit(model, text, new_text, page_num=0):
    return model.apply_plan(model.plan_edit(_run(model, text, page_num), new_text))


def _page_text(model, page_num=0):
    return model.get_page(page_num).get_text()


# -- Lifecycle -----------------------------------------------------------------

def test_open_is_clean_with_original_history(model):
    assert model.history_entries() == ["Original"]
    assert not model.is_dirty
    assert model.save_path is None


def test_closed_model_is_not_dirty():
    assert not EditorModel().is_dirty


# -- Edits and history ---------------------------------------------------------

def test_edit_is_one_undo_step(model):
    label = _edit(model, TARGET, "Edited line")

    assert label == "Edit “Target line to edit” → “Edited line”"
    assert model.history_entries() == ["Original", label]
    assert "Edited line" in _page_text(model)
    assert model.is_dirty

    model.undo()
    assert TARGET in _page_text(model) and "Edited line" not in _page_text(model)
    assert not model.is_dirty  # back at the saved (original) state

    model.redo()
    assert "Edited line" in _page_text(model)
    assert model.is_dirty


def test_delete_label(model):
    assert _edit(model, TARGET, "") == "Delete “Target line to edit”"


def test_long_text_is_shortened_in_labels(model):
    label = _edit(model, TARGET, "A much longer replacement for the target line")
    assert "…" in label and len(label) < 80


def test_noop_records_nothing(model):
    assert _edit(model, TARGET, TARGET) is None
    assert model.history_entries() == ["Original"]
    assert not model.is_dirty


def test_unappliable_plan_raises_and_changes_nothing(model):
    plan = model.plan_edit(_run(model, TARGET), "漢")
    with pytest.raises(te.EditError):
        model.apply_plan(plan)
    assert model.history_entries() == ["Original"]
    assert TARGET in _page_text(model)


def test_failed_edit_is_rolled_back(model, monkeypatch):
    """A failure after the page was already modified must restore it."""
    monkeypatch.setattr(te, "removal_rect",
                        lambda run: pymupdf.Rect(0, 140, 500, 180))
    before = _page_text(model)

    with pytest.raises(te.EditError):
        _edit(model, TARGET, "Edited")

    assert _page_text(model) == before
    assert model.history_entries() == ["Original"]
    monkeypatch.undo()
    # The document is still fully usable afterwards.
    _edit(model, TARGET, "Edited")
    assert "Edited" in _page_text(model)


def test_unexpected_exception_is_rolled_back_too(model, monkeypatch):
    def boom(page, plan):
        page.add_redact_annot(pymupdf.Rect(0, 0, 500, 600), fill=False)
        page.apply_redactions(images=0, graphics=0)  # wipes the page...
        raise RuntimeError("disk on fire")           # ...then fails
    monkeypatch.setattr("editor.apply_edit", boom)
    before = _page_text(model)

    with pytest.raises(te.EditError, match="disk on fire"):
        _edit(model, TARGET, "Edited")

    assert _page_text(model) == before


def test_rollback_keeps_current_page(model, monkeypatch):
    model.current_page = 1  # viewing page 2 while a page-1 edit fails
    monkeypatch.setattr(te, "removal_rect",
                        lambda run: pymupdf.Rect(0, 0, 500, 600))
    with pytest.raises(te.EditError):
        _edit(model, TARGET, "x")
    assert model.current_page == 1


def test_edit_after_undo_discards_redo(model):
    _edit(model, TARGET, "First")
    model.undo()
    _edit(model, TARGET, "Second")
    assert not model.can_redo()
    assert len(model.history_entries()) == 2


# -- Saving --------------------------------------------------------------------

def test_save_writes_the_edit(model, tmp_path):
    _edit(model, TARGET, "Edited line")
    out = tmp_path / "out.pdf"

    model.save_document(str(out))

    saved = pymupdf.open(str(out))
    text = saved[0].get_text()
    assert "Edited line" in text and TARGET not in text
    assert LINE_ABOVE in text and LINE_BELOW in text and TITLE in text
    assert SECOND_PAGE_TEXT in saved[1].get_text()
    saved.close()
    assert not model.is_dirty
    assert model.save_path == str(out)


def test_saved_file_contains_no_trace_of_replaced_text(model, tmp_path):
    _edit(model, TARGET, "Edited line")
    out = tmp_path / "out.pdf"
    model.save_document(str(out))

    doc = pymupdf.open(str(out))
    raw = b"".join((doc.xref_stream(x) or b"") for x in range(1, doc.xref_length())
                   if doc.xref_is_stream(x)).lower()
    doc.close()
    assert b"target line" not in raw
    assert "Target line".encode().hex().encode() not in raw


def test_live_document_survives_saving(model, tmp_path):
    """Regression: in PyMuPDF 1.24, a garbage-collecting save of a document
    with newly written text blanks the live document's pages. Saving must
    leave the open document intact and editable."""
    _edit(model, TARGET, "Edited line")
    before = _page_text(model)

    model.save_document(str(tmp_path / "first.pdf"))

    assert _page_text(model) == before
    _edit(model, "Edited line", "Edited twice")
    model.save_document(str(tmp_path / "second.pdf"))
    second = pymupdf.open(str(tmp_path / "second.pdf"))
    assert "Edited twice" in second[0].get_text()
    assert LINE_ABOVE in second[0].get_text()
    second.close()


def test_saving_over_the_open_file_works(model, sample_pdf_path):
    _edit(model, TARGET, "Edited line")
    model.save_document(str(sample_pdf_path))

    reopened = pymupdf.open(str(sample_pdf_path))
    assert "Edited line" in reopened[0].get_text()
    reopened.close()
    # Still usable after overwriting its own source file.
    _edit(model, "Edited line", "Again")
    assert "Again" in _page_text(model)
    assert [p.name for p in sample_pdf_path.parent.iterdir()
            if p.name.endswith(".pdf")] == [sample_pdf_path.name]


def test_failed_save_leaves_no_partial_file(model, tmp_path, monkeypatch):
    out = tmp_path / "out.pdf"
    out.write_bytes(b"previous contents")
    _edit(model, TARGET, "Edited line")

    def failing_save(self, *args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(pymupdf.Document, "save", failing_save)

    with pytest.raises(RuntimeError):
        model.save_document(str(out))

    assert out.read_bytes() == b"previous contents"
    assert sorted(os.listdir(tmp_path)) == ["out.pdf", "sample.pdf"]  # no temp file
    assert model.is_dirty


def test_editing_never_touches_the_source_file(model, sample_pdf_path, tmp_path):
    before = sample_pdf_path.read_bytes()
    _edit(model, TARGET, "Edited line")
    model.undo()
    model.redo()
    model.save_document(str(tmp_path / "elsewhere.pdf"))
    assert sample_pdf_path.read_bytes() == before


def test_dirty_tracking_follows_the_saved_state(model, tmp_path):
    _edit(model, TARGET, "One")
    model.save_document(str(tmp_path / "out.pdf"))
    assert not model.is_dirty
    _edit(model, "One", "Two")
    assert model.is_dirty
    model.undo()
    assert not model.is_dirty       # back to what was saved
    model.undo()
    assert model.is_dirty           # the original isn't what was saved


def test_describe_font(model):
    assert model.describe_font(_run(model, TITLE)) == "NimbusRoman-Regular (embedded)"
    assert model.describe_font(_run(model, TARGET)) == "Helvetica (standard PDF font)"


def test_password_protected_pdf_is_refused_cleanly(tmp_path, sample_pdf_path):
    path = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 100), "secret", fontname="helv")
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             user_pw="pw", owner_pw="pw")
    doc.close()

    m = EditorModel()
    m.open_document(str(sample_pdf_path))
    with pytest.raises(ValueError, match="password-protected"):
        m.open_document(str(path))

    # Nothing half-open left behind.
    assert m.doc is None
    assert not m.is_dirty
    assert m.history_entries() == []


def test_unreadable_file_is_refused_cleanly(tmp_path):
    path = tmp_path / "junk.pdf"
    path.write_bytes(b"this is not a pdf")
    m = EditorModel()
    with pytest.raises(Exception):
        m.open_document(str(path))
    assert m.doc is None and not m.is_dirty


def test_new_saved_file_gets_normal_permissions(model, tmp_path):
    out = tmp_path / "new.pdf"
    model.save_document(str(out))
    umask = os.umask(0)
    os.umask(umask)
    assert stat.S_IMODE(out.stat().st_mode) == 0o666 & ~umask


def test_overwriting_keeps_the_file_permissions(model, tmp_path):
    out = tmp_path / "existing.pdf"
    out.write_bytes(b"old")
    os.chmod(out, 0o640)
    model.save_document(str(out))
    assert stat.S_IMODE(out.stat().st_mode) == 0o640
