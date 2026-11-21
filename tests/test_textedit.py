"""Tests for the text-editing engine (textedit.py): finding runs, picking
fonts, planning edits, and applying them without disturbing anything else."""

from collections import Counter

import pymupdf
import pytest

import textedit as te
from conftest import (
    BLUE, BOLD_PART, LINE_ABOVE, LINE_BELOW, REGULAR_PART, SECOND_PAGE_TEXT,
    TARGET, TITLE,
)


def _run(doc, text, page_num=0):
    runs = te.page_runs(doc[page_num], page_num)
    matches = [r for r in runs if r.text.strip() == text.strip()]
    assert len(matches) == 1, [r.text for r in runs]
    return matches[0]


def _edit(doc, text, new_text, page_num=0):
    run = _run(doc, text, page_num)
    page = doc[page_num]
    plan = te.plan_edit(doc, page, run, new_text)
    te.apply_edit(page, plan)
    return plan


def _texts(doc, page_num=0):
    return [r.text.strip() for r in te.page_runs(doc[page_num], page_num)]


# ---------------------------------------------------------------------------
# Finding runs
# ---------------------------------------------------------------------------

def test_page_runs_finds_every_line_with_style(sample_doc):
    runs = {r.text.strip(): r for r in te.page_runs(sample_doc[0], 0)}

    assert set(runs) == {TITLE, LINE_ABOVE, TARGET, LINE_BELOW,
                         REGULAR_PART.strip(), BOLD_PART}
    title = runs[TITLE]
    assert title.size == pytest.approx(20)
    assert title.color == pytest.approx(BLUE, abs=0.01)
    assert title.origin == pytest.approx((50, 80))
    assert title.font == "NimbusRoman-Regular"
    assert runs[TARGET].font == "Helvetica"
    assert runs[TARGET].origin == pytest.approx((50, 162))
    assert all(r.is_horizontal for r in runs.values())


def test_differently_styled_neighbours_stay_separate_runs(sample_doc):
    runs = {r.text.strip(): r for r in te.page_runs(sample_doc[0], 0)}
    assert runs[BOLD_PART].bold
    assert not runs[REGULAR_PART.strip()].bold


def test_run_at_hits_and_misses(sample_doc):
    runs = te.page_runs(sample_doc[0], 0)
    assert te.run_at(runs, 60, 160).text == TARGET
    assert te.run_at(runs, 60, 148).text == LINE_ABOVE
    assert te.run_at(runs, 300, 300) is None
    # Just outside the right edge, within tolerance:
    target = _run(sample_doc, TARGET)
    assert te.run_at(runs, target.bbox[2] + 0.5, 160) == target
    assert te.run_at(runs, target.bbox[2] + 5, 160) is None


def test_run_at_prefers_the_smallest_overlapping_run():
    big = te.TextRun(0, "big", "Helvetica", 12, 0, (0, 0, 0), (0, 20),
                     (0, 0, 100, 40), (1, 0))
    small = te.TextRun(0, "small", "Helvetica", 12, 0, (0, 0, 0), (10, 20),
                       (10, 10, 20, 20), (1, 0))
    assert te.run_at([big, small], 15, 15) is small


@pytest.mark.parametrize("name, expected", [
    ("ABCDEF+Calibri-Bold", "calibribold"),
    ("Nimbus Roman Regular", "nimbusromanregular"),
    ("NimbusRoman-Regular", "nimbusromanregular"),
    ("Times New Roman,Bold", "timesnewromanbold"),
    ("abcdef+Lower", "abcdeflower"),  # not a subset prefix: lowercase
])
def test_normalize_font_name(name, expected):
    assert te.normalize_font_name(name) == expected


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

def test_embedded_subset_font_is_reused(sample_doc):
    title = _run(sample_doc, TITLE)
    choice, reason = te.resolve_original_font(sample_doc, sample_doc[0], title)
    assert reason == ""
    assert choice.original and choice.check_ink
    assert choice.can_draw("Q")                 # used in the title
    assert not choice.can_draw("Z")             # subset left it out


def test_subset_with_blank_glyphs_is_detected(sample_doc):
    """PyMuPDF's subsetter keeps the character map but empties unused
    glyphs; has_glyph() alone would say 'z' is available."""
    title = _run(sample_doc, TITLE)
    choice, _ = te.resolve_original_font(sample_doc, sample_doc[0], title)
    assert choice.font.has_glyph(ord("z"))      # the misleading part
    assert not choice.can_draw("z")             # ...caught by the ink check


def test_coretext_subset_missing_chars_are_detected(coretext_doc):
    """CoreText subsets drop unused characters from the cmap instead."""
    total = _run(coretext_doc, "Total due: 1,250.00 USD")
    choice, reason = te.resolve_original_font(coretext_doc, coretext_doc[0], total)
    assert reason == "" and choice.original
    assert choice.can_draw("T") and choice.can_draw("5")
    assert not choice.can_draw("E")
    assert not choice.font.has_glyph(ord("E"))


def test_space_that_would_extract_as_nbsp_is_rejected(coretext_doc):
    """This font maps space and no-break space to one glyph, and written
    text would extract as U+00A0. The round-trip check catches that."""
    total = _run(coretext_doc, "Total due: 1,250.00 USD")
    choice, _ = te.resolve_original_font(coretext_doc, coretext_doc[0], total)
    assert choice.font.has_glyph(ord(" "))
    assert not choice.can_draw(" ")
    assert te.fallback_font(total).can_draw(" ")


def test_reediting_finds_the_font_the_last_edit_embedded(sample_doc):
    """The written font is declared "Helvetica" in the PDF, but extracts
    as its program name "NimbusSans-Regular"; it must still be found."""
    _edit(sample_doc, TARGET, "Edited once")
    run = _run(sample_doc, "Edited once")
    assert run.font == "NimbusSans-Regular"

    choice, reason = te.resolve_original_font(sample_doc, sample_doc[0], run)
    assert reason == "" and choice.original

    plan = te.plan_edit(sample_doc, sample_doc[0], run, "Edited twice")
    assert plan.warnings == []
    te.apply_edit(sample_doc[0], plan)
    assert "Edited twice" in _texts(sample_doc)


def test_standard_14_font_is_used_as_is(sample_doc):
    target = _run(sample_doc, TARGET)
    choice, reason = te.resolve_original_font(sample_doc, sample_doc[0], target)
    assert reason == ""
    assert choice.original and not choice.check_ink
    assert choice.key == "base14:helv"


def test_non_embedded_non_standard_font_is_reported(sample_doc):
    page = sample_doc[0]
    helv = next(f[0] for f in page.get_fonts(full=True) if f[3] == "Helvetica")
    sample_doc.xref_set_key(helv, "BaseFont", "/Garamond")
    doc = pymupdf.open(stream=sample_doc.tobytes(), filetype="pdf")
    target = _run(doc, TARGET)
    assert target.font == "Garamond"

    choice, reason = te.resolve_original_font(doc, doc[0], target)

    assert choice is None
    assert "isn't embedded" in reason


@pytest.mark.parametrize("font, flags, expected", [
    ("Helvetica", 0, "helv"),
    ("Arial-BoldMT", 16, "hebo"),
    ("Arial-ItalicMT", 2, "heit"),
    ("Arial-BoldItalicMT", 18, "hebi"),
    ("TimesNewRomanPSMT", 4, "tiro"),
    ("Georgia-Bold", 20, "tibo"),
    ("Courier", 8, "cour"),
    ("Menlo-Bold", 8 | 16, "cobo"),
    ("SourceSansPro-Regular", 4, "helv"),  # "sans" beats the serif flag
])
def test_fallback_font_matches_style(font, flags, expected):
    run = te.TextRun(0, "x", font, 12, flags, (0, 0, 0), (0, 0), (0, 0, 1, 1),
                     (1, 0))
    assert te.fallback_font_code(run) == expected


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_unchanged_text_is_a_noop(sample_doc):
    target = _run(sample_doc, TARGET)
    plan = te.plan_edit(sample_doc, sample_doc[0], target, target.text)
    assert plan.is_noop and plan.can_apply and not plan.warnings


def test_plan_with_available_glyphs_has_no_warnings(sample_doc):
    title = _run(sample_doc, TITLE)
    plan = te.plan_edit(sample_doc, sample_doc[0], title, "Quarterly Rot")
    assert plan.can_apply
    assert plan.warnings == []
    assert len(plan.segments) == 1 and plan.segments[0].choice.original


def test_plan_lists_characters_needing_a_substitute(sample_doc):
    title = _run(sample_doc, TITLE)
    plan = te.plan_edit(sample_doc, sample_doc[0], title, "Quiz Report")

    assert plan.can_apply
    assert plan.substituted == ["i", "z"]
    assert plan.substitute_label == "Times"
    assert len(plan.warnings) == 1
    assert "i z" in plan.warnings[0]
    assert [s.choice.original for s in plan.segments] == [True, False, True]


def test_plan_warns_when_whole_run_needs_a_substitute(sample_doc):
    page = sample_doc[0]
    helv = next(f[0] for f in page.get_fonts(full=True) if f[3] == "Helvetica")
    sample_doc.xref_set_key(helv, "BaseFont", "/Garamond")
    doc = pymupdf.open(stream=sample_doc.tobytes(), filetype="pdf")
    target = _run(doc, TARGET)

    plan = te.plan_edit(doc, doc[0], target, "Changed")

    assert plan.can_apply
    assert plan.whole_run_reason
    # Garamond is a serif face, so the closest standard font is Times.
    assert "rewritten in Times" in plan.warnings[0]
    # Deleting needs no font, so no warning.
    assert te.plan_edit(doc, doc[0], target, "").warnings == []


@pytest.mark.parametrize("new_text, message", [
    ("漢字", "None of the available fonts"),
    ("two\nlines", "Line breaks"),
    ("tab\there", "Line breaks"),
])
def test_plan_rejects_what_cannot_be_written(sample_doc, new_text, message):
    target = _run(sample_doc, TARGET)
    plan = te.plan_edit(sample_doc, sample_doc[0], target, new_text)
    assert not plan.can_apply
    assert message in plan.errors[0]
    with pytest.raises(te.EditError):
        te.apply_edit(sample_doc[0], plan)


def test_plan_rejects_rotated_pages(sample_doc):
    sample_doc[0].set_rotation(90)
    target = _run(sample_doc, TARGET)
    plan = te.plan_edit(sample_doc, sample_doc[0], target, "x")
    assert any("rotated pages" in e for e in plan.errors)


def test_plan_rejects_vertical_text():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 300), "Sideways", fontname="helv", fontsize=12,
                     rotate=90)
    run = te.page_runs(page, 0)[0]
    assert not run.is_horizontal
    plan = te.plan_edit(doc, page, run, "x")
    assert any("horizontal" in e for e in plan.errors)


def test_plan_rejects_text_without_a_character_mapping(sample_doc):
    run = _run(sample_doc, TARGET)
    unmapped = te.TextRun(**{**run.__dict__, "text": "ab�cd"})
    plan = te.plan_edit(sample_doc, sample_doc[0], unmapped, "abcd")
    assert any("character mapping" in e for e in plan.errors)


def test_plan_refuses_pages_with_pending_redactions(sample_doc):
    sample_doc[0].add_redact_annot(pymupdf.Rect(300, 400, 350, 420))
    target = _run(sample_doc, TARGET)
    plan = te.plan_edit(sample_doc, sample_doc[0], target, "x")
    assert any("redaction" in e for e in plan.errors)


def test_plan_warns_about_overlapping_the_next_run(sample_doc):
    regular = _run(sample_doc, REGULAR_PART)
    page = sample_doc[0]

    wider = te.plan_edit(sample_doc, page, regular, "Current status: ")
    assert wider.overlaps == [BOLD_PART]
    assert "overlap" in wider.warnings[0]

    narrower = te.plan_edit(sample_doc, page, regular, "State: ")
    assert narrower.overlaps == []
    assert narrower.warnings == []


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("small_heights", [True, False])
def test_edit_replaces_only_the_target(sample_doc, small_heights):
    pymupdf.TOOLS.set_small_glyph_heights(small_heights)
    before = _texts(sample_doc)

    _edit(sample_doc, TARGET, "Edited line")

    after = _texts(sample_doc)
    assert "Edited line" in after and TARGET not in after
    assert Counter(after) == Counter(before) - Counter([TARGET]) + Counter(["Edited line"])
    assert _texts(sample_doc, 1) == [SECOND_PAGE_TEXT]


def test_edit_keeps_position_size_colour_and_font(sample_doc):
    title = _run(sample_doc, TITLE)
    _edit(sample_doc, TITLE, "Quarterly Rot")
    new = _run(sample_doc, "Quarterly Rot")

    assert new.origin == pytest.approx(title.origin, abs=0.01)
    assert new.size == pytest.approx(title.size)
    assert new.color == pytest.approx(title.color, abs=0.01)
    assert new.font == title.font


def test_edit_leaves_graphics_and_images_alone(sample_doc):
    page = sample_doc[0]
    green_before = _green_pixels(page)
    assert green_before > 20  # probe sanity
    drawings_before = [(d["rect"], d.get("color")) for d in page.get_drawings()]
    images_before = page.get_image_info(xrefs=True)

    _edit(sample_doc, TARGET, "Edited line")

    assert [(d["rect"], d.get("color")) for d in page.get_drawings()] == drawings_before
    images_after = page.get_image_info(xrefs=True)
    assert [i["bbox"] for i in images_after] == [i["bbox"] for i in images_before]
    # The green line through the old text is still visible. (Measured where
    # the shorter new text doesn't reach: new text is drawn on top of
    # existing graphics, a known limitation listed in the README.)
    assert _green_pixels(page) == green_before


def _green_pixels(page) -> int:
    """Pixels of the green line, along the stretch that crosses the end of
    the target text."""
    pix = page.get_pixmap(dpi=144, clip=pymupdf.Rect(115, 156, 140, 160))
    count = 0
    for y in range(pix.height):
        for x in range(pix.width):
            r, g, b = pix.pixel(x, y)[:3]
            count += g > r + 40 and g > b + 40
    return count


def test_no_box_is_painted_where_old_text_was(sample_doc):
    """The removal must not paint a filled rectangle (redactions do by
    default). Shorten the text and check the freed space is blank."""
    _edit(sample_doc, TARGET, "Short")
    target_line_tail = pymupdf.Rect(100, 160.5, 140, 163.5)  # below the green line
    pix = sample_doc[0].get_pixmap(dpi=144, clip=target_line_tail)
    assert min(pix.samples) > 240


def test_background_behind_edited_text_is_untouched():
    """Not even a white box: on a coloured background, any fill painted
    by the removal would show."""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=100)
    page.draw_rect(page.rect, color=None, fill=(0.6, 0.8, 1.0))
    page.insert_text((20, 50), "Text on a blue background", fontname="helv",
                     fontsize=12)
    run = te.page_runs(page, 0)[0]

    te.apply_edit(page, te.plan_edit(doc, page, run, "Blue"))

    # Where the (longer) old text was, beyond the new text: still blue.
    pix = page.get_pixmap(dpi=144, clip=pymupdf.Rect(60, 44, 160, 48))
    assert {pix.pixel(x, y)[:3] for x in range(pix.width)
            for y in range(pix.height)} == {(153, 204, 255)}


def test_delete_removes_the_run(sample_doc):
    plan = _edit(sample_doc, TARGET, "")
    assert plan.segments == []
    assert TARGET not in _texts(sample_doc)
    assert LINE_ABOVE in _texts(sample_doc) and LINE_BELOW in _texts(sample_doc)


def test_old_text_is_gone_from_the_file_not_just_covered(sample_doc):
    """In a garbage-collected save (which is how the editor saves), the old
    text exists nowhere in the file. Before garbage collection the old
    content stream lingers as an orphaned object, which is why saving
    must collect garbage."""
    _edit(sample_doc, TARGET, "Edited line")
    copy = pymupdf.open(stream=sample_doc.tobytes(), filetype="pdf")
    data = pymupdf.open(stream=copy.tobytes(garbage=3, deflate=True),
                        filetype="pdf")
    raw = b"".join((data.xref_stream(x) or b"") for x in range(1, data.xref_length())
                   if data.xref_is_stream(x))
    assert b"Target line" not in raw
    assert "Target line".encode().hex().encode() not in raw.lower()
    assert "Target" not in data[0].get_text()


def test_edit_in_bold_run_keeps_regular_neighbour(sample_doc):
    _edit(sample_doc, BOLD_PART, "DENIED")
    runs = {r.text.strip(): r for r in te.page_runs(sample_doc[0], 0)}
    assert runs["DENIED"].bold
    assert runs[REGULAR_PART.strip()].font == "Helvetica"
    assert not runs[REGULAR_PART.strip()].bold


def test_stale_run_is_rejected(sample_doc):
    target = _run(sample_doc, TARGET)
    _edit(sample_doc, TARGET, "Changed already")
    plan = te.plan_edit(sample_doc, sample_doc[0], target, "Again")
    with pytest.raises(te.EditError, match="changed since"):
        te.apply_edit(sample_doc[0], plan)


def test_collateral_damage_is_detected(sample_doc, monkeypatch):
    """If removal ever touches other text, the edit must fail loudly."""
    monkeypatch.setattr(te, "removal_rect",
                        lambda run: pymupdf.Rect(0, 140, 500, 180))
    target = _run(sample_doc, TARGET)
    plan = te.plan_edit(sample_doc, sample_doc[0], target, "Edited")
    with pytest.raises(te.EditError, match="nearby text"):
        te.apply_edit(sample_doc[0], plan)


def test_unverifiable_result_is_detected(sample_doc):
    target = _run(sample_doc, TARGET)
    plan = te.plan_edit(sample_doc, sample_doc[0], target, "Edited")
    # Same letters, different order: character counts alone can't tell.
    plan.segments[0].text = "Edtied"
    with pytest.raises(te.EditError, match="couldn't be verified"):
        te.apply_edit(sample_doc[0], plan)


def test_repeated_edits_do_not_grow_the_file(sample_doc):
    sizes = []
    for i in range(6):
        current = next(r for r in te.page_runs(sample_doc[0], 0)
                       if r.origin[1] == pytest.approx(80))
        plan = te.plan_edit(sample_doc, sample_doc[0], current, f"Report {i}")
        te.apply_edit(sample_doc[0], plan)
        copy = pymupdf.open(stream=sample_doc.tobytes(), filetype="pdf")
        sizes.append(len(copy.tobytes(garbage=3, deflate=True)))
    # Shrinks at first (unused original fonts drop out), then stays flat.
    assert sizes[-1] <= sizes[0], sizes
    assert max(sizes[2:]) - min(sizes[2:]) < 500, sizes


# ---------------------------------------------------------------------------
# Real-world fixture (macOS CoreText TrueType subsets)
# ---------------------------------------------------------------------------

def test_coretext_edit_with_substitution_then_reedit(coretext_doc):
    page = coretext_doc[0]
    original = _run(coretext_doc, "Total due: 1,250.00 USD")

    first = _edit(coretext_doc, original.text, "Total due: 9,999.99 EUR")
    assert first.substituted == ["9", "E", "R"]

    # Spaces must extract as plain spaces (not U+00A0).
    assert "Total due: 9,999.99 EUR" in page.get_text()

    # The line is one run again, still in the original font...
    rerun = _run(coretext_doc, "Total due: 9,999.99 EUR")
    assert rerun.font == original.font
    assert len(rerun.pieces) > 1

    # ...so it can be edited as a whole, back into the original glyphs.
    second = _edit(coretext_doc, rerun.text, "Total due: 1,000.00 USD")
    assert second.warnings == []
    final = _run(coretext_doc, "Total due: 1,000.00 USD")
    assert final.origin == pytest.approx(original.origin, abs=0.01)
    # Every visible character is back in the PDF's own font.
    assert all(p.font == original.font for p in final.pieces if p.text.strip())

    others = {"Invoice number 2024-117 for Acme Corp",
              "Thanks for your business.", "Fine print in Courier"}
    assert others <= set(_texts(coretext_doc))


def test_coretext_each_font_round_trips_its_own_text(coretext_doc):
    page = coretext_doc[0]
    for run in te.page_runs(page, 0):
        choice, reason = te.resolve_original_font(coretext_doc, page, run)
        assert reason == "", run.font
        plan = te.plan_edit(coretext_doc, page, run, run.text + "!")
        assert plan.can_apply, (run.font, plan.errors)
