"""Matching a PDF's fonts with installed ones (fontmatch.py) and using
them for new characters (textedit.FontChain).

Uses real macOS fonts, made "installed" one by one with the
``installed_fonts`` fixture, so results don't depend on what else this Mac
has installed.
"""

import io

import pymupdf
import pytest
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

import fontmatch
import textedit as te
from conftest import SUPPLEMENTAL, SYSTEM_FONTS

ARIAL = "Supplemental/Arial.ttf"
ARIAL_BOLD = "Supplemental/Arial Bold.ttf"
GEORGIA = "Supplemental/Georgia.ttf"
COURIER_NEW = "Supplemental/Courier New.ttf"
HELVETICA = "Helvetica.ttc"
TIMES = "Times.ttc"
PALATINO = "Palatino.ttc"


def _subset_bytes(path, text):
    """The font at ``path`` cut down to ``text``'s characters, keeping its
    character map: how most PDF generators embed fonts."""
    font = TTFont(str(path))
    options = Options()
    options.name_IDs = ["*"]
    options.notdef_outline = True
    subsetter = Subsetter(options=options)
    subsetter.populate(unicodes={ord(c) for c in text})
    subsetter.subset(font)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def _pdf_with(font_path, text, extra_pages=()):
    """One line of ``text`` in an embedded subset of the font; each of
    ``extra_pages`` is another page with its own subset of the same font."""
    doc = pymupdf.open()
    for page_text in (text,) + tuple(extra_pages):
        page = doc.new_page(width=400, height=200)
        font = pymupdf.Font(fontbuffer=_subset_bytes(font_path, page_text))
        writer = pymupdf.TextWriter(page.rect)
        writer.append((40, 100), page_text, font=font, fontsize=14)
        writer.write_text(page)
    return pymupdf.open("pdf", doc.tobytes(garbage=3, deflate=True))


def _plan(doc, new_text, page_num=0):
    page = doc[page_num]
    run = te.page_runs(page, page_num)[0]
    return te.plan_edit(doc, page, run, new_text)


def _apply(doc, plan, page_num=0):
    te.apply_edit(doc[page_num], plan)


def _span_fonts(page):
    return {s["font"] for b in page.get_text("dict")["blocks"] if b["type"] == 0
            for line in b["lines"] for s in line["spans"]}


def _non_embedded_pdf(basefont, width_font_path, text):
    """A line in a TrueType font the PDF names but doesn't embed (common
    in older Office PDFs), with the widths Word would record for it."""
    widths_font = pymupdf.Font(fontfile=str(width_font_path))
    widths = " ".join(str(round(widths_font.glyph_advance(c) * 1000))
                      for c in range(32, 127))
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    font_xref = doc.get_new_xref()
    doc.update_object(font_xref, (
        f"<</Type/Font/Subtype/TrueType/BaseFont/{basefont}/FirstChar 32"
        f"/LastChar 126/Widths[{widths}]/Encoding/WinAnsiEncoding>>"))
    page_xref = page.xref
    doc.xref_set_key(page_xref, "Resources", f"<</Font<</F1 {font_xref} 0 R>>>>")
    stream_xref = doc.get_new_xref()
    doc.update_object(stream_xref, "<<>>")
    doc.update_stream(stream_xref, f"BT /F1 14 Tf 40 100 Td ({text}) Tj ET".encode())
    doc.xref_set_key(page_xref, "Contents", f"{stream_xref} 0 R")
    return pymupdf.open("pdf", doc.tobytes())


# -- Names ----------------------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("ArialMT", "Arial"),
    ("ABCDEF+Arial-BoldMT", "Arial Bold"),
    ("TimesNewRomanPSMT", "Times New Roman"),
    ("TimesNewRomanPS-BoldItalicMT", "Times New Roman Bold Italic"),
    ("Arial,Bold", "Arial Bold"),
    ("Helvetica-Oblique", "Helvetica Italic"),
    ("Arial Regular", "ArialMT"),
])
def test_name_spellings_that_mean_the_same_face(a, b):
    assert te.same_font_name(a, b)


@pytest.mark.parametrize("a, b", [
    ("ArialMT", "Arial-BoldMT"),
    ("Helvetica", "HelveticaNeue"),
    ("Times-Roman", "Times-Bold"),
    ("Georgia", "Verdana"),
])
def test_different_faces_dont_match(a, b):
    assert not te.same_font_name(a, b)


# -- The index of installed fonts ----------------------------------------------------

def test_exact_match_by_name(installed_fonts):
    installed_fonts(ARIAL, ARIAL_BOLD, TIMES, "Supplemental/Times New Roman.ttf")
    index = fontmatch.default_index()
    assert index.exact("ABCDEF+ArialMT").label == "Arial"
    assert index.exact("Arial-BoldMT").label == "Arial Bold"
    assert index.exact("Arial,Bold").label == "Arial Bold"
    assert index.exact("TimesNewRomanPSMT").label == "Times New Roman"
    assert index.exact("Times-Bold", bold=True).label == "Times Bold"
    assert index.exact("Calibri") is None


def test_faces_inside_a_ttc_collection_load_individually(installed_fonts):
    installed_fonts(HELVETICA)
    face = fontmatch.default_index().exact("Helvetica-Bold", bold=True)
    assert face.index > 0
    font = fontmatch.face_font(face)
    assert font.name == "Helvetica Bold"
    assert fontmatch.subset_font(face, "Hi").has_glyph(ord("H"))


def test_index_is_cached_and_notices_new_files(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "Arial.ttf").symlink_to(SUPPLEMENTAL / "Arial.ttf")
    cache = tmp_path / "cache.json"
    first = fontmatch.FontIndex(dirs=(str(fonts),), cache_path=str(cache))
    assert [f.family for f in first.faces()] == ["Arial"]
    assert cache.exists()

    (fonts / "Georgia.ttf").symlink_to(SUPPLEMENTAL / "Georgia.ttf")
    second = fontmatch.FontIndex(dirs=(str(fonts),), cache_path=str(cache))
    assert sorted(f.family for f in second.faces()) == ["Arial", "Georgia"]


def test_fonts_that_forbid_embedding_are_never_used(tmp_path):
    font = TTFont(str(SUPPLEMENTAL / "Georgia.ttf"))
    font["OS/2"].fsType = fontmatch.FS_RESTRICTED
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    font.save(str(fonts / "Restricted.ttf"))
    index = fontmatch.FontIndex(dirs=(str(fonts),), cache_path=None)
    assert len(index.faces()) == 1
    assert index.usable_faces() == []
    assert index.exact("Georgia") is None


def test_subset_is_a_small_fraction_of_the_font(installed_fonts):
    installed_fonts(ARIAL)
    face = fontmatch.default_index().exact("Arial")
    subset = fontmatch.subset_font(face, "Hello")
    full_size = (SUPPLEMENTAL / "Arial.ttf").stat().st_size
    assert len(subset.buffer) < full_size / 20
    assert subset.has_glyph(ord("H")) and not subset.has_glyph(ord("Z"))


# -- The closest-looking font ----------------------------------------------------------

def _closest(reference_path, text, **style):
    ref = fontmatch.Reference(font=pymupdf.Font(fontfile=str(reference_path)),
                              chars=text, **style)
    return [face.family for _d, face in
            fontmatch.closest_faces(ref, fontmatch.default_index(), limit=3)]


def test_arial_finds_helvetica(installed_fonts):
    installed_fonts(HELVETICA, TIMES, PALATINO, COURIER_NEW)
    assert _closest(SUPPLEMENTAL / "Arial.ttf", "Invoice total due 2024")[0] == "Helvetica"


def test_arial_bold_finds_helvetica_bold(installed_fonts):
    installed_fonts(HELVETICA, TIMES, PALATINO, COURIER_NEW)
    family = _closest(SUPPLEMENTAL / "Arial Bold.ttf", "Invoice total due 2024",
                      bold=True)[0]
    face = fontmatch.default_index().family_faces(family)
    assert family == "Helvetica" and face


def test_times_new_roman_finds_times(installed_fonts):
    installed_fonts(HELVETICA, TIMES, COURIER_NEW)
    assert _closest(SUPPLEMENTAL / "Times New Roman.ttf",
                    "Quarterly report for members")[0] == "Times"


def test_a_serif_font_never_gets_a_sans_lookalike(installed_fonts):
    installed_fonts(HELVETICA, TIMES, PALATINO, COURIER_NEW)
    assert _closest(SUPPLEMENTAL / "Georgia.ttf", "Quarterly report for members",
                    serif=True)[0] in ("Palatino", "Times")


def test_monospaced_gets_monospaced(installed_fonts):
    installed_fonts(HELVETICA, TIMES, COURIER_NEW, "Courier.ttc")
    ref = fontmatch.Reference(font=pymupdf.Font(fontfile=str(SYSTEM_FONTS / "Courier.ttc")),
                              chars="Fine print in Courier", mono=True)
    ranked = fontmatch.closest_faces(ref, fontmatch.default_index(), limit=5)
    assert ranked and all(face.mono for _d, face in ranked)


# -- Editing with installed fonts -------------------------------------------------------

def test_missing_characters_use_the_installed_copy_without_a_warning(installed_fonts):
    installed_fonts(ARIAL, HELVETICA, TIMES)
    doc = _pdf_with(SUPPLEMENTAL / "Arial.ttf", "Invoice total due")
    plan = _plan(doc, "Invoice TOTAL Q9 due")

    kinds = {s.text: s.choice.kind for s in plan.segments}
    assert kinds["TOTAL"] == te.INSTALLED and kinds["Q9"] == te.INSTALLED
    assert kinds["Invoice "] == te.ORIGINAL
    assert plan.warnings == [] and plan.substituted == []

    _apply(doc, plan)
    assert "Invoice TOTAL Q9 due" in doc[0].get_text()
    assert all("Arial" in name for name in _span_fonts(doc[0]))


def test_installed_fonts_are_embedded_as_small_subsets(installed_fonts):
    installed_fonts(ARIAL)
    doc = _pdf_with(SUPPLEMENTAL / "Arial.ttf", "Invoice total due")
    before = len(doc.tobytes(deflate=True))
    _apply(doc, _plan(doc, "Invoice TOTAL Q9 due"))
    copy = pymupdf.open("pdf", doc.tobytes())          # the app saves a copy
    grown = len(copy.tobytes(garbage=3, deflate=True)) - before
    assert grown < 30_000          # Arial itself is ~770 KB
    assert "Invoice TOTAL Q9 due" in copy[0].get_text()


def test_lookalike_is_used_and_named_when_the_font_isnt_installed(installed_fonts):
    installed_fonts(HELVETICA, TIMES, PALATINO)
    doc = _pdf_with(SUPPLEMENTAL / "Arial.ttf", "Invoice total due")
    plan = _plan(doc, "Invoice TOTAL due")
    assert {s.choice.kind for s in plan.segments if s.text == "TOTAL"} == {te.SIMILAR}
    assert plan.substitute_label == "Helvetica"
    assert plan.substituted == ["T", "O", "A", "L"]
    assert "Helvetica, the closest match" in plan.warnings[0]
    _apply(doc, plan)
    assert "Invoice TOTAL due" in doc[0].get_text()


def test_other_subsets_of_the_same_font_in_the_document_come_first(installed_fonts):
    installed_fonts(HELVETICA)          # Georgia itself isn't installed
    doc = _pdf_with(SUPPLEMENTAL / "Georgia.ttf", "Quarterly report",
                    extra_pages=("ZEBRA 2025",))
    plan = _plan(doc, "Quarterly ZEBRA report")
    assert {s.choice.kind for s in plan.segments if s.text.strip() == "ZEBRA"} == \
        {te.DOCUMENT}
    assert plan.warnings == []
    _apply(doc, plan)
    assert "Quarterly ZEBRA report" in doc[0].get_text()


def test_bold_text_never_gets_a_regular_installed_face(installed_fonts):
    installed_fonts(ARIAL)                       # no Arial Bold
    doc = _pdf_with(SUPPLEMENTAL / "Arial Bold.ttf", "Total due")
    plan = _plan(doc, "TOTAL due")
    assert te.INSTALLED not in {s.choice.kind for s in plan.segments}


def test_a_font_the_pdf_doesnt_embed_is_rewritten_in_the_installed_copy(
        installed_fonts):
    installed_fonts(ARIAL, HELVETICA)
    doc = _non_embedded_pdf("ArialMT", SUPPLEMENTAL / "Arial.ttf", "Hello world")
    plan = _plan(doc, "Hello there")
    assert plan.whole_run_reason              # not embedded...
    assert plan.warnings == []                # ...but installed: no warning
    assert plan.same_font_label == "Arial"
    assert {s.choice.kind for s in plan.segments} == {te.INSTALLED}
    _apply(doc, plan)
    assert "Hello there" in doc[0].get_text()


def test_a_font_not_embedded_or_installed_is_matched_on_its_widths(installed_fonts):
    installed_fonts(HELVETICA, TIMES, COURIER_NEW)
    doc = _non_embedded_pdf("ArialMT", SUPPLEMENTAL / "Arial.ttf", "Hello world")
    plan = _plan(doc, "Hello there")
    assert plan.substitute_label == "Helvetica"
    assert "isn't installed on this Mac" in plan.warnings[0]


def test_standard_fonts_are_still_the_last_resort():
    # (No fonts installed: the autouse fixture.)
    doc = _pdf_with(SUPPLEMENTAL / "Arial.ttf", "Invoice total due")
    plan = _plan(doc, "Invoice TOTAL due")
    assert {s.choice.kind for s in plan.segments if s.text == "TOTAL"} == {te.STANDARD}
    assert plan.substitute_label == "Helvetica"


def test_lookalike_search_is_remembered(installed_fonts, monkeypatch):
    installed_fonts(HELVETICA, TIMES)
    doc = _pdf_with(SUPPLEMENTAL / "Arial.ttf", "Invoice total due")
    _plan(doc, "Invoice TOTAL due")
    calls = []
    real = fontmatch.closest_faces
    monkeypatch.setattr(fontmatch, "closest_faces",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    _plan(doc, "Invoice TOTAL due again")
    assert calls == []


# -- Adding text in an installed font ---------------------------------------------------

def test_add_text_in_an_installed_family(installed_fonts, sample_doc):
    installed_fonts(GEORGIA, "Supplemental/Georgia Bold.ttf")
    style = te.TextStyle(family="Georgia", size=14, bold=True)
    assert style.label == "Georgia Bold"
    plan = te.plan_add_text(sample_doc[0], 0, (60, 300), "Added in Georgia", style)
    assert plan.can_apply and plan.face.label == "Georgia Bold"
    te.apply_add_text(sample_doc[0], plan)
    assert "Added in Georgia" in sample_doc[0].get_text()
    assert "Georgia-Bold" in _span_fonts(sample_doc[0])


def test_add_text_family_list_has_installed_fonts(installed_fonts):
    installed_fonts(GEORGIA, HELVETICA)
    assert fontmatch.default_index().families() == ["Georgia", "Helvetica"]


def test_unknown_unembedded_font_is_matched_by_its_recorded_widths(installed_fonts):
    """A name that says nothing about the font: only the widths the PDF
    records can tell Times from Helvetica."""
    installed_fonts(HELVETICA, TIMES)
    doc = _non_embedded_pdf("MysteryFont", SYSTEM_FONTS / "Times.ttc", "Hello world")
    plan = _plan(doc, "Hello there")
    assert plan.substitute_label == "Times"


def test_glyph_comparison_ranks_by_likeness(installed_fonts):
    installed_fonts(ARIAL, HELVETICA, TIMES)
    ref = fontmatch.Reference(font=pymupdf.Font(fontfile=str(SUPPLEMENTAL / "Arial.ttf")),
                              chars="Invoice total due 2024")
    ranked = fontmatch.closest_faces(ref, fontmatch.default_index(), limit=20)
    best = {}
    for distance, face in ranked:          # best face of each family
        best.setdefault(face.family, distance)
    assert list(best) == ["Arial", "Helvetica", "Times"]
    assert best["Arial"] < 0.02 < best["Helvetica"] < 0.25 < best["Times"]


def test_index_rereads_a_font_file_that_changed(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    target = fonts / "Font.ttf"
    target.write_bytes((SUPPLEMENTAL / "Arial.ttf").read_bytes())
    cache = tmp_path / "cache.json"
    assert [f.family for f in fontmatch.FontIndex(
        dirs=(str(fonts),), cache_path=str(cache)).faces()] == ["Arial"]
    target.write_bytes((SUPPLEMENTAL / "Georgia.ttf").read_bytes())
    assert [f.family for f in fontmatch.FontIndex(
        dirs=(str(fonts),), cache_path=str(cache)).faces()] == ["Georgia"]
