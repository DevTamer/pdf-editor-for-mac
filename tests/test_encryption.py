"""Saving keeps a PDF's encryption and permission restrictions.

Uses tests/fixtures/encrypted_*.pdf: an owner-password-only document
(opens without a password; printing allowed, changes and copying not) in
each standard encryption scheme, with compressed object streams. Made by
tests/fixtures/make_encrypted_samples.sh.
"""

import shutil

import pymupdf
import pytest

import editor
from conftest import FIXTURES
from erase import ErasePlan

SAMPLES = sorted(FIXTURES.glob("encrypted_*.pdf"))
OWNER_PASSWORD = "owner"
OWNER = 4                     # what Document.authenticate returns for the owner


@pytest.fixture(params=SAMPLES, ids=lambda p: p.stem)
def encrypted(request, tmp_path):
    path = tmp_path / request.param.name
    shutil.copy(request.param, path)
    return path


def _edit(model):
    """An edit, a find-and-replace style edit on page 2, and an in-place
    image erase: every kind of object the save has to carry over."""
    run = next(r for r in model.runs(0) if r.text.startswith("Confidential"))
    model.apply_plan(model.plan_edit(run, "Public page 1"))
    match = model.find("Confidential")[0]
    model.apply_replace(model.plan_replace(match, "Public"), match, "Public")
    model.erase(ErasePlan(page_num=0, area=pymupdf.Rect(260, 160, 320, 220)))


def _decrypted_text(doc):
    """Every page's text plus every stream, decrypted and decompressed."""
    blob = "".join(page.get_text() for page in doc).encode()
    for xref in range(1, doc.xref_length()):
        try:
            blob += doc.xref_stream(xref) or b""
        except Exception:
            pass
    return blob


@pytest.mark.parametrize("mode", editor.METADATA_MODES)
def test_saving_keeps_encryption_permissions_and_owner_password(encrypted, tmp_path, mode):
    original = pymupdf.open(str(encrypted))
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    assert model.keeps_restrictions
    _edit(model)
    out = tmp_path / "saved.pdf"
    model.save_document(str(out), metadata=mode)

    saved = pymupdf.open(str(out))
    assert saved.metadata["encryption"] == original.metadata["encryption"]
    assert saved.permissions == original.permissions
    assert not saved.needs_pass                       # still opens without one
    # The original owner password still lifts the restrictions.
    assert saved.authenticate(OWNER_PASSWORD) == OWNER
    # The content is exactly what was being edited.
    for page_num in range(len(saved)):
        assert saved[page_num].get_text() == model.get_page(page_num).get_text()
        assert (saved[page_num].get_pixmap(dpi=40).samples
                == model.get_page(page_num).get_pixmap(dpi=40).samples)
    assert b"Confidential" not in _decrypted_text(saved)
    assert "Keep this line" in saved[1].get_text()


def test_metadata_modes_work_on_encrypted_pdfs(encrypted, tmp_path):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    saved = {}
    for mode in editor.METADATA_MODES:
        out = tmp_path / f"{mode}.pdf"
        model.save_document(str(out), metadata=mode)
        saved[mode] = pymupdf.open(str(out)).metadata
    assert saved[editor.METADATA_KEEP]["title"] == "Encrypted sample"
    assert saved[editor.METADATA_KEEP]["modDate"] == "D:20240102120000Z"
    assert saved[editor.METADATA_UPDATE_DATE]["title"] == "Encrypted sample"
    assert saved[editor.METADATA_UPDATE_DATE]["modDate"] != "D:20240102120000Z"
    assert saved[editor.METADATA_REMOVE]["title"] == ""
    assert all(saved[m]["encryption"] for m in editor.METADATA_MODES)


def test_undo_then_save_still_keeps_encryption(encrypted, tmp_path):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    _edit(model)
    model.undo()
    model.undo()
    out = tmp_path / "saved.pdf"
    model.save_document(str(out))
    saved = pymupdf.open(str(out))
    assert saved.metadata["encryption"] and saved.authenticate(OWNER_PASSWORD) == OWNER
    assert saved[0].get_text() == model.get_page(0).get_text()
    assert "Public page 1" in saved[0].get_text()


def test_saving_over_the_original_again_and_again(encrypted):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    _edit(model)
    model.save_document(str(encrypted))
    run = next(r for r in model.runs(1) if r.text.startswith("Keep"))
    model.apply_plan(model.plan_edit(run, "Kept this line"))
    model.save_document(str(encrypted))
    saved = pymupdf.open(str(encrypted))
    assert saved.metadata["encryption"] and saved.authenticate(OWNER_PASSWORD) == OWNER
    assert "Kept this line" in saved[1].get_text()


def test_unencrypted_pdfs_stay_unencrypted(sample_pdf_path, tmp_path):
    model = editor.EditorModel()
    model.open_document(str(sample_pdf_path))
    assert not model.keeps_restrictions
    out = tmp_path / "saved.pdf"
    model.save_document(str(out))
    assert pymupdf.open(str(out)).metadata["encryption"] is None


def test_opening_another_pdf_forgets_the_restrictions(encrypted, sample_pdf_path, tmp_path):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    model.open_document(str(sample_pdf_path))
    out = tmp_path / "saved.pdf"
    model.save_document(str(out))
    assert pymupdf.open(str(out)).metadata["encryption"] is None


def test_closing_forgets_the_restrictions(encrypted):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    model.close_document()
    assert not model.keeps_restrictions
