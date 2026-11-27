"""What saving does with a PDF's metadata (File > Metadata on Save)."""

import json
import re

import pymupdf
import pytest

import editor

OLD_DATE = "D:20240102120000Z"
OLD_XMP_DATE = "2024-01-02T12:00:00Z"
XMP = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
    f'xmp:CreatorTool="Microsoft Word" xmp:ModifyDate="{OLD_XMP_DATE}" '
    f"xmp:CreateDate='{OLD_XMP_DATE}'>"
    f"<xmp:MetadataDate>{OLD_XMP_DATE}</xmp:MetadataDate>"
    "</rdf:Description></rdf:RDF></x:xmpmeta>")
INFO = {"title": "Q3 Invoice", "author": "Jane Doe", "subject": "Billing",
        "keywords": "invoice", "creator": "Microsoft Word",
        "producer": "macOS Quartz PDFContext",
        "creationDate": "D:20240101120000Z", "modDate": OLD_DATE}


def _source(tmp_path, info=True, xmp=True):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Invoice total", fontname="helv", fontsize=14)
    if info:
        doc.set_metadata(INFO)
        info_xref = int(doc.xref_get_key(-1, "Info")[1].split()[0])
        doc.xref_set_key(info_xref, "Company", "(Acme Ltd)")
    if xmp:
        doc.set_xml_metadata(XMP)
    page_meta = doc.get_new_xref()
    doc.update_object(page_meta, "<</Type/Metadata/Subtype/XML>>")
    doc.update_stream(page_meta, b"<x:xmpmeta>page level</x:xmpmeta>")
    doc.xref_set_key(page.xref, "Metadata", f"{page_meta} 0 R")
    doc.xref_set_key(page.xref, "PieceInfo", "<</Illustrator<</Private 1 0 R>>>>")
    doc.xref_set_key(doc.pdf_catalog(), "PieceInfo", "<</Word<</Data(x)>>>>")
    path = tmp_path / "source.pdf"
    doc.save(str(path))
    doc.close()
    return path


def _save(tmp_path, source, mode):
    model = editor.EditorModel()
    model.open_document(str(source))
    out = tmp_path / f"out-{mode}.pdf"
    model.save_document(str(out), metadata=mode)
    return model, pymupdf.open(str(out))


def _custom(doc, key):
    kind, value = doc.xref_get_key(-1, "Info")
    if kind != "xref":
        return None
    return doc.xref_get_key(int(value.split()[0]), key)[1]


def test_default_updates_only_the_modification_dates(tmp_path):
    model, saved = _save(tmp_path, _source(tmp_path), editor.METADATA_UPDATE_DATE)
    meta = saved.metadata
    assert meta["modDate"] != OLD_DATE
    assert meta["modDate"].startswith(pymupdf.get_pdf_now()[:10])   # D:YYYYMMDD
    # Nothing else changes: not even the Producer.
    for key in ("title", "author", "subject", "keywords", "creator",
                "producer", "creationDate"):
        assert meta[key] == INFO[key], key
    assert _custom(saved, "Company") == "Acme Ltd"

    xmp = saved.get_xml_metadata()
    today = editor._xmp_now()[:10]
    assert re.search(rf'xmp:ModifyDate="{today}', xmp)
    assert re.search(rf"<xmp:MetadataDate>{today}", xmp)
    assert f"xmp:CreateDate='{OLD_XMP_DATE}'" in xmp       # creation untouched
    assert 'xmp:CreatorTool="Microsoft Word"' in xmp
    # The open document itself isn't changed by saving.
    assert model.doc.metadata["modDate"] == OLD_DATE


def test_keep_leaves_metadata_exactly_as_it_was(tmp_path):
    source = _source(tmp_path)
    _model, saved = _save(tmp_path, source, editor.METADATA_KEEP)
    original = pymupdf.open(str(source))
    assert {k: saved.metadata[k] for k in INFO} == INFO
    assert saved.get_xml_metadata() == original.get_xml_metadata()
    assert _custom(saved, "Company") == "Acme Ltd"


def test_remove_takes_out_document_info_xmp_and_private_data(tmp_path):
    _model, saved = _save(tmp_path, _source(tmp_path), editor.METADATA_REMOVE)
    assert saved.xref_get_key(-1, "Info")[0] == "null"
    assert all(not saved.metadata[k] for k in INFO)
    assert saved.get_xml_metadata() == ""
    page = saved[0]
    assert saved.xref_get_key(page.xref, "Metadata")[0] == "null"
    assert saved.xref_get_key(page.xref, "PieceInfo")[0] == "null"
    assert saved.xref_get_key(saved.pdf_catalog(), "PieceInfo")[0] == "null"
    raw = open(saved.name, "rb").read()
    streams = b"".join(saved.xref_stream(x) or b"" for x in range(1, saved.xref_length()))
    for leftover in (b"Jane Doe", b"Acme Ltd", b"Microsoft Word", b"Quartz",
                     b"page level"):
        assert leftover not in raw + streams, leftover
    assert "Invoice total" in page.get_text()               # content untouched


def test_updating_the_date_doesnt_invent_metadata(tmp_path):
    _model, saved = _save(tmp_path, _source(tmp_path, info=False, xmp=False),
                          editor.METADATA_UPDATE_DATE)
    assert saved.xref_get_key(-1, "Info")[0] == "null"
    assert saved.get_xml_metadata() == ""


def test_unknown_mode_is_refused(tmp_path, sample_pdf_path):
    model = editor.EditorModel()
    model.open_document(str(sample_pdf_path))
    with pytest.raises(ValueError):
        model.save_document(str(tmp_path / "x.pdf"), metadata="bogus")
    assert not (tmp_path / "x.pdf").exists()


def test_settings_survive_a_relaunch_and_bad_files(tmp_path):
    assert editor.load_settings() == {}
    editor.save_settings({"metadata": editor.METADATA_REMOVE})
    assert editor.load_settings() == {"metadata": editor.METADATA_REMOVE}
    with open(editor.SETTINGS_PATH, "w") as fh:
        fh.write("{not json")
    assert editor.load_settings() == {}
