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
import io
import math
import re
import string
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pymupdf
from fontTools import agl
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

import fontmatch
from fontmatch import Face

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

# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------
#
# Comment boxes (FreeText), stamps and form fields draw their own text, and
# PyMuPDF's text extraction reports it alongside the page's. But it isn't
# page content: a redaction over it deletes the whole annotation. So that
# text is refused up front, and every removal also checks that no
# annotation disappeared.

_TEXT_ANNOT_TYPES = (pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_STAMP,
                     pymupdf.PDF_ANNOT_WIDGET)

ANNOTATION_TEXT_ERROR = (
    "This text is part of (or under) a comment box, stamp or form field. "
    "The editor can't change those yet.")
ANNOTATION_LOST_ERROR = (
    "That would also have removed a comment, stamp or form field, so "
    "nothing was changed.")


def annotation_boxes(page: pymupdf.Page) -> List[pymupdf.Rect]:
    """Areas of annotations and form fields that draw their own text."""
    boxes = [pymupdf.Rect(a.rect) for a in page.annots(types=_TEXT_ANNOT_TYPES)]
    boxes += [pymupdf.Rect(w.rect) for w in page.widgets()]
    return boxes


def touches_annotation(page: pymupdf.Page, rect: pymupdf.Rect) -> bool:
    return any(rect.intersects(box) for box in annotation_boxes(page))


def annotation_inventory(page: pymupdf.Page) -> set:
    """Every annotation, link and form field on the page (by object
    number), except redaction marks."""
    # (PyMuPDF 1.24 returns (xref, type, id) triples here.)
    return {entry[0] for entry in page.annot_xrefs()
            if entry[1] != pymupdf.PDF_ANNOT_REDACT}


def check_annotations_kept(page: pymupdf.Page, before: set) -> None:
    if annotation_inventory(page) != before:
        raise EditError(ANNOTATION_LOST_ERROR)


# ---------------------------------------------------------------------------
# Writing text
# ---------------------------------------------------------------------------

_WRITER_OFFSETS: Dict[Tuple, Tuple[float, float]] = {}


def _writer_offset(page: pymupdf.Page) -> Tuple[float, float]:
    """How far TextWriter misplaces text on this page's geometry.

    In PyMuPDF 1.24, on a page whose CropBox has different top and bottom
    margins, TextWriter puts text off vertically by the difference. Rather
    than hard-code that (and break when PyMuPDF fixes it), measure it once
    per MediaBox/CropBox with a probe on a scratch page.
    """
    key = (tuple(page.mediabox), tuple(page.cropbox))
    if key not in _WRITER_OFFSETS:
        scratch = pymupdf.open()
        try:
            probe = scratch.new_page(width=page.mediabox.width,
                                     height=page.mediabox.height)
            probe.set_mediabox(page.mediabox)
            probe.set_cropbox(page.cropbox)
            want = pymupdf.Point(probe.rect.width / 2, probe.rect.height / 2)
            writer = pymupdf.TextWriter(probe.rect)
            writer.append(want, "x", font=pymupdf.Font("helv"), fontsize=10)
            writer.write_text(probe)
            got = pymupdf.Point(
                probe.get_text("dict")["blocks"][0]["lines"][0]["spans"][0]["origin"])
            _WRITER_OFFSETS[key] = (got.x - want.x, got.y - want.y)
        except Exception:
            _WRITER_OFFSETS[key] = (0.0, 0.0)  # verification will catch it
        finally:
            scratch.close()
    return _WRITER_OFFSETS[key]


def write_text(page: pymupdf.Page, origin: Tuple[float, float],
               pieces: List[Tuple[str, pymupdf.Font]], size: float,
               color: Tuple[float, float, float]) -> None:
    """Write text pieces one after another from baseline ``origin``."""
    dx, dy = _writer_offset(page)
    writer = pymupdf.TextWriter(page.rect)
    position = pymupdf.Point(origin[0] - dx, origin[1] - dy)
    for text, font in pieces:
        _rect, position = writer.append(position, text, font=font, fontsize=size)
    writer.write_text(page, color=color)


def normalize_font_name(name: str) -> str:
    """'ABCDEF+Nimbus Roman-Regular' -> 'nimbusromanregular': no subset
    prefix, case or punctuation, so the names a PDF's font objects use and
    the names text extraction reports can be compared."""
    name = re.sub(r"[^a-z0-9]", "", _SUBSET_PREFIX.sub("", name).lower())
    # "Arial Unicode MS Regular" (as declared) vs "ArialUnicodeMS" (as
    # extracted): a trailing "Regular" carries no information.
    return name[:-len("regular")] if name.endswith("regular") and name != "regular" else name


# Where a font for new characters came from, best first.
ORIGINAL = "original"    # the run's own font in the PDF
DOCUMENT = "document"    # the same font, embedded elsewhere in the PDF
INSTALLED = "installed"  # the same font, installed on this Mac
SIMILAR = "similar"      # the closest-looking installed font
STANDARD = "standard"    # Helvetica, Times or Courier: the last resort
SAME_TYPEFACE = (ORIGINAL, DOCUMENT, INSTALLED)


def same_font_name(a: str, b: str, allow_generic: bool = False) -> bool:
    """Whether two names for a font mean the same face: "ArialMT",
    "Arial Regular" and "ABCDEF+Arial" do; "Arial-BoldMT" doesn't.

    Generic names ("Regular", "Bold") match nothing, unless
    ``allow_generic``, when they match exactly the same name (for a font on
    the same page that must also draw the run's characters)."""
    if fontmatch.name_keys(a) & fontmatch.name_keys(b):
        return True
    return allow_generic and normalize_font_name(a) == normalize_font_name(b) != ""


def metrics_agree(a: pymupdf.Font, b: pymupdf.Font, chars: str) -> bool:
    """Whether two fonts are plausibly subsets of one typeface: the
    characters both have get the same widths, and (with none in common)
    the fonts' ascender and descender match."""
    shared = [c for c in dict.fromkeys(chars)
              if not c.isspace() and a.has_glyph(ord(c)) and b.has_glyph(ord(c))]
    if not shared:
        return (abs(a.ascender - b.ascender) <= 0.01
                and abs(a.descender - b.descender) <= 0.01)
    return all(abs(a.glyph_advance(ord(c)) - b.glyph_advance(ord(c))) <= 0.005
               for c in shared)


@dataclass(frozen=True)
class FontChoice:
    """A font new text can be written with."""

    font: pymupdf.Font = field(compare=False)
    label: str                  # human-readable name
    original: bool              # is this the run's own font?
    key: str                    # identity, for caching
    check_ink: bool             # subset fonts need the ink check
    kind: str = ORIGINAL
    # An installed font: written as a subset of this face (see fontmatch).
    face: Optional[Face] = field(default=None, compare=False)

    @property
    def same_typeface(self) -> bool:
        """Drawn in the run's own typeface (no visible substitution)."""
        return self.kind in SAME_TYPEFACE

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
                      original=False, key=f"base14:{code}", check_ink=False,
                      kind=STANDARD)


def installed_choice(face: Face, kind: str) -> FontChoice:
    # No ink or round-trip probe: an installed font is complete, and it's
    # written as a subset of just the typed characters, so two characters
    # sharing a glyph (space and no-break space) can't be mixed up unless
    # both are typed at once, which read-back verification catches.
    return FontChoice(font=fontmatch.face_font(face), label=face.label,
                      original=False, key=f"installed:{face.key}",
                      check_ink=False, kind=kind, face=face)


# Lookalike searches are slowish (up to a second), so remember them.
_SIMILAR_CACHE: Dict[tuple, Optional[Face]] = {}


class FontChain:
    """The fonts to try for each new character of a run, best first:

    1. the run's own font,
    2. the same font embedded elsewhere in the document (another subset of
       it may have the character),
    3. the same font installed on this Mac,
    4. the closest-looking installed font,
    5. the closest standard font (Helvetica, Times or Courier).

    Steps 2 to 4 are only worked out if an earlier one lacks a character.
    """

    def __init__(self, doc: pymupdf.Document, page: pymupdf.Page, run: TextRun,
                 original: Optional[FontChoice],
                 runs: Optional[List[TextRun]] = None):
        self.doc, self.page, self.run, self.runs = doc, page, run, runs
        self.original = original
        self._stages = [self._document_fonts, self._installed, self._similar]
        self._choices: List[FontChoice] = [original] if original else []
        self._fallback = fallback_font(run)

    def choices(self):
        yield from self._choices
        while self._stages:
            found = self._stages.pop(0)()
            self._choices.extend(found)
            yield from found
        yield self._fallback

    def choose(self, ch: str) -> Optional[FontChoice]:
        for choice in self.choices():
            if choice.can_draw(ch):
                return choice
        return None

    # -- The stages -------------------------------------------------------

    def _document_fonts(self) -> List[FontChoice]:
        found = []
        for name, choice in _embedded_fonts(self.doc):
            if self.original is not None and choice.key == self.original.key:
                continue
            if not same_font_name(name, self.run.font):
                continue
            # Same name isn't proof: where both can draw the run's text,
            # the widths must agree too.
            if self.original is not None and not metrics_agree(
                    self.original.font, choice.font,
                    string.ascii_letters + string.digits + self.run.text):
                continue
            found.append(FontChoice(font=choice.font, label=self.run.font,
                                    original=False, key=choice.key,
                                    check_ink=True, kind=DOCUMENT))
        return found

    def _installed_face(self) -> Optional[Face]:
        found = fontmatch.default_index().match(self.run.font, self.run.bold,
                                                self.run.italic)
        if found is None:
            return None
        face, by_full_name = found
        # A match on the family name alone must also have the right weight
        # and slant (a full-name match like "Arial-Black" names the face).
        if not by_full_name and (face.italic != self.run.italic
                                 or (face.weight >= 600) != self.run.bold):
            return None
        return face

    def _installed(self) -> List[FontChoice]:
        face = self._installed_face()
        return [installed_choice(face, INSTALLED)] if face else []

    def _similar(self) -> List[FontChoice]:
        face = similar_face(self.doc, self.page, self.run, self.original,
                            self.runs)
        return [installed_choice(face, SIMILAR)] if face else []


def _embedded_fonts(doc: pymupdf.Document) -> List[Tuple[str, FontChoice]]:
    """Every reusable embedded font in the document, under each name it
    goes by (the name the PDF declares, and the font program's own).
    Worked out once per document: an undo or redo loads a new document
    object, so this can't go stale across them."""
    cached = getattr(doc, "_pdfeditor_embedded_fonts", None)
    if cached is not None:
        return cached
    found, seen = [], set()
    for pno in range(len(doc)):
        for xref, ext, _type, basefont, *_ in doc.get_page_fonts(pno, full=True):
            if xref in seen or ext in ("", "n/a"):
                continue
            seen.add(xref)
            choice = _embedded_choice(doc, xref, basefont, DOCUMENT)
            if choice is None:
                continue
            for name in dict.fromkeys((basefont, choice.font.name)):
                found.append((name, choice))
    try:
        doc._pdfeditor_embedded_fonts = found
    except AttributeError:
        pass
    return found


def _embedded_choice(doc: pymupdf.Document, xref: int, label: str,
                     kind: str) -> Optional[FontChoice]:
    try:
        content = embedded_font_program(doc, xref)
        if content is None:
            return None
        font = pymupdf.Font(fontbuffer=content)
    except Exception:
        return None
    if not font.is_writable:
        return None
    return FontChoice(font=font, label=label, original=kind == ORIGINAL,
                      key=hashlib.sha1(content).hexdigest(), check_ink=True,
                      kind=kind)


def embedded_font_program(doc: pymupdf.Document, xref: int) -> Optional[bytes]:
    """The font program embedded for font ``xref``, ready to write with.

    PDFs from browsers and many other generators embed TrueType subsets
    whose own character map is missing characters the page plainly shows
    (dashes, bullets...): the PDF finds the glyphs by number and says what
    they are in a separate /ToUnicode map. Such a font is given a
    character map rebuilt from that, so it can draw everything the page
    already uses."""
    try:
        _name, ext, _ftype, content = doc.extract_font(xref)
    except Exception:
        return None
    if not content or ext in ("", "n/a"):
        return None
    if ext == "ttf":
        content = _with_page_cmap(doc, xref, content) or content
    return content


_HEX = re.compile(r"<([0-9A-Fa-f]+)>")


def _tounicode(doc: pymupdf.Document, xref: int) -> Dict[int, str]:
    """A Type0 font's /ToUnicode map: character code -> one character."""
    kind, value = doc.xref_get_key(xref, "ToUnicode")
    if kind != "xref":
        return {}
    try:
        cmap = doc.xref_stream(int(value.split()[0])).decode("latin-1")
    except Exception:
        return {}

    def char(hexstr: str) -> Optional[str]:
        try:
            text = bytes.fromhex(hexstr).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return None
        # One printable character: not ligatures, and not U+0000, which
        # some writers (Chrome) use for "unknown, see ActualText".
        return text if len(text) == 1 and text >= " " else None

    mapping: Dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", cmap, re.S):
        codes = _HEX.findall(block)
        for code, target in zip(codes[0::2], codes[1::2]):
            ch = char(target)
            if ch:
                mapping[int(code, 16)] = ch
    for block in re.findall(r"beginbfrange(.*?)endbfrange", cmap, re.S):
        for line in block.strip().splitlines():
            parts = _HEX.findall(line)
            if len(parts) < 3:
                continue
            low, high = int(parts[0], 16), int(parts[1], 16)
            if "[" in line:                          # <lo> <hi> [<u1> <u2> ...]
                for code, target in zip(range(low, high + 1), parts[2:]):
                    ch = char(target)
                    if ch:
                        mapping[code] = ch
            elif high - low < 0x10000:
                start = int(parts[2], 16)
                for offset in range(high - low + 1):
                    point = start + offset
                    if point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF:
                        mapping[low + offset] = chr(point)
    return mapping


_CONTENT_TOKENS = re.compile(
    rb"/([^\s/<>\[\]()]+)\s+[-\d.]+\s+Tf"                         # font
    rb"|/ActualText\s*(?:<([0-9A-Fa-f\s]*)>|\(((?:\\.|[^\\)])*)\))"  # span text
    rb"|\bEMC\b"                                                 # span end
    rb"|<([0-9A-Fa-f]{4})>\s*Tj")                                 # one glyph


def _actualtext_glyphs(doc: pymupdf.Document) -> Dict[int, Dict[int, str]]:
    """Glyphs the page content says the character of with ActualText
    (font xref -> {glyph code: character}). Chrome and Skia mark each glyph
    they have no Unicode for that way, instead of in /ToUnicode. Worked
    out once per document."""
    cached = getattr(doc, "_pdfeditor_actualtext", None)
    if cached is not None:
        return cached
    found: Dict[int, Dict[int, str]] = {}
    for page in doc:
        try:
            names = {f[4]: f[0] for f in page.get_fonts(full=True)}
            content = b"".join(doc.xref_stream(x) or b"" for x in page.get_contents())
        except Exception:
            continue
        font, text, glyphs = None, None, []
        for m in _CONTENT_TOKENS.finditer(content):
            if m.group(1) is not None:
                font = names.get(m.group(1).decode("latin-1"))
            elif m.group(2) is not None or m.group(3) is not None:
                text = _pdf_text_string(m.group(2), m.group(3))
                glyphs = []
            elif m.group(4) is not None:
                if text is not None:
                    glyphs.append((font, int(m.group(4), 16)))
            else:                                       # EMC
                if text is not None and len(text) == 1 and text >= " " \
                        and len(glyphs) == 1 and glyphs[0][0] is not None:
                    xref, code = glyphs[0]
                    found.setdefault(xref, {})[code] = text
                text, glyphs = None, []
    try:
        doc._pdfeditor_actualtext = found
    except AttributeError:
        pass
    return found


def _pdf_text_string(hex_part: Optional[bytes], literal: Optional[bytes]) -> Optional[str]:
    """A PDF text string (UTF-16 with a byte-order mark, or PDFDocEncoding,
    taken as Latin-1) from its hex or literal form."""
    try:
        if hex_part is not None:
            raw = bytes.fromhex(hex_part.decode("ascii").replace(" ", "").replace("\n", ""))
        else:
            raw = re.sub(rb"\\([0-7]{1,3}|.)", lambda m: (
                bytes([int(m.group(1), 8) & 0xFF]) if m.group(1)[:1].isdigit()
                else {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                      b"f": b"\f"}.get(m.group(1), m.group(1))), literal)
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be")
        return raw.decode("latin-1")
    except (ValueError, UnicodeDecodeError):
        return None


def _with_page_cmap(doc: pymupdf.Document, xref: int,
                    content: bytes) -> Optional[bytes]:
    """``content`` (a TrueType font program) with characters from the
    font's /ToUnicode added to its character map, for an Identity-encoded
    CIDFontType2 whose CIDs are glyph numbers. None if there's nothing to
    add or it can't be done safely."""
    if doc.xref_get_key(xref, "Encoding")[1] not in ("/Identity-H",):
        return None
    kind, value = doc.xref_get_key(xref, "DescendantFonts")
    refs = re.findall(r"(\d+) 0 R", value) if kind in ("array", "xref") else []
    if kind == "xref":
        refs = re.findall(r"(\d+) 0 R", doc.xref_object(int(refs[0])))
    if not refs:
        return None
    descendant = int(refs[0])
    if doc.xref_get_key(descendant, "Subtype")[1] != "/CIDFontType2":
        return None
    if doc.xref_get_key(descendant, "CIDToGIDMap")[1] not in ("/Identity", "null"):
        return None                         # a CID -> glyph table: not handled
    by_code = _tounicode(doc, xref)
    for code, ch in _actualtext_glyphs(doc).get(xref, {}).items():
        by_code.setdefault(code, ch)
    try:
        font = TTFont(io.BytesIO(content))
        order = font.getGlyphOrder()
        existing = (font.getBestCmap() or {}) if "cmap" in font else {}
        added = {}
        # 1. What the PDF says each glyph it uses is.
        for gid, ch in by_code.items():
            if 0 < gid < len(order) and ord(ch) not in existing:
                added.setdefault(ord(ch), order[gid])
        # 2. Glyphs named for their character ("endash", "uni2013"), as
        #    MuPDF itself reads them when /ToUnicode doesn't say. Subsets
        #    often keep every glyph but only a few character map entries.
        named = set(existing.values()) | set(added.values())
        for name in order[1:]:
            if name in named:
                continue
            text = agl.toUnicode(name)
            if len(text) == 1 and ord(text) not in existing and ord(text) not in added:
                added[ord(text)] = name
        if not added:
            return None
        if "cmap" not in font:
            font["cmap"] = newTable("cmap")
            font["cmap"].tableVersion = 0
            font["cmap"].tables = []
        mapping = dict(existing)
        mapping.update(added)
        tables = []
        for platform, encoding, fmt in ((3, 1, 4), (3, 10, 12)):
            table = CmapSubtable.newSubtable(fmt)
            table.platformID, table.platEncID, table.language = platform, encoding, 0
            table.cmap = ({k: v for k, v in mapping.items() if k <= 0xFFFF}
                          if fmt == 4 else dict(mapping))
            tables.append(table)
        font["cmap"].tables = tables
        out = io.BytesIO()
        font.save(out)
        return out.getvalue()
    except Exception:
        return None


def _pdf_widths(doc: pymupdf.Document, page: pymupdf.Page,
                run: TextRun) -> Tuple[Tuple[str, float], ...]:
    """The character widths a PDF records for a font it doesn't embed
    (simple fonts, single-byte codes), as (char, width in ems)."""
    for xref, _ext, _type, basefont, *_ in page.get_fonts(full=True):
        if not same_font_name(basefont, run.font):
            continue
        try:
            first = int(doc.xref_get_key(xref, "FirstChar")[1])
            kind, value = doc.xref_get_key(xref, "Widths")
            if kind == "xref":
                value = doc.xref_object(int(value.split()[0]))
            widths = [float(w) for w in re.findall(r"-?[\d.]+", value)]
        except (ValueError, IndexError, RuntimeError):
            continue
        out = {}
        for ch in run.text:
            code = ord(ch)
            if ch.isalnum() and 0 <= code - first < len(widths) and code < 128:
                out[ch] = widths[code - first] / 1000
        return tuple(out.items())
    return ()


def similar_face(doc: pymupdf.Document, page: pymupdf.Page, run: TextRun,
                 original: Optional[FontChoice] = None,
                 runs: Optional[List[TextRun]] = None) -> Optional[Face]:
    """The installed font that looks most like ``run``'s (see fontmatch)."""
    reference_font = original.font if original is not None else None
    widths = () if reference_font is not None else _pdf_widths(doc, page, run)
    # An embedded font is identified by its content; one that isn't
    # embedded by its name *and* the widths the PDF records for it (two
    # PDFs can use one name for different fonts).
    key = (original.key if original else normalize_font_name(run.font),
           widths, run.bold, run.italic, run.monospaced, run.serif)
    if key not in _SIMILAR_CACHE:
        if runs is None:
            runs = page_runs(page, run.page_num)
        chars = "".join(r.text for r in runs if same_font_name(r.font, run.font))
        if reference_font is not None:
            chars = "".join(c for c in chars + run.text
                            if reference_font.has_glyph(ord(c)))
        ref = fontmatch.Reference(
            font=reference_font, chars=chars or run.text,
            widths=widths,
            bold=run.bold, italic=run.italic, mono=run.monospaced, name=run.font,
            # Flags often leave "serif" unset, so a no means "don't know",
            # unless the name says sans.
            serif=(True if run.serif
                   else False if fontmatch.looks_sans(run.font) else None))
        ranked = fontmatch.closest_faces(ref, fontmatch.default_index())
        _SIMILAR_CACHE[key] = ranked[0][1] if ranked else None
    return _SIMILAR_CACHE[key]


def resolve_original_font(doc: pymupdf.Document, page: pymupdf.Page,
                          run: TextRun) -> Tuple[Optional[FontChoice], str]:
    """The run's own font, ready to write with, or (None, reason).

    Several fonts on a page can share a name (e.g. two different subsets of
    the same typeface), so a candidate only counts if it can draw every
    character the run already shows in that font.
    """
    seen = set()
    found_embedded = False
    best, best_count = None, 0
    wanted = [c for c in dict.fromkeys(run.primary_text) if not c.isspace()]
    for xref, _ext, _type, basefont, *_ in page.get_fonts(full=True):
        if xref in seen:
            continue
        seen.add(xref)
        content = embedded_font_program(doc, xref)
        if content is None:
            continue
        try:
            font = pymupdf.Font(fontbuffer=content)
        except Exception:
            continue
        # Text extraction reports the embedded font program's own name,
        # which needn't match the name the PDF declares for it (e.g. a
        # font declared "Helvetica" whose program is "NimbusSans-Regular"),
        # so accept a match on either.
        if not (same_font_name(run.font, basefont, allow_generic=True)
                or same_font_name(run.font, font.name, allow_generic=True)):
            continue
        found_embedded = True
        if not font.is_writable:
            continue
        choice = FontChoice(font=font, label=run.font, original=True,
                            key=hashlib.sha1(content).hexdigest(),
                            check_ink=True, kind=ORIGINAL)
        count = sum(1 for c in wanted if choice.has_char(c))
        if count == len(wanted):
            return choice, ""
        # Keep the candidate that draws the most of the run's characters:
        # a font missing one or two (often punctuation) is still the run's
        # font for everything else, and only the rest needs another font.
        if count > best_count:
            best, best_count = choice, count

    if best is not None:
        return best, ""
    if not found_embedded:
        code = STANDARD_14.get(normalize_font_name(run.font))
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
    substitute_label: str = ""           # lookalike font(s), if any are used
    substituted: List[str] = field(default_factory=list)   # in a lookalike
    whole_run_reason: str = ""           # set if the original font is unusable
    # Set when the whole run is rewritten in the same font, installed on
    # this Mac or embedded elsewhere in the PDF (no warning needed).
    same_font_label: str = ""
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
        if self.whole_run_reason and self.new_text.strip() and self.substituted:
            out.append(f"{self.whole_run_reason[0].upper()}"
                       f"{self.whole_run_reason[1:]}, and it isn't installed "
                       f"on this Mac, so this text will be rewritten in "
                       f"{self.substitute_label}, the closest match.")
        elif self.substituted:
            chars = " ".join(self.substituted)
            out.append(f"These characters can't be drawn with the PDF's copy "
                       f"of “{self.run.font}” (PDFs usually include only the "
                       f"characters a document already uses), and it isn't "
                       f"installed on this Mac, so they'll use "
                       f"{self.substitute_label}, the closest match: {chars}")
        if self.overlaps:
            others = ", ".join(f"“{t}”" for t in self.overlaps)
            out.append(f"The new text is wider than the old and will "
                       f"overlap {others} on the same line.")
        return out


def _unique(chars) -> List[str]:
    return list(dict.fromkeys(chars))


PENDING_REDACTIONS_ERROR = (
    "This page has unapplied redaction marks. Changing it would apply them "
    "too, so apply or remove them in a redaction tool first.")


def has_pending_redactions(page: pymupdf.Page) -> bool:
    return any(True for _ in page.annots(types=[pymupdf.PDF_ANNOT_REDACT]))


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
    if has_pending_redactions(page):
        plan.errors.append(PENDING_REDACTIONS_ERROR)
    if touches_annotation(page, run.rect):
        plan.errors.append(ANNOTATION_TEXT_ERROR)
    if plan.errors:
        return plan

    if runs is None:
        runs = page_runs(page, run.page_num)
    original, reason = resolve_original_font(doc, page, run)
    plan.whole_run_reason = reason
    chain = FontChain(doc, page, run, original, runs)

    lookalikes = []
    for ch in new_text:
        choice = chain.choose(ch)
        if choice is None:
            plan.unsupported.append(ch)
            continue
        if not choice.same_typeface and not ch.isspace():
            plan.substituted.append(ch)
            lookalikes.append(choice.label)
        if plan.segments and plan.segments[-1].choice.key == choice.key:
            plan.segments[-1].text += ch
        else:
            plan.segments.append(Segment(text=ch, choice=choice))

    plan.substituted = _unique(plan.substituted)
    plan.unsupported = _unique(plan.unsupported)
    plan.substitute_label = " and ".join(_unique(lookalikes))
    labels = Counter()
    for segment in plan.segments:
        labels[segment.choice.label] += len(segment.text)
    plan.font_label = (labels.most_common(1)[0][0] if labels
                       else original.label if original else run.font)
    if reason and not plan.substituted:
        same = [s.choice.label for s in plan.segments if s.choice.same_typeface]
        plan.same_font_label = same[0] if same else ""
    if plan.unsupported:
        plan.errors.append(
            "None of the available fonts can draw these characters: "
            + " ".join(plan.unsupported))
        return plan

    plan.new_width = sum(s.choice.font.text_length(s.text, fontsize=run.size)
                         for s in plan.segments)
    new_right = run.origin[0] + plan.new_width
    for other in runs:
        if (other.is_horizontal and not other.same_as(run)
                and abs(other.origin[1] - run.origin[1]) <= SAME_LINE_TOLERANCE
                and other.bbox[0] >= run.bbox[2] - 0.5
                and new_right > other.bbox[0] + 0.5):
            plan.overlaps.append(other.text.strip())
    return plan


def writing_fonts(segments: List[Segment]) -> List[Tuple[str, pymupdf.Font]]:
    """(text, font) pieces to write. An installed font is written as a
    subset holding just the characters this edit uses, not the whole font.

    Characters that share one glyph in a font (hyphen and soft hyphen,
    space and no-break space) go into separate subsets, so each extracts
    as itself when copied."""
    # For each installed font: the character groups, each one subset.
    groups: Dict[str, List[Dict[int, str]]] = {}
    group_of: Dict[Tuple[str, str], int] = {}
    for segment in segments:
        choice = segment.choice
        if choice.face is None:
            continue
        for ch in segment.text:
            if (choice.key, ch) in group_of:
                continue
            glyph = choice.font.has_glyph(ord(ch))
            sets = groups.setdefault(choice.key, [])
            index = next((i for i, g in enumerate(sets)
                          if glyph not in g or g[glyph] == ch), len(sets))
            if index == len(sets):
                sets.append({})
            sets[index][glyph] = ch
            group_of[(choice.key, ch)] = index
    subsets: Dict[Tuple[str, int], pymupdf.Font] = {}
    for segment in segments:
        if segment.choice.face is not None:
            for index, group in enumerate(groups[segment.choice.key]):
                subsets[(segment.choice.key, index)] = None
    faces = {s.choice.key: s.choice.face for s in segments if s.choice.face is not None}
    for (key, index) in subsets:
        subsets[(key, index)] = fontmatch.subset_font(
            faces[key], "".join(groups[key][index].values()))

    pieces: List[Tuple[str, pymupdf.Font]] = []
    for segment in segments:
        choice = segment.choice
        for ch in segment.text:
            font = (subsets[(choice.key, group_of[(choice.key, ch)])]
                    if choice.face is not None else choice.font)
            if pieces and pieces[-1][1] is font:
                pieces[-1] = (pieces[-1][0] + ch, font)
            else:
                pieces.append((ch, font))
    return pieces


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
    apply_edits(page, [plan])


def apply_edits(page: pymupdf.Page, plans: List[EditPlan]) -> None:
    """Replace several runs on one page at once (Replace All), with one
    removal pass and one verification for them all, as strict as for a
    single edit: every other character on the page must be unchanged, and
    each new text must read back where it was written."""
    for plan in plans:
        if not plan.can_apply:
            raise EditError(plan.errors[0])
    plans = [p for p in plans if not p.is_noop]
    if not plans:
        return
    page_num = plans[0].run.page_num
    current = page_runs(page, page_num)
    for plan in plans:
        if not any(r.same_as(plan.run) for r in current):
            raise EditError("That text has changed since it was selected. "
                            "Click it again to edit it.")

    expected_after_removal = _char_counts(page)
    for plan in plans:
        expected_after_removal -= _counted(plan.run.text)
    annotations = annotation_inventory(page)

    for plan in plans:
        page.add_redact_annot(removal_rect(plan.run), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    check_annotations_kept(page, annotations)
    # Every line that isn't being replaced must still be there, exactly
    # as it was. (Counting the page's characters alone could let one
    # wrongly removed character hide one that should have gone but
    # didn't.)
    after_removal = page_runs(page, page_num)
    untouched = [r for r in current
                 if not any(r.same_as(p.run) for p in plans)]
    if (_char_counts(page) != expected_after_removal
            or not all(any(r.same_as(a) for a in after_removal) for r in untouched)):
        raise EditError("Removing the old text would also have changed "
                        "nearby text, so the edit was cancelled.")

    expected = expected_after_removal
    for plan in plans:
        if plan.segments:
            write_text(page, plan.run.origin, writing_fonts(plan.segments),
                       plan.run.size, plan.run.color)
        expected += _counted(plan.new_text)

    runs_after = page_runs(page, page_num)
    if (_char_counts(page) != expected
            or not all(_reads_back(page, plan, runs_after) for plan in plans)):
        raise EditError("The new text couldn't be verified after writing "
                        "it, so the edit was cancelled.")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _reads_back(page: pymupdf.Page, plan: EditPlan,
                runs: Optional[List[TextRun]] = None) -> bool:
    """Whether the new text extracts, in order, on the run's baseline where
    it was written. (Character counts alone can't see reordering.)"""
    wanted = _collapse(plan.new_text)
    if not wanted:
        return True
    x0 = plan.run.origin[0]
    x1 = x0 + plan.new_width
    if runs is None:
        runs = page_runs(page, plan.run.page_num)
    for run in runs:
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
    """How new text looks: one of the standard fonts every PDF viewer has
    (FAMILIES), or any installed font family (embedded as a subset)."""

    family: str = "Helvetica"
    size: float = 12.0
    bold: bool = False
    italic: bool = False
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def is_standard(self) -> bool:
        return self.family in FAMILIES

    @property
    def font_code(self) -> str:
        return FAMILIES[self.family][(1 if self.bold else 0) + (2 if self.italic else 0)]

    @property
    def face(self) -> Optional[Face]:
        """The installed face for a non-standard family."""
        if self.is_standard:
            return None
        return fontmatch.pick_style(
            fontmatch.default_index().family_faces(self.family),
            self.bold, self.italic)

    @property
    def label(self) -> str:
        if self.is_standard:
            return FONT_LABELS[self.font_code]
        face = self.face
        return face.label if face else self.family


@dataclass
class AddPlan:
    page_num: int
    top_left: Tuple[float, float]      # where the text box was placed
    text: str
    style: TextStyle
    origin: Tuple[float, float] = (0.0, 0.0)   # baseline start
    width: float = 0.0
    errors: List[str] = field(default_factory=list)
    face: Optional[Face] = None        # installed font, if not a standard one

    @property
    def can_apply(self) -> bool:
        return not self.errors and bool(self.text.strip())


MIN_SIZE, MAX_SIZE = 4.0, 144.0


def plan_add_text(page: pymupdf.Page, page_num: int,
                  top_left: Tuple[float, float], text: str,
                  style: TextStyle) -> AddPlan:
    """Plan writing ``text`` with its top-left corner at ``top_left``."""
    plan = AddPlan(page_num=page_num, top_left=top_left, text=text, style=style)
    if style.is_standard:
        font = pymupdf.Font(style.font_code)
    else:
        plan.face = style.face
        if plan.face is None:
            plan.errors.append(f"The font “{style.family}” isn't available.")
            return plan
        font = fontmatch.face_font(plan.face)
    if not (math.isfinite(style.size) and MIN_SIZE <= style.size <= MAX_SIZE):
        plan.errors.append(f"Font size must be between {MIN_SIZE:g} and "
                           f"{MAX_SIZE:g} pt.")
    if page.rotation:
        plan.errors.append("Adding text to rotated pages isn't supported yet.")
    if any(c in text for c in "\r\n\t") or any(ord(c) < 32 for c in text):
        plan.errors.append("Line breaks, tabs and control characters "
                           "aren't supported in added text.")
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
    if plan.face is not None:
        pieces = writing_fonts([Segment(plan.text, installed_choice(plan.face, INSTALLED))])
    else:
        pieces = [(plan.text, pymupdf.Font(plan.style.font_code))]
    write_text(page, plan.origin, pieces, plan.style.size, plan.style.color)
    probe = EditPlan(run=TextRun(plan.page_num, "", "", plan.style.size, 0,
                                 plan.style.color, plan.origin, (0, 0, 0, 0),
                                 (1.0, 0.0)),
                     new_text=plan.text, new_width=plan.width)
    if _char_counts(page) != expected or not _reads_back(page, probe):
        raise EditError("The new text couldn't be verified after writing "
                        "it, so nothing was added.")
