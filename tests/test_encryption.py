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


def _contains(doc, word):
    """Whether ``word`` is anywhere in the decrypted file: page text, any
    object, any stream (decompressed), as plain text or as the hex string
    content streams often write it in."""
    blob = "".join(page.get_text() for page in doc).encode()
    for xref in range(1, doc.xref_length()):
        try:
            blob += doc.xref_object(xref, compressed=True).encode("latin-1", "replace")
            blob += doc.xref_stream(xref) or b""
        except Exception:
            pass
    hexed = word.encode().hex().encode()
    return word.encode() in blob or hexed in blob.lower()


@pytest.mark.parametrize("mode", editor.METADATA_MODES)
def test_saving_keeps_encryption_permissions_and_owner_password(encrypted, tmp_path, mode):
    original = pymupdf.open(str(encrypted))
    assert _contains(original, "Confidential")     # (so the check below means something)
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
    assert not _contains(saved, "Confidential")
    # A changed file gets a new second /ID; the first one stays.
    ids = [d.xref_get_key(-1, "ID")[1].strip("[]<>").split("><") for d in (original, saved)]
    assert ids[0][0] == ids[1][0] and ids[0][1] != ids[1][1]
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



# -- What can't be kept is never silently lost -------------------------------------------

UNSUPPORTED = FIXTURES / "unsupported_cleartext_metadata.pdf"


@pytest.mark.parametrize("key, value", [
    ("StrF", "/Identity"),          # strings not encrypted, streams are
    ("StmF", "/Identity"),          # the other way round
    ("EncryptMetadata", "false"),   # metadata left in the clear
    ("Filter", "/Adobe.PubSec"),    # another security handler
])
def test_encryption_setups_mupdf_cant_write_back_are_recognised(key, value):
    doc = pymupdf.open(str(FIXTURES / "encrypted_aes_128.pdf"))
    assert editor.keep_encryption_problem(doc) is None
    encrypt = int(doc.xref_get_key(-1, "Encrypt")[1].split()[0])
    doc.xref_set_key(encrypt, key, value)
    assert "can't be kept" in editor.keep_encryption_problem(doc)


def test_a_per_stream_crypt_filter_is_recognised():
    doc = pymupdf.open(str(FIXTURES / "encrypted_aes_256.pdf"))
    stream = doc[0].get_contents()[0]
    doc.xref_set_key(stream, "Filter", "[/Crypt /FlateDecode]")
    assert editor.keep_encryption_problem(doc)


def test_unsupported_encryption_is_never_saved_wrong(tmp_path):
    path = tmp_path / "u.pdf"
    shutil.copy(UNSUPPORTED, path)
    model = editor.EditorModel()
    model.open_document(str(path))
    assert not model.keeps_restrictions
    assert "metadata unencrypted" in model.restriction_problem
    out = tmp_path / "out.pdf"
    with pytest.raises(editor.RestrictionsError):
        model.save_document(str(out))
    assert not out.exists()
    # Only when asked to, it's saved without them.
    model.save_document(str(out), drop_restrictions=True)
    saved = pymupdf.open(str(out))
    assert saved.metadata["encryption"] is None
    assert "Confidential page 1" in saved[0].get_text()


def test_a_save_that_doesnt_read_back_right_is_refused(encrypted, tmp_path, monkeypatch):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    real = editor.keep_encryption

    def lose_a_page(edited, original):
        target = real(edited, original)
        target[1].clean_contents()
        target.update_stream(target[1].get_contents()[0], b"")
        return target
    monkeypatch.setattr(editor, "keep_encryption", lose_a_page)
    out = tmp_path / "out.pdf"
    with pytest.raises(editor.RestrictionsError, match="couldn't be saved correctly"):
        model.save_document(str(out))
    assert not out.exists() and list(tmp_path.glob("*.pdf")) == [encrypted]


def test_the_encryption_slot_must_hold_nothing_else(encrypted):
    """If the edited copy had other content under the number of the
    original's /Encrypt object, skipping it would lose that content."""
    original = encrypted.read_bytes()
    target = pymupdf.open(stream=original, filetype="pdf")
    slot = int(target.xref_get_key(-1, "Encrypt")[1].split()[0])
    edited = pymupdf.open(stream=pymupdf.open(str(encrypted)).tobytes(), filetype="pdf")
    edited.update_object(slot, "<</Title(Something else)>>")
    with pytest.raises(ValueError):
        editor.keep_encryption(edited, original)


def test_a_file_replaced_while_opening_isnt_trusted(encrypted, monkeypatch):
    monkeypatch.setattr(editor, "_same_file", lambda doc, original: False)
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    assert not model.keeps_restrictions and "changed" in model.restriction_problem


def test_same_file_compares_the_permanent_id(tmp_path):
    a = pymupdf.open(str(FIXTURES / "encrypted_aes_256.pdf"))
    a.tobytes()                                  # (rewrites ID[1] in memory)
    same = pymupdf.open(str(FIXTURES / "encrypted_aes_256.pdf"))
    assert editor._same_file(a, same)
    # Two files alike in every way checked but their ID: same structure,
    # same encryption, one letter of text different.
    twins = []
    for letter in "AB":
        source = pymupdf.open()
        source.new_page().insert_text((50, 80), f"Twin {letter}", fontname="helv")
        path = tmp_path / f"{letter}.pdf"
        source.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                    owner_pw="owner", user_pw="", permissions=pymupdf.PDF_PERM_PRINT)
        twins.append(pymupdf.open(str(path)))
    assert twins[0].xref_length() == twins[1].xref_length()
    assert twins[0].metadata["encryption"] == twins[1].metadata["encryption"]
    assert not editor._same_file(twins[0], twins[1])


def test_an_unreadable_file_changes_nothing(encrypted, tmp_path):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    with pytest.raises(OSError):
        model.open_document(str(tmp_path / "missing.pdf"))
    assert model.file_path == str(encrypted) and model.keeps_restrictions



def test_a_save_whose_metadata_doesnt_read_back_is_refused(encrypted, tmp_path, monkeypatch):
    model = editor.EditorModel()
    model.open_document(str(encrypted))
    real = editor.keep_encryption

    def garble_xmp(edited, original):
        target = real(edited, original)
        target.set_xml_metadata("<x:xmpmeta>garbled</x:xmpmeta>")
        return target
    monkeypatch.setattr(editor, "keep_encryption", garble_xmp)
    with pytest.raises(editor.RestrictionsError):
        model.save_document(str(tmp_path / "out.pdf"))
