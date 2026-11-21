"""Text-editing engine for PDF Editor.

Finds the editable runs of text on a page, plans a replacement (which font
each new character will be drawn with, and what deserves a warning), and
applies it. No Tk here, so all of this is testable without a display.

How an edit works
-----------------
1. **Remove the old text.** A redaction that touches text only (images and
   vector graphics are left alone, and nothing is painted in its place)
   removes the run's characters. The redaction rectangle is a thin band
   through the middle of the run, so neighbouring lines (even tightly
   spaced ones) and neighbouring runs on the same line aren't touched.
2. **Write the new text** at the same baseline origin, size and colour,
   using the run's own embedded font for every character it can draw, and
   a similar standard font for any it can't.
3. **Verify both steps** by counting the page's characters: after step 1,
   everything except the run must be unchanged; after step 2, exactly the
   new text must have been added. Anything else raises EditError, and the
   caller restores the page from its last undo snapshot.

Why "can it draw this character?" takes two checks
--------------------------------------------------
PDFs usually embed only a *subset* of each font: just the glyphs the
document used. Subsetting tools differ in how they cut the rest, so:

- some drop unused characters from the font's character map entirely,
  which ``Font.has_glyph()`` detects (it returns 0), but
- others keep the character map and just empty the glyph outlines, where
  ``has_glyph()`` still says yes and only actually drawing the glyph shows
  it's blank.

So a character counts as available only if ``has_glyph()`` is non-zero
**and** drawing it produces ink (whitespace is exempt from the ink check).
The ink check must never run on a character the font lacks, because
PyMuPDF silently draws missing glyphs with a fallback font, which would
look like ink. The same applies when writing: every segment of new text is
only ever written with a font known to contain all of its characters.

There's a third check, a round trip: write the character and extract it
again. When re-embedding a font, PyMuPDF builds the "glyph -> character"
table from the font's own character map, and fonts often map several
characters to one glyph (space and no-break space, hyphen and soft
hyphen). The table can then name the wrong one, so text that looks right
would copy, paste and search as something else. A character only uses a
font whose round trip gives it back unchanged.

What counts as one run
----------------------
PyMuPDF reports text in "spans" that break at every font change. After an
edit that needed a substitute font for a few characters, one visual line is
many spans. So a run here is a group of spans that sit next to each other
on the same baseline with the same size, colour and style (bold, italic,
monospaced), and it's rewritten in its *primary* font: the one most of its
characters use. Differently styled neighbours (regular next to bold) stay
separate runs, so editing one never flattens another's style.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pymupdf

# Removal band, as fractions of the font size measured up from the
# baseline: a thin horizontal strip through the middle of lowercase letters
# that every character of the run overlaps, but that stays clear of the
# lines above and below even when the line spacing equals the font size.
BAND_TOP = 0.45
BAND_BOTTOM = 0.20
# Horizontal inset (points) so the band doesn't touch a run that starts
# exactly where this one ends.
BAND_INSET = 0.3

HIT_TOLERANCE = 1.0          # points of slop around a run when clicking it
SAME_LINE_TOLERANCE = 0.5    # baseline difference (points) for "same line"

# Span flag bits reported by PyMuPDF text extraction.
FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16

# The PDF "standard 14" fonts, which viewers supply themselves when a PDF
# references them without embedding them. PyMuPDF has built-in
# equivalents, keyed by these short codes.
STANDARD_14 = {
    "helvetica": "helv", "helveticabold": "hebo",
    "helveticaoblique": "heit", "helveticaitalic": "heit",
    "helveticaboldoblique": "hebi", "helveticabolditalic": "hebi",
    "timesroman": "tiro", "times": "tiro", "timesbold": "tibo",
    "timesitalic": "tiit", "timesbolditalic": "tibi",
    "courier": "cour", "courierbold": "cobo",
    "courieroblique": "coit", "courieritalic": "coit",
    "courierboldoblique": "cobi", "courierbolditalic": "cobi",
    "symbol": "symb", "zapfdingbats": "zadb",
}

FONT_LABELS = {
    "helv": "Helvetica", "hebo": "Helvetica Bold", "heit": "Helvetica Oblique",
    "hebi": "Helvetica Bold Oblique", "tiro": "Times", "tibo": "Times Bold",
    "tiit": "Times Italic", "tibi": "Times Bold Italic", "cour": "Courier",
    "cobo": "Courier Bold", "coit": "Courier Oblique",
    "cobi": "Courier Bold Oblique", "symb": "Symbol", "zadb": "ZapfDingbats",
}

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


class EditError(Exception):
    """An edit couldn't be applied (or couldn't be verified) safely."""


# ---------------------------------------------------------------------------
# Finding text
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Piece:
    """One PyMuPDF span inside a TextRun."""

    text: str
    font: str
    flags: int
    bbox: Tuple[float, float, float, float]


@dataclass(frozen=True)
class TextRun:
    """A run of same-style text on one baseline (see the module docstring)."""

    page_num: int
    text: str
    font: str                                   # primary font name
    size: float
    flags: int                                  # of the primary font
    color: Tuple[float, float, float]           # RGB, 0..1
    origin: Tuple[float, float]                 # baseline start, PDF points
    bbox: Tuple[float, float, float, float]     # PDF points
    direction: Tuple[float, float]
    pieces: Tuple[Piece, ...] = ()

    @property
    def primary_text(self) -> str:
        """The characters drawn with the primary font."""
        if not self.pieces:
            return self.text
        return "".join(p.text for p in self.pieces if p.font == self.font)

    @property
    def rect(self) -> pymupdf.Rect:
        return pymupdf.Rect(self.bbox)

    @property
    def is_horizontal(self) -> bool:
        dx, dy = self.direction
        return abs(dx - 1.0) < 1e-3 and abs(dy) < 1e-3

    @property
    def bold(self) -> bool:
        return bool(self.flags & FLAG_BOLD) or "bold" in self.font.lower()

    @property
    def italic(self) -> bool:
        name = self.font.lower()
        return (bool(self.flags & FLAG_ITALIC)
                or "italic" in name or "oblique" in name)

    @property
    def monospaced(self) -> bool:
        name = self.font.lower()
        return (bool(self.flags & FLAG_MONO)
                or "courier" in name or "mono" in name)

    @property
    def serif(self) -> bool:
        name = self.font.lower()
        if "sans" in name:
            return False
        return (bool(self.flags & FLAG_SERIF)
                or any(s in name for s in ("times", "roman", "serif",
                                            "georgia", "garamond")))

    def same_as(self, other: "TextRun") -> bool:
        """Same text at the same place (tolerating float noise)."""
        return (self.text == other.text
                and all(abs(a - b) < 0.05 for a, b in zip(self.bbox, other.bbox)))


def _style_key(font: str, flags: int) -> Tuple[bool, bool, bool]:
    name = font.lower()
    return (bool(flags & FLAG_BOLD) or "bold" in name,
            bool(flags & FLAG_ITALIC) or "italic" in name or "oblique" in name,
            bool(flags & FLAG_MONO) or "courier" in name or "mono" in name)


def _rgb(color_int: int) -> Tuple[float, float, float]:
    return (((color_int >> 16) & 255) / 255.0,
            ((color_int >> 8) & 255) / 255.0,
            (color_int & 255) / 255.0)


# Largest gap (as a fraction of the font size) between two spans for them
# to count as one run.
MAX_JOIN_GAP = 0.3


def page_runs(page: pymupdf.Page, page_num: int) -> List[TextRun]:
    """Every run of visible text on the page (see the module docstring for
    how spans are grouped into runs), top to bottom, left to right."""
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"]:
                    spans.append((span, tuple(line["dir"])))

    def sort_key(item):
        span, _direction = item
        return (round(span["origin"][1], 1), span["bbox"][0])

    groups: List[List[tuple]] = []
    for span, direction in sorted(spans, key=sort_key):
        if groups and _joins(groups[-1][-1], (span, direction)):
            groups[-1].append((span, direction))
        else:
            groups.append([(span, direction)])

    runs = []
    for group in groups:
        text = "".join(s["text"] for s, _ in group)
        if not text.strip():
            continue
        weight: Counter = Counter()
        for s, _ in group:
            weight[s["font"]] += len(s["text"].strip()) or 0
        primary = max(weight, key=lambda f: (weight[f], -_first_index(group, f)))
        first = group[0][0]
        primary_span = next(s for s, _ in group if s["font"] == primary)
        x0 = min(s["bbox"][0] for s, _ in group)
        y0 = min(s["bbox"][1] for s, _ in group)
        x1 = max(s["bbox"][2] for s, _ in group)
        y1 = max(s["bbox"][3] for s, _ in group)
        runs.append(TextRun(
            page_num=page_num,
            text=text,
            font=primary,
            size=first["size"],
            flags=primary_span["flags"],
            color=_rgb(first["color"]),
            origin=tuple(first["origin"]),
            bbox=(x0, y0, x1, y1),
            direction=group[0][1],
            pieces=tuple(Piece(s["text"], s["font"], s["flags"],
                               tuple(s["bbox"])) for s, _ in group),
        ))
    return runs


def _first_index(group, font) -> int:
    return next(i for i, (s, _) in enumerate(group) if s["font"] == font)


def _joins(prev, cur) -> bool:
    """Whether span ``cur`` continues the run that span ``prev`` ends."""
    (a, a_dir), (b, b_dir) = prev, cur
    horizontal = (1.0, 0.0)
    if not (_close(a_dir, horizontal) and _close(b_dir, horizontal)):
        return False
    if abs(a["origin"][1] - b["origin"][1]) > SAME_LINE_TOLERANCE:
        return False
    if abs(a["size"] - b["size"]) > 0.05 or a["color"] != b["color"]:
        return False
    if _style_key(a["font"], a["flags"]) != _style_key(b["font"], b["flags"]):
        return False
    gap = b["bbox"][0] - a["bbox"][2]
    return -1.0 <= gap <= MAX_JOIN_GAP * a["size"]


def _close(a, b) -> bool:
    return all(abs(x - y) < 1e-3 for x, y in zip(a, b))


def run_at(runs: List[TextRun], x: float, y: float,
           tolerance: float = HIT_TOLERANCE) -> Optional[TextRun]:
    """The run under PDF point (x, y), preferring the smallest if several
    overlap there."""
    hits = [r for r in runs
            if r.bbox[0] - tolerance <= x <= r.bbox[2] + tolerance
            and r.bbox[1] - tolerance <= y <= r.bbox[3] + tolerance]
    if not hits:
        return None
    return min(hits, key=lambda r: r.rect.width * r.rect.height)


# Whitespace that text extraction adds or drops depending on layout. Every
# other character (including unusual spaces like U+00A0) is counted, so a
# space that comes back as a no-break space fails verification.
_LAYOUT_WHITESPACE = " \n\r\t"


def _counted(text: str) -> Counter:
    return Counter(c for c in text if c not in _LAYOUT_WHITESPACE)


def _char_counts(page: pymupdf.Page) -> Counter:
    """Every character on the page except layout whitespace, counted."""
    return _counted(page.get_text())


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

def normalize_font_name(name: str) -> str:
    """'ABCDEF+Nimbus Roman-Regular' -> 'nimbusromanregular': no subset
    prefix, case or punctuation, so the names a PDF's font objects use and
    the names text extraction reports can be compared."""
    return re.sub(r"[^a-z0-9]", "", _SUBSET_PREFIX.sub("", name).lower())


@dataclass(frozen=True)
class FontChoice:
    """A font new text can be written with."""

    font: pymupdf.Font = field(compare=False)
    label: str                  # human-readable name
    original: bool              # is this the run's own font?
    key: str                    # identity, for caching
    check_ink: bool             # subset fonts need the ink check

    def has_char(self, ch: str) -> bool:
        """Whether the font really contains a drawable glyph for ``ch``
        (character map plus ink check; see the module docstring)."""
        if not self.font.has_glyph(ord(ch)):
            return False
        if not self.check_ink:
            return True
        return _probe(self, ch)[0]

    def can_draw(self, ch: str) -> bool:
        """Whether ``ch`` can be written with this font so that it both
        looks right and extracts as itself."""
        if not self.has_char(ch):
            return False
        if not self.check_ink:
            return True
        return _probe(self, ch)[1]


_PROBE_CACHE: Dict[Tuple[str, str], Tuple[bool, bool]] = {}
_SENTINEL = "x"


def _probe(choice: FontChoice, ch: str) -> Tuple[bool, bool]:
    """(has ink, round-trips) for one character. Only call it for
    characters the font's cmap contains (see the module docstring)."""
    cache_key = (choice.key, ch)
    if cache_key in _PROBE_CACHE:
        return _PROBE_CACHE[cache_key]
    sentinel_font = pymupdf.Font("helv")
    scratch = pymupdf.open()
    try:
        # Ink: the character on its own.
        page = scratch.new_page(width=96, height=72)
        writer = pymupdf.TextWriter(page.rect)
        writer.append((24, 52), ch, font=choice.font, fontsize=40)
        writer.write_text(page)
        pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
        inked = ch.isspace() or min(pix.samples) < 200

        # Round trip: between two sentinels, so a space is extracted too.
        page = scratch.new_page(width=200, height=72)
        writer = pymupdf.TextWriter(page.rect)
        _r, pos = writer.append((24, 52), _SENTINEL, font=sentinel_font,
                                fontsize=20)
        _r, pos = writer.append(pos, ch, font=choice.font, fontsize=20)
        writer.append(pos, _SENTINEL, font=sentinel_font, fontsize=20)
        writer.write_text(page)
        round_trips = (page.get_text().rstrip("\n")
                       == _SENTINEL + ch + _SENTINEL)
    finally:
        scratch.close()
    _PROBE_CACHE[cache_key] = (inked, round_trips)
    return _PROBE_CACHE[cache_key]


def fallback_font_code(run: TextRun) -> str:
    """The standard font closest in style to ``run``."""
    if run.monospaced:
        family = ("cour", "cobo", "coit", "cobi")
    elif run.serif:
        family = ("tiro", "tibo", "tiit", "tibi")
    else:
        family = ("helv", "hebo", "heit", "hebi")
    return family[(1 if run.bold else 0) + (2 if run.italic else 0)]


def fallback_font(run: TextRun) -> FontChoice:
    code = fallback_font_code(run)
    return FontChoice(font=pymupdf.Font(code), label=FONT_LABELS[code],
                      original=False, key=f"base14:{code}", check_ink=False)


def resolve_original_font(doc: pymupdf.Document, page: pymupdf.Page,
                          run: TextRun) -> Tuple[Optional[FontChoice], str]:
    """The run's own font, ready to write with, or (None, reason).

    Several fonts on a page can share a name (e.g. two different subsets of
    the same typeface), so a candidate only counts if it can draw every
    character the run already shows in that font.
    """
    wanted = normalize_font_name(run.font)
    seen = set()
    found_embedded = False
    for xref, _ext, _type, basefont, *_ in page.get_fonts(full=True):
        if xref in seen:
            continue
        seen.add(xref)
        _name, ext, _ftype, content = doc.extract_font(xref)
        if not content or ext in ("", "n/a"):
            continue
        try:
            font = pymupdf.Font(fontbuffer=content)
        except Exception:
            continue
        # Text extraction reports the embedded font program's own name,
        # which needn't match the name the PDF declares for it (e.g. a
        # font declared "Helvetica" whose program is "NimbusSans-Regular"),
        # so accept a match on either.
        if wanted not in (normalize_font_name(basefont),
                          normalize_font_name(font.name)):
            continue
        found_embedded = True
        if not font.is_writable:
            continue
        choice = FontChoice(font=font, label=run.font, original=True,
                            key=hashlib.sha1(content).hexdigest(),
                            check_ink=True)
        if all(choice.has_char(c) for c in run.primary_text
               if not c.isspace()):
            return choice, ""

    if not found_embedded:
        code = STANDARD_14.get(wanted)
        if code:
            # A standard font the viewer supplies; PyMuPDF's built-in copy
            # is the same one it rendered the original with.
            return FontChoice(font=pymupdf.Font(code), label=run.font,
                              original=True, key=f"base14:{code}",
                              check_ink=False), ""
        return None, f"“{run.font}” isn't embedded in this PDF"
    return None, f"the PDF's copy of “{run.font}” can't be reused"


# ---------------------------------------------------------------------------
# Planning and applying an edit
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    text: str
    choice: FontChoice


@dataclass
class EditPlan:
    """Everything decided about an edit before anything is changed."""

    run: TextRun
    new_text: str
    segments: List[Segment] = field(default_factory=list)
    font_label: str = ""                 # what the run will mostly use
    substitute_label: str = ""           # fallback font, if any is used
    substituted: List[str] = field(default_factory=list)
    whole_run_reason: str = ""           # set if the original font is unusable
    unsupported: List[str] = field(default_factory=list)
    overlaps: List[str] = field(default_factory=list)
    new_width: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.new_text == self.run.text

    @property
    def can_apply(self) -> bool:
        return not self.errors

    @property
    def warnings(self) -> List[str]:
        out = []
        if self.whole_run_reason and self.new_text.strip():
            out.append(f"{self.whole_run_reason[0].upper()}"
                       f"{self.whole_run_reason[1:]}, so this text will be "
                       f"rewritten in {self.substitute_label}.")
        elif self.substituted:
            chars = " ".join(self.substituted)
            out.append(f"These characters can't be drawn with the PDF's copy "
                       f"of “{self.run.font}” (PDFs usually include only the "
                       f"characters a document already uses), so they'll "
                       f"use {self.substitute_label} instead: {chars}")
        if self.overlaps:
            others = ", ".join(f"“{t}”" for t in self.overlaps)
            out.append(f"The new text is wider than the old and will "
                       f"overlap {others} on the same line.")
        return out


def _unique(chars) -> List[str]:
    return list(dict.fromkeys(chars))


def plan_edit(doc: pymupdf.Document, page: pymupdf.Page, run: TextRun,
              new_text: str, runs: Optional[List[TextRun]] = None) -> EditPlan:
    """Decide how ``run`` would be replaced by ``new_text``, without
    changing anything."""
    plan = EditPlan(run=run, new_text=new_text)
    if plan.is_noop:
        return plan

    if page.rotation:
        plan.errors.append("Text on rotated pages can't be edited yet.")
    if not run.is_horizontal:
        plan.errors.append("Only horizontal text can be edited for now.")
    if "�" in run.text:
        plan.errors.append(
            "This text has no reliable character mapping in the PDF, so it "
            "can't be edited safely.")
    if any(c in new_text for c in "\r\n\t") or any(
            ord(c) < 32 for c in new_text):
        plan.errors.append("Line breaks, tabs and control characters "
                           "aren't supported in edited text.")
    if any(True for _ in page.annots(types=[pymupdf.PDF_ANNOT_REDACT])):
        plan.errors.append(
            "This page has unapplied redaction marks. Applying an edit would "
            "apply them too, so apply or remove them in a redaction tool "
            "first.")
    if plan.errors:
        return plan

    original, reason = resolve_original_font(doc, page, run)
    fallback = fallback_font(run)
    plan.substitute_label = fallback.label
    plan.whole_run_reason = reason
    plan.font_label = original.label if original else fallback.label

    for ch in new_text:
        if original is not None and original.can_draw(ch):
            choice = original
        elif fallback.can_draw(ch):
            choice = fallback
            if original is not None and not ch.isspace():
                plan.substituted.append(ch)
        else:
            plan.unsupported.append(ch)
            continue
        if plan.segments and plan.segments[-1].choice.key == choice.key:
            plan.segments[-1].text += ch
        else:
            plan.segments.append(Segment(text=ch, choice=choice))

    plan.substituted = _unique(plan.substituted)
    plan.unsupported = _unique(plan.unsupported)
    if plan.unsupported:
        plan.errors.append(
            "None of the available fonts can draw these characters: "
            + " ".join(plan.unsupported))
        return plan

    plan.new_width = sum(s.choice.font.text_length(s.text, fontsize=run.size)
                         for s in plan.segments)
    new_right = run.origin[0] + plan.new_width
    if runs is None:
        runs = page_runs(page, run.page_num)
    for other in runs:
        if (other.is_horizontal and not other.same_as(run)
                and abs(other.origin[1] - run.origin[1]) <= SAME_LINE_TOLERANCE
                and other.bbox[0] >= run.bbox[2] - 0.5
                and new_right > other.bbox[0] + 0.5):
            plan.overlaps.append(other.text.strip())
    return plan


def removal_rect(run: TextRun) -> pymupdf.Rect:
    """The thin band that removes ``run``'s characters and nothing else."""
    _ox, oy = run.origin
    return pymupdf.Rect(run.bbox[0] + BAND_INSET, oy - BAND_TOP * run.size,
                        run.bbox[2] - BAND_INSET, oy - BAND_BOTTOM * run.size)


def apply_edit(page: pymupdf.Page, plan: EditPlan) -> None:
    """Replace the run's text on ``page`` as planned.

    Raises EditError if the plan can't be applied or the result can't be
    verified. In that case the page may be partly modified, and the caller
    must restore it (e.g. from the last undo snapshot).
    """
    if not plan.can_apply:
        raise EditError(plan.errors[0])
    if plan.is_noop:
        return
    run = plan.run

    if not any(r.same_as(run) for r in page_runs(page, run.page_num)):
        raise EditError("That text has changed since it was selected. "
                        "Click it again to edit it.")

    expected_after_removal = _char_counts(page) - _counted(run.text)

    page.add_redact_annot(removal_rect(run), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    if _char_counts(page) != expected_after_removal:
        raise EditError("Removing the old text would also have changed "
                        "nearby text, so the edit was cancelled.")

    if plan.segments:
        writer = pymupdf.TextWriter(page.rect)
        position = pymupdf.Point(run.origin)
        for segment in plan.segments:
            _rect, position = writer.append(position, segment.text,
                                            font=segment.choice.font,
                                            fontsize=run.size)
        writer.write_text(page, color=run.color)

    if (_char_counts(page) != expected_after_removal + _counted(plan.new_text)
            or not _reads_back(page, plan)):
        raise EditError("The new text couldn't be verified after writing "
                        "it, so the edit was cancelled.")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _reads_back(page: pymupdf.Page, plan: EditPlan) -> bool:
    """Whether the new text extracts, in order, on the run's baseline where
    it was written. (Character counts alone can't see reordering.)"""
    wanted = _collapse(plan.new_text)
    if not wanted:
        return True
    x0 = plan.run.origin[0]
    x1 = x0 + plan.new_width
    for run in page_runs(page, plan.run.page_num):
        if (abs(run.origin[1] - plan.run.origin[1]) <= SAME_LINE_TOLERANCE
                and run.bbox[0] < x1 and run.bbox[2] > x0
                and wanted in _collapse(run.text)):
            return True
    return False


# ---------------------------------------------------------------------------
# Adding new text
# ---------------------------------------------------------------------------

FAMILIES = {"Helvetica": ("helv", "hebo", "heit", "hebi"),
            "Times": ("tiro", "tibo", "tiit", "tibi"),
            "Courier": ("cour", "cobo", "coit", "cobi")}


@dataclass(frozen=True)
class TextStyle:
    """How new text looks. Uses the standard fonts every PDF viewer has."""

    family: str = "Helvetica"
    size: float = 12.0
    bold: bool = False
    italic: bool = False
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def font_code(self) -> str:
        return FAMILIES[self.family][(1 if self.bold else 0) + (2 if self.italic else 0)]

    @property
    def label(self) -> str:
        return FONT_LABELS[self.font_code]


@dataclass
class AddPlan:
    page_num: int
    top_left: Tuple[float, float]      # where the text box was placed
    text: str
    style: TextStyle
    origin: Tuple[float, float] = (0.0, 0.0)   # baseline start
    width: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def can_apply(self) -> bool:
        return not self.errors and bool(self.text.strip())


MIN_SIZE, MAX_SIZE = 4.0, 144.0


def plan_add_text(page: pymupdf.Page, page_num: int,
                  top_left: Tuple[float, float], text: str,
                  style: TextStyle) -> AddPlan:
    """Plan writing ``text`` with its top-left corner at ``top_left``."""
    plan = AddPlan(page_num=page_num, top_left=top_left, text=text, style=style)
    if style.family not in FAMILIES:
        plan.errors.append(f"Unknown font family “{style.family}”.")
        return plan
    if not MIN_SIZE <= style.size <= MAX_SIZE:
        plan.errors.append(f"Font size must be between {MIN_SIZE:g} and "
                           f"{MAX_SIZE:g} pt.")
    if page.rotation:
        plan.errors.append("Adding text to rotated pages isn't supported yet.")
    if any(c in text for c in "\r\n\t") or any(ord(c) < 32 for c in text):
        plan.errors.append("Line breaks, tabs and control characters "
                           "aren't supported in added text.")
    font = pymupdf.Font(style.font_code)
    missing = _unique(c for c in text if not font.has_glyph(ord(c)))
    if missing:
        plan.errors.append(f"{style.label} can't draw these characters: "
                           + " ".join(missing))
    x, y = top_left
    plan.origin = (x, y + font.ascender * style.size)
    plan.width = font.text_length(text, fontsize=style.size)
    return plan


def apply_add_text(page: pymupdf.Page, plan: AddPlan) -> None:
    """Write planned new text, verifying it reads back. Raises EditError;
    the caller restores the page on failure."""
    if not plan.can_apply:
        raise EditError(plan.errors[0] if plan.errors else "There's no text to add.")
    expected = _char_counts(page) + _counted(plan.text)
    writer = pymupdf.TextWriter(page.rect)
    writer.append(plan.origin, plan.text, font=pymupdf.Font(plan.style.font_code),
                  fontsize=plan.style.size)
    writer.write_text(page, color=plan.style.color)
    probe = EditPlan(run=TextRun(plan.page_num, "", "", plan.style.size, 0,
                                 plan.style.color, plan.origin, (0, 0, 0, 0),
                                 (1.0, 0.0)),
                     new_text=plan.text, new_width=plan.width)
    if _char_counts(page) != expected or not _reads_back(page, probe):
        raise EditError("The new text couldn't be verified after writing "
                        "it, so nothing was added.")
