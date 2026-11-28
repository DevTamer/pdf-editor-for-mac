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
    today = editor.xmp_date(editor._now())[:10]
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


# -- Review findings ------------------------------------------------------------------

import datetime  # noqa: E402

XMP_HEAD = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">')
XMP_TAIL = "</rdf:RDF></x:xmpmeta>"
WHEN = datetime.datetime(2026, 1, 15, 9, 30, 0,
                         tzinfo=datetime.timezone(datetime.timedelta(hours=-8)))


@pytest.mark.parametrize("offset, expected", [
    ((-8, 0), "D:20260115093000-08'00'"),       # winter, west of UTC
    ((5, 30), "D:20260115093000+05'30'"),       # half-hour zone, east
    ((-3, -30), "D:20260115093000-03'30'"),     # half-hour zone, west
    ((0, 0), "D:20260115093000+00'00'"),
])
def test_pdf_dates_use_the_real_offset(offset, expected):
    tz = datetime.timezone(datetime.timedelta(hours=offset[0], minutes=offset[1]))
    assert editor.pdf_date(WHEN.replace(tzinfo=tz)) == expected


def test_info_and_xmp_dates_agree(tmp_path):
    _model, saved = _save(tmp_path, _source(tmp_path), editor.METADATA_UPDATE_DATE)
    info = saved.metadata["modDate"]                       # D:YYYYMMDDHHmmSS+HH'mm'
    xmp = re.search(r'xmp:ModifyDate="([^"]+)"', saved.get_xml_metadata()).group(1)
    as_xmp = (f"{info[2:6]}-{info[6:8]}-{info[8:10]}T{info[10:12]}:{info[12:14]}:"
              f"{info[14:16]}{info[16:19]}:{info[20:22]}")
    assert as_xmp == xmp


@pytest.mark.parametrize("body, updated, kept", [
    # Acrobat 4-5 files bind the "xap" prefix.
    ('<rdf:Description xmlns:xap="http://ns.adobe.com/xap/1.0/">'
     '<xap:ModifyDate>2020-01-01T00:00:00Z</xap:ModifyDate></rdf:Description>', 1, 0),
    # An element with attributes of its own.
    ('<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
     '<xmp:ModifyDate rdf:datatype="d">2020-01-01T00:00:00Z</xmp:ModifyDate>'
     '</rdf:Description>', 1, 0),
    # The XMP namespace as the element's default namespace.
    ('<rdf:Description><ModifyDate xmlns="http://ns.adobe.com/xap/1.0/">'
     '2020-01-01T00:00:00Z</ModifyDate></rdf:Description>', 1, 0),
    # A custom field that merely ends in "xmp:ModifyDate".
    ('<rdf:Description xmlns:myxmp="urn:custom" myxmp:ModifyDate="2020-01-01T00:00:00Z"'
     ' xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:ModifyDate="2020-01-01T00:00:00Z"/>',
     1, 1),
    # "xmp" bound to some other namespace isn't the XMP date.
    ('<rdf:Description xmlns:xmp="urn:other"><xmp:ModifyDate>2020-01-01T00:00:00Z'
     '</xmp:ModifyDate></rdf:Description>', 0, 1),
    # Placed images' dates in the Pantry aren't the document's.
    ('<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:xmpMM="m" '
     'xmp:ModifyDate="2020-01-01T00:00:00Z"><xmpMM:Pantry><rdf:Bag><rdf:li>'
     '<rdf:Description xmp:ModifyDate="2020-01-01T00:00:00Z"/></rdf:li></rdf:Bag>'
     '</xmpMM:Pantry></rdf:Description>', 1, 1),
    # Damaged XMP is left as it is.
    ('<rdf:Description xmp:ModifyDate="2020-01-01T00:00:00Z" xmp:Create', 0, 1),
])
def test_xmp_dates_found_exactly(body, updated, kept):
    xmp = XMP_HEAD + body + XMP_TAIL
    out = editor.update_xmp_dates(xmp, WHEN)
    assert out.count(editor.xmp_date(WHEN)) == updated
    assert out.count("2020-01-01T00:00:00Z") == kept


@pytest.mark.parametrize("info", ["null", "(not a dictionary)", "42"])
def test_a_damaged_info_entry_doesnt_stop_saving(tmp_path, info):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Hello")
    bad = doc.get_new_xref()
    doc.update_object(bad, info)
    doc.xref_set_key(-1, "Info", f"{bad} 0 R")
    path = tmp_path / "bad.pdf"
    doc.save(str(path))
    _model, saved = _save(tmp_path, path, editor.METADATA_UPDATE_DATE)
    assert "Hello" in saved[0].get_text()


def test_a_direct_info_dictionary_gets_the_date_too():
    doc = pymupdf.open()
    doc.new_page()
    doc.xref_set_key(-1, "Info", "<</Title(T)/ModDate(D:20200101000000Z)>>")
    editor.update_modification_date(doc, WHEN)
    assert doc.xref_get_key(-1, "Info/ModDate")[1] == editor.pdf_date(WHEN)
    assert doc.xref_get_key(-1, "Info/Title")[1] == "T"


def test_remove_takes_private_data_off_embedded_graphics_too(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    src = pymupdf.open()
    src.new_page().insert_text((20, 40), "Logo")
    page.show_pdf_page(pymupdf.Rect(0, 0, 200, 200), src, 0)
    form = page.get_xobjects()[0][0]
    doc.xref_set_key(form, "PieceInfo",
                     "<</Illustrator<</Private(ILLUSTRATOR-PRIVATE-COPY)>>>>")
    path = tmp_path / "form.pdf"
    doc.save(str(path))
    _model, saved = _save(tmp_path, path, editor.METADATA_REMOVE)
    assert b"ILLUSTRATOR-PRIVATE-COPY" not in open(saved.name, "rb").read()
    assert "Logo" in saved[0].get_text()


def test_saving_settings_keeps_other_settings(tmp_path):
    editor.save_settings({"other": 1})
    editor.save_settings({"metadata": editor.METADATA_KEEP})
    assert editor.load_settings() == {"other": 1, "metadata": editor.METADATA_KEEP}
