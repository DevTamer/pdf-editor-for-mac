"""Find and replace (findreplace.py, and EditorModel's find/replace)."""

import pymupdf
import pytest

import editor
import findreplace as fr
import textedit as te
from conftest import LINE_ABOVE, LINE_BELOW, SECOND_PAGE_TEXT, TARGET, TITLE


# -- Matching text --------------------------------------------------------------------

@pytest.mark.parametrize("text, query, options, expected", [
    ("Target line", "line", {}, [(7, 11)]),
    ("Target LINE", "line", {}, [(7, 11)]),                     # any case...
    ("Target LINE", "line", {"match_case": True}, []),          # ...unless asked
    ("cat category", "cat", {"whole_word": True}, [(0, 3)]),
    ("cat category", "cat", {}, [(0, 3), (4, 7)]),
    ("1x2 1.2", "1.2", {}, [(4, 7)]),                           # not a pattern
    ("aaaa", "aa", {}, [(0, 2), (2, 4)]),                       # no overlaps
    ("The ﬁnal word", "final", {}, [(4, 8)]),                   # ligature
    ("ofﬁce hours", "office hours", {}, [(0, 11)]),      # ligature + NBSP
    ("co­operate", "cooperate", {}, [(0, 10)]),            # soft hyphen
    ("ﬁx", "f", {}, []),                                        # half a ligature
    ("ﬁx", "ix", {}, []),                                       # ...either half
    ("anything", "", {}, []),
])
def test_find_in_text(text, query, options, expected):
    assert fr.find_in_text(text, query, **options) == expected


def test_matches_highlight_just_the_matched_characters(sample_doc):
    matches = fr.find_all(sample_doc, "line")
    assert [m.text for m in matches] == ["line"] * 3
    for match in matches:
        x0, y0, x1, y1 = match.rect
        rx0, ry0, rx1, ry1 = match.run.bbox
        assert rx0 < x0 < x1 < rx1                  # inside the line, narrower
        assert x1 - x0 < (rx1 - rx0) / 3


def test_find_all_is_in_page_and_reading_order(sample_doc):
    matches = fr.find_all(sample_doc, "e")
    pages = [m.page_num for m in matches]
    assert pages == sorted(pages) and 1 in pages
    assert matches[0].run.text.strip() == TITLE


# -- Replacing one ----------------------------------------------------------------

def _model(sample_pdf_path):
    model = editor.EditorModel()
    model.open_document(str(sample_pdf_path))
    return model


def test_replace_one_keeps_the_line_and_is_one_undo_step(sample_pdf_path):
    model = _model(sample_pdf_path)
    match = next(m for m in model.find("line") if m.run.text.strip() == TARGET)
    plan = model.plan_replace(match, "row")
    assert plan.can_apply and plan.warnings == []
    label = model.apply_replace(plan, match, "row")
    assert label == "Replace “line” → “row”"
    text = model.get_page(0).get_text()
    assert "Target row to edit" in text
    assert LINE_ABOVE in text and LINE_BELOW in text          # others untouched
    run = next(r for r in model.runs(0) if r.text.strip() == "Target row to edit")
    # (Same size and colour; a standard font's text comes back as PyMuPDF's
    # metric-identical copy, NimbusSans for Helvetica, as with any edit.)
    assert run.size == match.run.size and run.color == match.run.color
    model.undo()
    assert TARGET in model.get_page(0).get_text()


# -- Replace All -------------------------------------------------------------------

def test_replace_all_changes_every_page_in_one_undo_step(sample_pdf_path):
    model = _model(sample_pdf_path)
    matches = model.find("the")                 # page 1 three times, page 2 once
    plan = model.plan_replace_all(matches, "a", "the")
    assert plan.count == len(matches) == 5 and plan.pages == [0, 1]
    assert plan.skipped == []
    model.apply_replace_all(plan)
    assert model.history_entries()[-1] == "Replace all “the” → “a” (5)"
    assert len(model.history_entries()) == 2
    assert model.find("the") == []
    assert "Second page stays exactly a same" in model.get_page(1).get_text()
    model.undo()
    assert LINE_ABOVE in model.get_page(0).get_text()
    assert SECOND_PAGE_TEXT in model.get_page(1).get_text()


def test_several_matches_in_one_line_are_one_edit(sample_pdf_path):
    model = _model(sample_pdf_path)
    plan = model.plan_replace_all(model.find("e", match_case=True), "E", "e")
    target_plan = next(p for p in plan.plans if p.run.text.strip() == TARGET)
    assert target_plan.new_text.strip() == "TargEt linE to Edit"
    assert plan.count == sum(plan.counts) and len(plan.plans) < plan.count


def test_replacement_containing_the_search_text(sample_pdf_path):
    model = _model(sample_pdf_path)
    plan = model.plan_replace_all(model.find("line"), "line line", "line")
    model.apply_replace_all(plan)
    assert len(model.find("line")) == 6


def test_lines_that_cant_change_are_skipped_and_said_why(sample_pdf_path):
    model = _model(sample_pdf_path)
    page = model.get_page(0)
    target = next(r for r in model.runs(0) if r.text.strip() == TARGET)
    page.add_freetext_annot(pymupdf.Rect(target.bbox), "note")
    plan = model.plan_replace_all(model.find("line"), "row", "line")
    assert plan.count == 2 and len(plan.skipped) == 1
    assert fr.skipped_summary(plan) == [f"1 × {te.ANNOTATION_TEXT_ERROR}"]
    model.apply_replace_all(plan)
    text = model.get_page(0).get_text()
    assert "The row above the target" in text and TARGET in text


def test_replace_all_is_all_or_nothing(sample_pdf_path, monkeypatch):
    model = _model(sample_pdf_path)
    plan = model.plan_replace_all(model.find("the"), "a", "the")
    real = editor.apply_edits
    calls = []

    def fail_on_second_page(page, edits):
        calls.append(1)
        if len(calls) == 2:
            raise te.EditError("verification failed")
        real(page, edits)
    monkeypatch.setattr(editor, "apply_edits", fail_on_second_page)
    with pytest.raises(te.EditError, match="Nothing was replaced: page 2 couldn't"):
        model.apply_replace_all(plan)
    assert LINE_ABOVE in model.get_page(0).get_text()       # first one undone too
    assert len(model.history_entries()) == 1


def test_text_split_across_styles_isnt_one_match(sample_pdf_path):
    """A documented limit: "Status: APPROVED" is a regular run then a bold
    one, so it can't be found (or replaced) as one piece of text."""
    model = _model(sample_pdf_path)
    assert model.find("Status: APPROVED") == []
    assert len(model.find("APPROVED")) == 1


def test_a_batch_on_one_page_is_verified_as_strictly_as_one_edit(sample_doc, monkeypatch):
    """If writing one of a page's replacements goes wrong, the batch fails
    verification (the caller then restores the page)."""
    page = sample_doc[0]
    plans = [fr.plan_replace(sample_doc, m, "row") for m in fr.find_all(sample_doc, "line")]
    assert len(plans) == 3
    real = te.write_text
    calls = []

    def drop_second(page, origin, pieces, size, color):
        calls.append(1)
        if len(calls) != 2:
            real(page, origin, pieces, size, color)
    monkeypatch.setattr(te, "write_text", drop_second)
    with pytest.raises(te.EditError, match="couldn't be verified"):
        te.apply_edits(page, plans)


def test_a_batch_that_would_damage_a_neighbour_is_refused(sample_doc, monkeypatch):
    page = sample_doc[0]
    # The lines above and below the target; the target itself isn't in
    # the batch, and a too-tall removal band would hit it.
    plans = [fr.plan_replace(sample_doc, m, "row") for m in fr.find_all(sample_doc, "line")
             if m.run.text.strip() != TARGET]
    assert len(plans) == 2
    wide = te.removal_rect
    monkeypatch.setattr(te, "removal_rect",
                        lambda run: pymupdf.Rect(wide(run)) + (0, -8, 0, 8))
    with pytest.raises(te.EditError, match="nearby text"):
        te.apply_edits(page, plans)


def test_search_ignores_case_and_ligatures_on_every_page(sample_pdf_path):
    model = _model(sample_pdf_path)
    assert len(model.find("LINE")) == 3                 # the page says "line"
    assert len(model.find("SECOND PAGE")) == 1


def test_every_line_in_a_batch_must_read_back(sample_doc, monkeypatch):
    """Character counts can't see text written in the wrong place."""
    page = sample_doc[0]
    plans = [fr.plan_replace(sample_doc, m, "row") for m in fr.find_all(sample_doc, "line")]
    real = te.write_text
    calls = []

    def misplace_second(page, origin, pieces, size, color):
        calls.append(1)
        if len(calls) == 2:
            origin = (origin[0], origin[1] + 200)
        real(page, origin, pieces, size, color)
    monkeypatch.setattr(te, "write_text", misplace_second)
    with pytest.raises(te.EditError, match="couldn't be verified"):
        te.apply_edits(page, plans)


# -- Review findings ------------------------------------------------------------------

from erase import ErasePlan  # noqa: E402


def test_search_sees_changes_made_inside_form_xobjects(tmp_path):
    """MuPDF can move a page's text into a form XObject when it removes
    text, leaving the page's own content identical for different edits."""
    src = pymupdf.open()
    page = src.new_page(width=400, height=300)
    for y, text in ((100, "Alpha line one"), (130, "Beta line two"),
                    (160, "Gamma line three")):
        page.insert_text((50, y), text, fontname="helv", fontsize=12)
    doc = pymupdf.open()
    doc.new_page(width=400, height=300).show_pdf_page(pymupdf.Rect(0, 0, 400, 300), src, 0)
    path = tmp_path / "forms.pdf"
    doc.save(str(path))

    model = editor.EditorModel()
    model.open_document(str(path))

    def erase(word):
        w = next(w for w in model.words(0) if w.text == word)
        model.erase(ErasePlan(page_num=0, words=[w]))
    erase("Beta")
    assert [m.run.text.strip() for m in model.find("line")][1] == "line two"
    model.undo()
    erase("Gamma")
    found = sorted(m.run.text.strip() for m in model.find("line"))
    assert found == ["Alpha line one", "Beta line two", "line three"]
    assert model.find("Gamma") == []


def test_text_drawn_twice_is_replaced_once_each(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((50, 100), "Total line here", fontname="helv", fontsize=12)
    page.insert_text((50, 100), "Total line here", fontname="helv", fontsize=12)
    page.insert_text((50, 140), "Other line text", fontname="helv", fontsize=12)
    path = tmp_path / "dup.pdf"
    doc.save(str(path))
    model = editor.EditorModel()
    model.open_document(str(path))
    plan = model.plan_replace_all(model.find("line"), "row", "line")
    assert [p.new_text for p in plan.plans].count("Total row here") == 2
    assert plan.count == 3 and plan.skipped == []
    model.apply_replace_all(plan)
    assert model.get_page(0).get_text() == ("Total row here\nTotal row here\n"
                                            "Other row text\n")


def test_a_line_whose_removal_would_hit_other_text_is_skipped_not_fatal(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((50, 100), "Total line here", fontname="helv", fontsize=12)
    page.insert_text((50, 99), "XXXXXXXXXXXXXXXX", fontname="helv", fontsize=12)  # overprinted
    page.insert_text((50, 140), "Other line text", fontname="helv", fontsize=12)
    path = tmp_path / "over.pdf"
    doc.save(str(path))
    model = editor.EditorModel()
    model.open_document(str(path))
    plan = model.plan_replace_all(model.find("line"), "row", "line")
    assert [why for _m, why in plan.skipped] == [fr.OVERLAP_ERROR]
    model.apply_replace_all(plan)                  # the rest still goes ahead
    text = model.get_page(0).get_text()
    assert "Other row text" in text and "Total line here" in text


def test_batch_refuses_when_a_neighbour_line_changes_even_if_counts_balance(
        sample_doc, monkeypatch):
    """A wrongly removed character elsewhere can't hide behind one that
    should have gone but didn't: lines not being replaced must be intact,
    whatever the character counts say."""
    from collections import Counter
    page = sample_doc[0]
    plans = [fr.plan_replace(sample_doc, m, "row") for m in fr.find_all(sample_doc, "line")
             if m.run.text.strip() == TARGET]
    # Character counts that always balance, so only the per-line check
    # can notice anything.
    monkeypatch.setattr(te, "_char_counts", lambda page: Counter())
    monkeypatch.setattr(te, "_counted", lambda text: Counter())
    real_apply = pymupdf.Page.apply_redactions

    def also_damage_title(self, *a, **k):
        self.add_redact_annot(pymupdf.Rect(40, 60, 70, 85), fill=False)  # "Qu" of the title
        return real_apply(self, *a, **k)
    monkeypatch.setattr(pymupdf.Page, "apply_redactions", also_damage_title)
    with pytest.raises(te.EditError, match="nearby text"):
        te.apply_edits(page, plans)
