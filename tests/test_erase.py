"""Tests for the erase engine (erase.py): words, selections, fills, and
erasing text, scanned pixels and leftover graphics."""

import io

import pymupdf
import pytest
from PIL import Image, ImageDraw

import erase as er
from conftest import LINE_ABOVE, LINE_BELOW, TARGET

# -- A "scanned" page ---------------------------------------------------------
#
# Top: an image (800x300 px shown at 400x150 pt, so 2 px per point) with a
# left-to-right colour gradient and dark ink blocks where two scanned words
# are, plus an invisible OCR text layer over them, as OCR'd scans have.
# Middle: real text. Bottom: a red vector "stamp" inside a page-wide blue
# outline.

SCAN_RECT = pymupdf.Rect(0, 0, 400, 150)
INK_WORD_1 = pymupdf.Rect(40, 64, 120, 82)     # where "SCANNED" is in the scan
INK_WORD_2 = pymupdf.Rect(200, 64, 280, 82)    # where "WORDS" is
STAMP = pymupdf.Rect(300, 232, 340, 258)
OUTLINE = pymupdf.Rect(5, 222, 395, 295)


def _gradient(x):
    return (190 + x // 16, 215, 245 - x // 16)


def _scan_image() -> bytes:
    img = Image.new("RGB", (800, 300))
    d = ImageDraw.Draw(img)
    for x in range(800):
        d.line([(x, 0), (x, 300)], fill=_gradient(x))
    for word in (INK_WORD_1, INK_WORD_2):
        # Five "letters" per word, in scan pixels.
        x0, x1 = word.x0 * 2 + 4, word.x1 * 2 - 4
        step = (x1 - x0) / 5
        for i in range(5):
            d.rectangle((x0 + i * step, word.y0 * 2 + 6,
                         x0 + i * step + step * 0.7, word.y1 * 2 - 4),
                        fill=(25, 25, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def scan_doc():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_image(SCAN_RECT, stream=_scan_image())
    for word, text in ((INK_WORD_1, "SCANNED"), (INK_WORD_2, "WORDS")):
        size = 18
        width = pymupdf.get_text_length(text, "helv", size)
        page.insert_text((word.x0, word.y1 - 4), text, fontname="helv",
                         fontsize=size * (word.width / width), render_mode=3)
    page.insert_text((40, 200), "Visible words stay here", fontname="helv",
                     fontsize=14)
    page.insert_text((40, 214), "Second line just below", fontname="helv",
                     fontsize=14)
    page.draw_rect(STAMP, color=(1, 0, 0), fill=(1, 0.3, 0.3))
    page.draw_rect(OUTLINE, color=(0, 0, 1))
    data = doc.tobytes()
    doc.close()
    doc = pymupdf.open(stream=data, filetype="pdf")
    yield doc
    doc.close()


def _words(doc, page_num=0):
    return er.page_words(doc[page_num])


def _word(doc, text, page_num=0):
    return next(w for w in _words(doc, page_num) if w.text == text)


def _pixel(page, x, y):
    pix = page.get_pixmap(dpi=144, clip=pymupdf.Rect(x - 0.25, y - 0.25,
                                                     x + 0.25, y + 0.25))
    return pix.pixel(0, 0)[:3]


def _is_dark(rgb):
    return sum(rgb) < 200


def _close(a, b, tol=12):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Words and selections
# ---------------------------------------------------------------------------

def test_page_words_in_reading_order_with_ocr_flag(scan_doc):
    words = _words(scan_doc)
    assert [w.text for w in words] == [
        "SCANNED", "WORDS", "Visible", "words", "stay", "here",
        "Second", "line", "just", "below"]
    assert [w.index for w in words] == list(range(len(words)))
    assert {w.text for w in words if w.invisible} == {"SCANNED", "WORDS"}


def test_page_words_skips_vertical_text():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 300), "Sideways", fontname="helv", rotate=90)
    page.insert_text((50, 50), "Upright", fontname="helv")
    assert [w.text for w in er.page_words(page)] == ["Upright"]


def test_word_continues_across_a_mid_word_style_change(sample_doc):
    # "Status: " + "APPROVED" are separate words (space between)...
    texts = [w.text for w in _words(sample_doc)]
    assert "Status:" in texts and "APPROVED" in texts
    # ...but abutting spans with no space form one word.
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Bold", fontname="hebo", fontsize=12)
    w = pymupdf.get_text_length("Bold", "hebo", 12)
    page.insert_text((50 + w, 50), "face", fontname="helv", fontsize=12)
    assert [x.text for x in er.page_words(page)] == ["Boldface"]


def test_word_at(scan_doc):
    words = _words(scan_doc)
    stay = _word(scan_doc, "stay")
    x0, y0, x1, y1 = stay.bbox
    assert er.word_at(words, (x0 + x1) / 2, (y0 + y1) / 2).text == "stay"
    assert er.word_at(words, 390, 180) is None


def test_words_between_is_order_independent(scan_doc):
    words = _words(scan_doc)
    a, b = _word(scan_doc, "words"), _word(scan_doc, "line")
    forward = [w.text for w in er.words_between(words, a, b)]
    backward = [w.text for w in er.words_between(words, b, a)]
    assert forward == backward == ["words", "stay", "here", "Second", "line"]


def test_chars_in_area_uses_character_centres(scan_doc):
    stay = _word(scan_doc, "stay")
    # Cover only the first two letters' centres.
    c0, c1 = stay.chars[0], stay.chars[1]
    area = pymupdf.Rect(c0.bbox[0] - 1, stay.bbox[1] - 1,
                        (c1.bbox[0] + c1.bbox[2]) / 2 + 0.5, stay.bbox[3] + 1)
    assert "".join(c.c for c in er.chars_in_area(_words(scan_doc), area)) == "st"


def test_plan_labels():
    w = er.Word(0, (er.Char("hi", (0, 0, 1, 1), (0, 1), 12),), False)
    assert er.ErasePlan(0, words=[w]).label == "Erase “hi”"
    assert er.ErasePlan(0, area=pymupdf.Rect(0, 0, 5, 5)).label == "Erase area"
    long = er.Word(0, (er.Char("x" * 40, (0, 0, 1, 1), (0, 1), 12),), False)
    assert er.ErasePlan(0, words=[long]).label.endswith("…”")


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------

def _gradient_image(w=120, h=60):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for x in range(w):
        d.line([(x, 0), (x, h)], fill=(40 + x, 100, 220 - x))
    return img


def test_color_fill_paints_the_surrounding_median():
    img = Image.new("RGB", (60, 40), (200, 210, 220))
    ImageDraw.Draw(img).rectangle((20, 10, 40, 30), fill=(0, 0, 0))
    out = er.fill_hole(img, (20, 10, 41, 31), er.FILL_COLOR)
    assert set(out.crop((20, 10, 41, 31)).getdata()) == {(200, 210, 220)}


def test_content_fill_continues_a_gradient():
    img = _gradient_image()
    original = img.copy()
    ImageDraw.Draw(img).rectangle((40, 20, 80, 40), fill=(0, 0, 0))
    out = er.fill_hole(img, (40, 20, 81, 41), er.FILL_CONTENT)
    for x in (45, 60, 75):
        assert _close(out.getpixel((x, 30)), original.getpixel((x, 30)), tol=14)


def test_fill_ignores_neighbouring_ink():
    """Dark ink right next to the hole (another letter) mustn't smear in."""
    img = Image.new("RGB", (80, 40), (235, 235, 230))
    d = ImageDraw.Draw(img)
    d.rectangle((30, 10, 50, 30), fill=(10, 10, 10))   # the word to erase
    d.rectangle((52, 10, 58, 30), fill=(10, 10, 10))   # a neighbouring letter
    out = er.fill_hole(img, (30, 10, 51, 31), er.FILL_CONTENT)
    assert min(sum(px) for px in out.crop((30, 10, 51, 31)).getdata()) > 600
    # ...and the neighbouring letter itself is left exactly as it was.
    assert list(out.crop((52, 10, 59, 31)).getdata()) == \
        list(img.crop((52, 10, 59, 31)).getdata())


def _noisy_paper(w=160, h=80, sigma=8, seed=3):
    import random
    rnd = random.Random(seed)
    img = Image.new("L", (w, h))
    img.putdata([max(0, min(255, int(rnd.gauss(225, sigma)))) for _ in range(w * h)])
    return img.convert("RGB")


def _std(img):
    from PIL import ImageStat
    return ImageStat.Stat(img.convert("L")).stddev[0]


def test_content_fill_keeps_paper_grain():
    """On a grainy scan, a perfectly smooth fill would stand out: the fill
    must carry similar texture."""
    img = _noisy_paper()
    ImageDraw.Draw(img).rectangle((50, 30, 110, 50), fill=(20, 20, 20))
    out = er.fill_hole(img, (50, 30, 111, 51), er.FILL_CONTENT)
    filled = out.crop((50, 30, 111, 51))
    around = img.crop((50, 5, 111, 25))
    assert _std(filled) > 0.5 * _std(around)
    assert min(sum(px) for px in filled.getdata()) > 450   # no ink left


def test_content_fill_stays_clean_on_clean_backgrounds():
    img = Image.new("RGB", (80, 40), (250, 250, 250))
    ImageDraw.Draw(img).rectangle((20, 10, 60, 30), fill=(0, 0, 0))
    out = er.fill_hole(img, (20, 10, 61, 31), er.FILL_CONTENT)
    assert set(out.crop((20, 10, 61, 31)).getdata()) == {(250, 250, 250)}


def test_fill_only_changes_the_hole():
    img = _gradient_image()
    out = er.fill_hole(img, (40, 20, 81, 41), er.FILL_CONTENT)
    for xy in ((0, 0), (39, 30), (81, 30), (60, 19), (60, 41), (119, 59)):
        assert out.getpixel(xy) == img.getpixel(xy)


def test_fill_keeps_grayscale_images_grayscale():
    img = Image.new("L", (40, 40), 230)
    out = er.fill_hole(img, (10, 10, 30, 30))
    assert out.mode == "L"


def test_unknown_fill_mode_is_rejected():
    with pytest.raises(ValueError):
        er.fill_hole(Image.new("RGB", (10, 10)), (2, 2, 5, 5), "sparkles")


def test_residual_measures_changed_pixels():
    a = Image.new("RGB", (10, 10), (255, 255, 255))
    b = a.copy()
    assert er._residual(a, b, (0, 0, 10, 10)) == 0
    ImageDraw.Draw(b).rectangle((0, 0, 4, 9), fill=(0, 0, 0))
    assert er._residual(a, b, (0, 0, 10, 10)) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Erasing real text
# ---------------------------------------------------------------------------

def test_erasing_words_removes_exactly_those_words(scan_doc):
    page = scan_doc[0]
    images_before = [(i["xref"], i["bbox"]) for i in page.get_image_info(xrefs=True)]
    drawings_before = len(page.get_drawings())
    words = _words(scan_doc)

    done = er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=er.words_between(
        words, _word(scan_doc, "words"), _word(scan_doc, "stay"))))

    assert done == {"chars": 9, "images": 0, "patches": 0}
    texts = [w.text for w in _words(scan_doc)]
    assert "words" not in texts and "stay" not in texts
    assert texts.count("Visible") == 1 and "here" in texts
    assert ["Second", "line", "just", "below"] == texts[-4:]
    # (Removing text rewrites the content stream, which may rename the
    # image's resource, so compare what's drawn where, not names.)
    assert [(i["xref"], i["bbox"]) for i in
            scan_doc[0].get_image_info(xrefs=True)] == images_before
    assert len(scan_doc[0].get_drawings()) == drawings_before


def test_erased_text_reveals_the_background_without_painting(scan_doc):
    stay = _word(scan_doc, "stay")
    er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[stay]))
    x0, y0, x1, y1 = stay.bbox
    pix = scan_doc[0].get_pixmap(dpi=144, clip=stay.rect)
    assert min(pix.samples) > 240  # plain white page shows through


def test_erasing_a_line_keeps_tightly_spaced_neighbours(sample_doc):
    """...and the green line drawn straight through it (graphics are never
    removed by erasing text)."""
    drawings_before = [(d["rect"], d.get("color")) for d in sample_doc[0].get_drawings()]
    words = er.page_words(sample_doc[0])
    line = [w for w in words if w.text in TARGET.split()
            and abs(w.chars[0].origin[1] - 162) < 0.5]
    er.apply_erase(sample_doc, 0, er.ErasePlan(0, words=line))
    text = sample_doc[0].get_text()
    assert TARGET not in text
    assert LINE_ABOVE in text and LINE_BELOW in text
    assert [(d["rect"], d.get("color")) for d in
            sample_doc[0].get_drawings()] == drawings_before


def test_erased_text_is_gone_from_the_saved_file(scan_doc):
    er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "Visible")]))
    copy = pymupdf.open(stream=scan_doc.tobytes(), filetype="pdf")
    saved = pymupdf.open(stream=copy.tobytes(garbage=3, deflate=True), filetype="pdf")
    raw = b"".join((saved.xref_stream(x) or b"") for x in range(1, saved.xref_length())
                   if saved.xref_is_stream(x)).lower()
    assert b"visible" not in raw
    assert b"Visible".hex().encode() not in raw


def test_collateral_damage_is_detected(sample_doc, monkeypatch):
    monkeypatch.setattr(er, "BAND_TOP", 1.6)  # band reaching the line above
    target_words = [w for w in er.page_words(sample_doc[0])
                    if abs(w.chars[0].origin[1] - 162) < 0.5]
    with pytest.raises(er.EditError, match="outside the selection"):
        er.apply_erase(sample_doc, 0, er.ErasePlan(0, words=target_words))


def test_stale_selection_is_rejected(scan_doc):
    stay = _word(scan_doc, "stay")
    er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[stay]))
    with pytest.raises(er.EditError, match="changed"):
        er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[stay]))


def test_rotated_pages_are_refused(scan_doc):
    scan_doc[0].set_rotation(90)
    with pytest.raises(er.EditError, match="rotated"):
        er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "stay")]))


# ---------------------------------------------------------------------------
# Erasing scanned (image) content
# ---------------------------------------------------------------------------

def test_erasing_an_ocr_word_fills_the_scan_pixels(scan_doc):
    # Hold a page reference across the erase, as the app might.
    page = scan_doc[0]
    centre = (INK_WORD_1.x0 + 20, (INK_WORD_1.y0 + INK_WORD_1.y1) / 2)
    other = (INK_WORD_2.x0 + 20, (INK_WORD_2.y0 + INK_WORD_2.y1) / 2)
    assert _is_dark(_pixel(page, *centre)) and _is_dark(_pixel(page, *other))

    done = er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "SCANNED")]))

    assert done["images"] == 1 and done["patches"] == 0
    filled = _pixel(page, *centre)   # the held page shows the new pixels
    assert _pixel(scan_doc[0], *centre) == filled
    assert not _is_dark(filled)
    # It blends with the gradient at that spot (scan px = 2 x page pt).
    assert _close(filled, _gradient(int(centre[0] * 2)), tol=20)
    # The other scanned word is untouched, and the OCR word is gone.
    assert _is_dark(_pixel(page, *other))
    assert "SCANNED" not in page.get_text() and "WORDS" in page.get_text()


def test_scan_is_edited_in_place_not_covered(scan_doc):
    er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "SCANNED")]))
    page = scan_doc[0]
    infos = page.get_image_info(xrefs=True)
    assert len(infos) == 1                       # no patch image on top
    assert pymupdf.Rect(infos[0]["bbox"]) == SCAN_RECT


def test_background_colour_fill_on_a_scan(scan_doc):
    er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "SCANNED")],
                                             fill=er.FILL_COLOR))
    page = scan_doc[0]
    a = _pixel(page, INK_WORD_1.x0 + 10, 72)
    b = _pixel(page, INK_WORD_1.x1 - 10, 72)
    assert not _is_dark(a)
    assert a == b  # one flat colour


def test_rectangle_over_a_scan_fills_it(scan_doc):
    done = er.apply_erase(scan_doc, 0, er.ErasePlan(0, area=pymupdf.Rect(195, 60, 285, 86)))
    page = scan_doc[0]
    assert done["images"] == 1
    assert not _is_dark(_pixel(page, INK_WORD_2.x0 + 20, 72))
    assert "WORDS" not in page.get_text()       # its OCR text went too
    assert "SCANNED" in page.get_text()


def test_shared_image_is_covered_not_edited():
    """An image shown twice can't be edited in place without changing both
    copies, so the erased spot gets a patch instead."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=320)
    data = _scan_image()
    xref = page.insert_image(pymupdf.Rect(0, 0, 400, 150), stream=data)
    page.insert_image(pymupdf.Rect(0, 160, 400, 310), xref=xref)
    doc = pymupdf.open(stream=doc.tobytes(), filetype="pdf")

    done = er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(195, 60, 285, 86)))

    page = doc[0]
    assert done == {"chars": 0, "images": 0, "patches": 1}
    assert not _is_dark(_pixel(page, INK_WORD_2.x0 + 20, 72))      # covered
    assert _is_dark(_pixel(page, INK_WORD_2.x0 + 20, 160 + 72))    # other copy intact


def test_image_with_transparency_is_covered_not_edited():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=150)
    img = Image.open(io.BytesIO(_scan_image())).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    page.insert_image(pymupdf.Rect(0, 0, 400, 150), stream=buf.getvalue())
    doc = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    xref = doc[0].get_images()[0][0]
    assert doc.xref_get_key(xref, "SMask")[0] != "null"

    done = er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(195, 60, 285, 86)))
    assert done["images"] == 0 and done["patches"] == 1
    assert not _is_dark(_pixel(doc[0], INK_WORD_2.x0 + 20, 72))


# ---------------------------------------------------------------------------
# Erasing an area with graphics
# ---------------------------------------------------------------------------

def test_rectangle_covers_a_vector_stamp_but_not_the_outline(scan_doc):
    page = scan_doc[0]
    assert _close(_pixel(page, 320, 245), (255, 77, 77), tol=30)

    done = er.apply_erase(scan_doc, 0, er.ErasePlan(0, area=STAMP + (-4, -4, 4, 4)))

    page = scan_doc[0]
    assert done == {"chars": 0, "images": 0, "patches": 1}
    assert _close(_pixel(page, 320, 245), (255, 255, 255), tol=6)
    # The big outline the stamp sat inside is still drawn.
    assert _pixel(page, OUTLINE.x0, 260)[2] > 150
    assert len(page.get_drawings()) == 2        # nothing deleted, only covered


def test_rectangle_over_plain_text_needs_no_patch(scan_doc):
    stay = _word(scan_doc, "stay")
    done = er.apply_erase(scan_doc, 0, er.ErasePlan(0, area=stay.rect + (-2, -2, 2, 2)))
    assert done == {"chars": 4, "images": 0, "patches": 0}
    assert len(scan_doc[0].get_image_info()) == 1   # just the scan; no patch


def test_rectangle_takes_only_characters_centred_inside(scan_doc):
    here = _word(scan_doc, "here")
    c = here.chars[1]  # 'e'
    area = pymupdf.Rect(here.bbox[0] - 2, here.bbox[1] - 1,
                        (c.bbox[0] + c.bbox[2]) / 2 + 0.5, here.bbox[3] + 1)
    er.apply_erase(scan_doc, 0, er.ErasePlan(0, area=area))
    assert "re" in [w.text for w in _words(scan_doc)]


def test_erasing_empty_space_is_an_error(scan_doc):
    with pytest.raises(er.EditError, match="nothing to erase"):
        er.apply_erase(scan_doc, 0, er.ErasePlan(0, area=pymupdf.Rect(100, 260, 200, 280)))


# ---------------------------------------------------------------------------
# File size: edited images are stored like the originals
# ---------------------------------------------------------------------------

def _saved_size(doc):
    copy = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    return len(copy.tobytes(garbage=3, deflate=True))


def _single_image_doc(img: Image.Image, fmt: str, **save_args):
    buf = io.BytesIO()
    img.save(buf, fmt, **save_args)
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_image(page.rect, stream=buf.getvalue())
    return pymupdf.open(stream=doc.tobytes(garbage=3, deflate=True), filetype="pdf")


def _image_key(doc, key):
    xref = doc[0].get_image_info(xrefs=True)[0]["xref"]
    return doc.xref_get_key(xref, key)[1]


def test_jpeg_scan_stays_jpeg_and_small():
    img = _noisy_paper(800, 600, sigma=6)
    ImageDraw.Draw(img).rectangle((200, 200, 400, 240), fill=(20, 20, 20))
    doc = _single_image_doc(img, "JPEG", quality=85)
    before = _saved_size(doc)

    er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(98, 98, 202, 122)))

    assert "DCTDecode" in _image_key(doc, "Filter")
    assert _saved_size(doc) < 1.5 * before
    assert not _is_dark(_pixel(doc[0], 150, 110))


def test_one_bit_scan_stays_one_bit():
    img = Image.new("1", (800, 600), 1)
    ImageDraw.Draw(img).rectangle((200, 200, 400, 240), fill=0)
    doc = _single_image_doc(img, "PNG")
    assert _image_key(doc, "BitsPerComponent") == "1"

    er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(98, 98, 202, 122)))

    assert _image_key(doc, "BitsPerComponent") == "1"
    assert _pixel(doc[0], 150, 110) == (255, 255, 255)


def test_inverted_one_bit_scan_keeps_its_colours():
    """Black-and-white scans often store inverted bits (/Decode [1 0]).
    The edited image must look the same everywhere except the erased spot,
    not come out colour-inverted."""
    img = Image.new("1", (800, 600), 1)
    ImageDraw.Draw(img).rectangle((200, 200, 400, 240), fill=0)
    doc = _single_image_doc(img, "PNG")
    xref = doc[0].get_image_info(xrefs=True)[0]["xref"]
    doc.xref_set_key(xref, "Decode", "[1 0]")       # now drawn inverted
    pymupdf.TOOLS.store_shrink(100)
    background = _pixel(doc[0], 350, 250)
    ink = _pixel(doc[0], 150, 110)
    assert background != ink

    er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(98, 98, 202, 122)))

    assert _pixel(doc[0], 350, 250) == background   # rest of the page unchanged
    assert _pixel(doc[0], 150, 110) == background   # erased spot = background


# ---------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------

def test_erase_refuses_pages_with_pending_redactions(scan_doc):
    page = scan_doc[0]
    page.add_redact_annot(pymupdf.Rect(300, 260, 350, 280), fill=(1, 0, 0))
    with pytest.raises(er.EditError, match="redaction marks"):
        er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "stay")]))
    assert [a.type[1] for a in scan_doc[0].annots()] == ["Redact"]   # not applied


def _freetext_doc():
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text((20, 40), "Body text", fontname="helv", fontsize=12)
    page.add_freetext_annot(pymupdf.Rect(100, 100, 250, 130), "APPROVED", fontsize=14)
    return pymupdf.open(stream=doc.tobytes(), filetype="pdf")


def test_erase_refuses_comment_box_text():
    doc = _freetext_doc()
    word = _word(doc, "APPROVED")
    with pytest.raises(er.EditError, match="comment box"):
        er.apply_erase(doc, 0, er.ErasePlan(0, words=[word]))
    with pytest.raises(er.EditError, match="comment box"):
        er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(90, 90, 260, 140)))
    assert [a.type[1] for a in doc[0].annots()] == ["FreeText"]


def test_erase_detects_a_lost_annotation(monkeypatch):
    doc = _freetext_doc()
    monkeypatch.setattr(er, "annotation_boxes", lambda page: [])
    with pytest.raises(er.EditError, match="comment, stamp or form field"):
        er.apply_erase(doc, 0, er.ErasePlan(0, words=[_word(doc, "APPROVED")]))


def test_rectangle_with_vertical_text_is_refused():
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text((100, 200), "Vertical", fontname="helv", fontsize=14, rotate=90)
    page.insert_text((150, 150), "Horiz", fontname="helv", fontsize=14)
    doc = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    with pytest.raises(er.EditError, match="vertical or rotated"):
        er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(80, 80, 220, 220)))
    assert "Vertical" in doc[0].get_text() and "Horiz" in doc[0].get_text()


def test_empty_rectangle_is_refused(scan_doc):
    with pytest.raises(er.EditError, match="Nothing to erase"):
        er.apply_erase(scan_doc, 0, er.ErasePlan(0, area=pymupdf.Rect(50, 50, 50, 50)))


def test_image_shown_in_an_annotation_elsewhere_is_not_edited():
    """An image drawn on page 1 and inside an annotation's appearance on
    page 2: editing it in place would change page 2 too."""
    doc = pymupdf.open()
    doc.new_page(width=200, height=200)
    doc.new_page(width=200, height=200)   # (creating it invalidates page objects)
    p1 = doc[0]
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32), False)
    pix.set_rect(pix.irect, (30, 120, 200))
    pix.set_rect(pymupdf.IRect(5, 5, 11, 11), (255, 0, 0))
    xref = p1.insert_image(pymupdf.Rect(20, 20, 180, 180), pixmap=pix)
    p2 = doc[1]
    annot = p2.add_rect_annot(pymupdf.Rect(20, 20, 180, 180))
    ap = int(doc.xref_get_key(annot.xref, "AP/N")[1].split()[0])
    doc.update_stream(ap, b"q 160 0 0 160 0 0 cm /Im1 Do Q")
    doc.xref_set_key(ap, "Resources", f"<</XObject <</Im1 {xref} 0 R>>>>")
    doc = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    page2_before = doc[1].get_pixmap(dpi=72).samples

    done = er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(40, 40, 80, 80)))

    assert done["images"] == 0 and done["patches"] == 1
    assert doc[1].get_pixmap(dpi=72).samples == page2_before


# -- Colour spaces ------------------------------------------------------------

ROMM = "/System/Library/ColorSync/Profiles/ROMM RGB.icc"
W = H = 64


def _image_with_colorspace(colorspace, samples, icc=None):
    """A page with one 64x64 image (160pt square) whose samples are written
    raw under the given /ColorSpace."""
    import zlib
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, W, H), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    xref = page.insert_image(pymupdf.Rect(20, 20, 180, 180), pixmap=pix)
    if icc is not None:
        icc_xref = doc.get_new_xref()
        doc.update_object(icc_xref, "<</N 3>>")
        doc.update_stream(icc_xref, icc)
        colorspace = colorspace.replace("ICC_REF", f"{icc_xref} 0 R")
    doc.update_stream(xref, zlib.compress(samples), compress=False)
    for key in ("SMask", "DecodeParms", "Decode"):
        doc.xref_set_key(xref, key, "null")
    doc.xref_set_key(xref, "Filter", "/FlateDecode")
    doc.xref_set_key(xref, "ColorSpace", colorspace)
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    return pymupdf.open(stream=doc.tobytes(), filetype="pdf")


def _two_tone(pixel_a, pixel_b):
    """Samples: left half pixel_a (with a block of pixel_b to erase)."""
    rows = []
    for y in range(H):
        row = b""
        for x in range(W):
            in_block = 18 <= x < 26 and 18 <= y < 26
            row += pixel_b if in_block else pixel_a
        rows.append(row)
    return b"".join(rows)


COLORSPACE_CASES = {
    "DeviceGray": ("/DeviceGray", bytes([170]), bytes([20])),
    "DeviceRGB": ("/DeviceRGB", bytes([200, 60, 60]), bytes([0, 0, 0])),
    "CMYK": ("/DeviceCMYK", bytes([0, 200, 200, 0]), bytes([0, 0, 0, 255])),
    "Separation": ("[/Separation /Spot /DeviceCMYK <</FunctionType 2 /Domain [0 1] "
                   "/C0 [0 0 0 0] /C1 [0 0.9 0.9 0] /N 1>>]", bytes([204]), bytes([10])),
    "Lab": ("[/Lab <</WhitePoint [0.9505 1 1.089]>>]", bytes([128, 200, 128]),
            bytes([20, 128, 128])),
    "Indexed": ("[/Indexed /DeviceRGB 1 <00FF00 0000FF>]", bytes([1]), bytes([0])),
}


@pytest.mark.parametrize("name", list(COLORSPACE_CASES) + ["ICC ROMM"])
def test_erasing_keeps_the_image_colours(name):
    """Pixels outside the erased spot must look exactly as before, whatever
    the image's colour space (the bug: samples re-tagged as DeviceRGB/Gray
    recoloured spot-colour, Lab and ICC images)."""
    if name == "ICC ROMM":
        import os
        if not os.path.exists(ROMM):
            pytest.skip("macOS ROMM RGB profile not available")
        doc = _image_with_colorspace("[/ICCBased ICC_REF]",
                                     _two_tone(bytes([200, 60, 60]), bytes([0, 0, 0])),
                                     icc=open(ROMM, "rb").read())
    else:
        colorspace, a, b = COLORSPACE_CASES[name]
        doc = _image_with_colorspace(colorspace, _two_tone(a, b))
    outside = (150, 150)
    block = (20 + 22 * 2.5, 20 + 22 * 2.5)   # centre of the block, in page pts
    before = _pixel(doc[0], *outside)
    block_before = _pixel(doc[0], *block)
    assert block_before != before

    done = er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(60, 60, 90, 90)))

    assert done["images"] + done["patches"] >= 1
    assert _close(_pixel(doc[0], *outside), before, tol=3)
    assert _close(_pixel(doc[0], *block), before, tol=12)   # block filled to match


def test_icc_and_spot_images_keep_their_colour_space_entry():
    colorspace, a, b = COLORSPACE_CASES["Separation"]
    doc = _image_with_colorspace(colorspace, _two_tone(a, b))
    er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(60, 60, 90, 90)))
    assert "/Separation" in _image_key(doc, "ColorSpace")


def test_cmyk_images_are_converted_and_tagged_rgb():
    colorspace, a, b = COLORSPACE_CASES["CMYK"]
    doc = _image_with_colorspace(colorspace, _two_tone(a, b))
    done = er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(60, 60, 90, 90)))
    assert done["images"] == 1
    assert _image_key(doc, "ColorSpace") == "/DeviceRGB"


def test_jpeg_keeps_its_chroma_subsampling():
    img = _noisy_paper(400, 300, sigma=6)
    ImageDraw.Draw(img).rectangle((100, 100, 200, 120), fill=(20, 20, 20))
    doc = _single_image_doc(img, "JPEG", quality=85, subsampling=0)   # 4:4:4
    er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(98, 98, 202, 122)))
    from PIL import JpegImagePlugin
    xref = doc[0].get_image_info(xrefs=True)[0]["xref"]
    stored = Image.open(io.BytesIO(doc.xref_stream_raw(xref)))
    assert JpegImagePlugin.get_sampling(stored) == 0


def test_jpeg2000_scan_becomes_compact_jpeg():
    img = _noisy_paper(800, 600, sigma=6)
    ImageDraw.Draw(img).rectangle((200, 200, 400, 240), fill=(20, 20, 20))
    try:
        doc = _single_image_doc(img, "JPEG2000", quality_mode="rates",
                                quality_layers=[40])
    except (OSError, KeyError):
        pytest.skip("Pillow was built without JPEG 2000 support")
    assert "JPXDecode" in _image_key(doc, "Filter")
    before = _saved_size(doc)

    er.apply_erase(doc, 0, er.ErasePlan(0, area=pymupdf.Rect(98, 98, 202, 122)))

    assert "DCTDecode" in _image_key(doc, "Filter")
    assert _saved_size(doc) < 3 * before
    assert not _is_dark(_pixel(doc[0], 150, 110))


def test_orphaned_objects_do_not_count_as_sharing(scan_doc):
    """Removing text rewrites the page's resources and orphans the old
    resource dictionary (which still names the image). That mustn't make
    the scan look shared and get it patched instead of edited."""
    er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "Visible")]))
    done = er.apply_erase(scan_doc, 0, er.ErasePlan(0, words=[_word(scan_doc, "SCANNED")]))
    assert done["images"] == 1 and done["patches"] == 0
