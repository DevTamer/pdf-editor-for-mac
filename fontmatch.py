"""Finding the installed font that best matches a font in a PDF.

PDFs usually embed only the characters a document already uses, so typing
a new character often needs another font. This module finds the best one
on this Mac, in order:

1. **The same font, installed.** Matched by name, so "ABCDEF+Arial-BoldMT"
   finds Arial Bold, "TimesNewRomanPSMT" finds Times New Roman, and
   "Calibri" finds the copy that comes with Microsoft Office.
2. **The closest-looking installed font**, when the PDF's font isn't
   installed. Candidates of the right kind (serif or not, monospaced or
   not, italic or not) are first narrowed down by x-height and cap height,
   then the best few are compared glyph by glyph: each character the PDF's
   font can draw is rendered in both fonts at the same size and position,
   and the fonts whose letters overlap most, with the closest widths, win.
   If the PDF's font isn't embedded at all, the widths the PDF records for
   it are compared instead.

New text is never written with a whole installed font: only the characters
it needs are embedded (see ``subset_font``), so a 20 MB font adds a few KB.
Fonts whose licence forbids embedding, or subsetting, are never used.

No Tk here. Reading font files uses fontTools.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf
from PIL import Image, ImageChops

# fontTools logs a warning for every slightly odd table in a system font.
logging.getLogger("fontTools").setLevel(logging.ERROR)

from fontTools.subset import Options, Subsetter  # noqa: E402
from fontTools.ttLib import TTCollection, TTFont  # noqa: E402

FONT_DIRS = (
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    # Calibri, Cambria, Aptos and the rest of Office's fonts aren't
    # installed system-wide; Word keeps its own copies.
    "/Applications/Microsoft Word.app/Contents/Resources/DFonts",
    "/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts",
    "/Applications/Microsoft Excel.app/Contents/Resources/DFonts",
)
FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc")
CACHE_PATH = os.path.expanduser(
    "~/Library/Caches/com.devtamer.pdfeditor/font-index-v4.json")

# OS/2 fsType bits (embedding licence).
FS_RESTRICTED = 0x0002     # may not be embedded at all
FS_NO_SUBSETTING = 0x0100  # may only be embedded whole
FS_BITMAP_ONLY = 0x0200

# Fonts made for another script that include Latin letters only as an
# extra. (Unicode range bits can't tell: plenty of Latin fonts also cover
# Greek, Hebrew or Arabic.)
OTHER_SCRIPT_WORDS = (
    "devanagari", "bangla", "bengali", "gurmukhi", "gujarati", "oriya",
    "tamil", "telugu", "kannada", "malayalam", "sinhala", "kohinoor",
    "mukta", "myanmar", "khmer", "lao", "thai", "tibet", "arabic", "hebrew",
    "naskh", "nastaliq", "kufi", "farah", "geeza", "baghdad", "nadeem",
    "diwan", "raanana", "corsiva", "gothic neo", "hiragino", "pingfang",
    "heiti", "songti", "kaiti", "hei", "mincho", "gothic", "ming", "yuan",
    "batang", "gulim", "dotum", "nanum", "apple sd", "applemyungjo",
    "osaka", "toppan", "tsukushi", "klee", "yu ", "sukhumvit", "silom",
    "ayuthaya", "krungthep", "mshtakan", "noto sans", "noto serif",
    "sathu", "inaimathi", "galvji", "stix", "symbol", "webdings",
    "wingdings", "mt extra", "bodoni ornaments", "zapf dingbats",
    "mongolian", "tai le", "tai lue", "dubai", "meiryo", "yi baiti",
    "phagspa", "himalaya", "ebrima", "nirmala", "gadugi", "javanese",
    "leelawadee", "sylfaen", "estrangelo", "euphemia", "plantagenet",
    "aparajita", "kokila", "utsaah", "vani", "vijaya", "shruti", "tunga",
    "kartika", "latha", "mangal", "raavi", "gautami", "kalinga", "vrinda",
    "iskoola", "daunpenh", "moolboran", "dokchampa", "angsana", "browallia",
    "cordia", "dilleniaupc", "eucrosia", "freesia", "iris", "jasmine",
    "kodchiang", "lily", "simsun", "simhei", "fangsong", "microsoft yahei",
    "microsoft jhenghei", "dengxian", "malgun", "ms gothic", "ms mincho",
    "ms pgothic", "ms pmincho", "ms ui gothic")

WEIGHT_TOLERANCE = 150      # usWeightClass difference allowed for lookalikes
SHORTLIST = 24            # candidates compared glyph by glyph
SAMPLE_LIMIT = 48          # characters compared
GLYPH_SIZE = 48            # points, when comparing glyphs
CELL = 72                  # points per glyph cell


@dataclass(frozen=True)
class Face:
    """One installed typeface (a file, or one face of a .ttc collection)."""

    path: str
    index: int               # face number inside a .ttc, else 0
    postscript: str
    family: str
    style: str
    full: str
    weight: int
    italic: bool
    mono: bool
    serif: Optional[bool]    # None: the font doesn't say
    latin: bool              # has Basic Latin
    other_script: bool       # mainly for another script (CJK, Indic...)
    usable: bool             # licence allows embedding a subset
    x_height: float          # fractions of the em, 0 if unknown
    cap_height: float
    variable: bool

    @property
    def key(self) -> str:
        return f"{self.path}#{self.index}"

    @property
    def label(self) -> str:
        return self.full or f"{self.family} {self.style}".strip()


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _SUBSET_PREFIX.sub("", name or "").lower())


# Names that say nothing about the typeface (some fonts only carry their
# real names in records a careless subsetter drops). Never match on these.
GENERIC_NAMES = {"", "regular", "bold", "italic", "bolditalic", "oblique",
                 "boldoblique", "roman", "normal", "medium", "light", "book",
                 "plain", "null", "untitled", "font"}


def name_keys(name: str) -> set:
    """Spellings a font name can be matched on. "TimesNewRomanPS-BoldMT",
    "Times New Roman Bold" and "TimesNewRoman,Bold" all give
    "timesnewromanbold"; "ArialMT" and "Arial" give "arial". A generic
    name ("Regular", "Bold", "(null)") gives nothing."""
    key = _norm(name)
    if key in GENERIC_NAMES:
        return set()
    keys = {key}
    for suffix in ("regular", "roman", "mt", "psmt", "ps", "std", "pro"):
        if key.endswith(suffix) and len(key) > len(suffix) + 2:
            keys.add(key[:-len(suffix)])
    # Times New Roman's PostScript names: TimesNewRomanPS-BoldItalicMT.
    keys |= {re.sub(r"ps(bold|italic|bolditalic|boldmt|italicmt|bolditalicmt)$",
                    r"\1", k) for k in set(keys)}
    keys |= {k[:-2] for k in set(keys) if k.endswith("mt") and len(k) > 4}
    # "Oblique" and "Italic" mean the same thing here.
    keys |= {k.replace("oblique", "italic") for k in set(keys)}
    return keys - GENERIC_NAMES


SANS_WORDS = ("sans", "arial", "helvetica", "verdana", "tahoma", "calibri",
              "segoe", "trebuchet", "avenir", "futura", "gill", "myriad",
              "roboto", "lato", "inter", "aptos", "franklin", "frutiger",
              "univers", "geneva", "lucida grande", "lucidagrande", "corbel",
              "candara", "century gothic", "centurygothic", "optima", "sfpro",
              "sfui", "sfns", "grotesk", "grotesque")


def looks_sans(font_name: str) -> bool:
    """Whether a font's name says it's sans serif (PDF font flags only
    ever say "serif", never "sans")."""
    name = font_name.lower()
    return any(word in name or word in _norm(font_name) for word in SANS_WORDS)


# ---------------------------------------------------------------------------
# The index of installed fonts
# ---------------------------------------------------------------------------

def _read_faces(path: str) -> List[Face]:
    if path.lower().endswith((".ttc", ".otc")):
        fonts = TTCollection(path, lazy=True).fonts
    else:
        fonts = [TTFont(path, lazy=True)]
    faces = []
    for index, font in enumerate(fonts):
        try:
            faces.append(_describe(font, path, index))
        except Exception:
            continue
    return faces


def _other_script(family: str, os2) -> bool:
    name = family.lower()
    if any(word in name for word in OTHER_SCRIPT_WORDS):
        return True
    # No Latin-1 code page: not a Western text font.
    return bool(os2 is not None and getattr(os2, "version", 0) >= 1
                and not (os2.ulCodePageRange1 & 1))


def _describe(font: TTFont, path: str, index: int) -> Face:
    names = font["name"]
    os2 = font["OS/2"] if "OS/2" in font else None
    head = font["head"]
    upm = float(head.unitsPerEm or 1000)
    fs_type = os2.fsType if os2 is not None else 0
    italic = bool((os2 is not None and os2.fsSelection & 1) or head.macStyle & 2)
    panose = getattr(os2, "panose", None) if os2 is not None else None
    serif = None
    if panose is not None and panose.bFamilyType == 2:     # Latin text
        if 2 <= panose.bSerifStyle <= 10:                  # cove ... triangle
            serif = True
        elif 11 <= panose.bSerifStyle <= 15:               # sans, flared, rounded
            serif = False
    style = names.getBestSubFamilyName() or ""
    return Face(
        path=path, index=index,
        postscript=names.getDebugName(6) or "",
        family=names.getBestFamilyName() or "",
        style=style,
        full=names.getBestFullName() or "",
        weight=int(os2.usWeightClass) if os2 is not None else 400,
        italic=italic or "italic" in style.lower() or "oblique" in style.lower(),
        mono=bool("post" in font and font["post"].isFixedPitch),
        serif=serif,
        latin=bool(os2 is None or os2.ulUnicodeRange1 & 1),
        other_script=_other_script(names.getBestFamilyName() or "", os2),
        # Embedding allowed, and real outlines (not colour bitmaps like
        # Apple Color Emoji).
        usable=(not (fs_type & (FS_RESTRICTED | FS_NO_SUBSETTING | FS_BITMAP_ONLY))
                and ("glyf" in font or "CFF " in font or "CFF2" in font)
                and not any(t in font for t in ("sbix", "CBDT"))),
        x_height=(getattr(os2, "sxHeight", 0) or 0) / upm if os2 is not None else 0.0,
        cap_height=(getattr(os2, "sCapHeight", 0) or 0) / upm if os2 is not None else 0.0,
        variable="fvar" in font,
    )


def _faces_from_cache(entry) -> Optional[List[Face]]:
    """A cached file's faces, or None if the entry is damaged (the file is
    then read again)."""
    try:
        return [Face(**face) for face in entry["faces"]]
    except (TypeError, KeyError):
        return None


class FontIndex:
    """The fonts installed on this Mac, scanned once and cached on disk
    (only new or changed files are read again)."""

    def __init__(self, dirs: Sequence[str] = FONT_DIRS,
                 cache_path: Optional[str] = CACHE_PATH):
        self.dirs = tuple(dirs)
        self.cache_path = cache_path
        self._faces: Optional[List[Face]] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def load_in_background(self) -> None:
        """Start scanning now, so the first edit that needs it doesn't wait."""
        if self._thread is None and self._faces is None:
            self._thread = threading.Thread(target=self.faces, daemon=True)
            self._thread.start()

    def faces(self) -> List[Face]:
        with self._lock:
            if self._faces is None:
                try:
                    self._faces = self._scan()
                except Exception:
                    # Never let font matching break editing: with no
                    # installed fonts, the standard fonts are used.
                    logging.getLogger(__name__).exception("font scan failed")
                    self._faces = []
            return self._faces

    def _scan(self) -> List[Face]:
        cached = self._read_cache()
        entries: Dict[str, dict] = {}
        for directory in self.dirs:
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                if not name.lower().endswith(FONT_EXTENSIONS):
                    continue
                path = os.path.join(directory, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                stamp = [stat.st_mtime, stat.st_size]
                old = cached.get(path)
                if old and old.get("stamp") == stamp:
                    faces = _faces_from_cache(old)
                    if faces is not None:
                        entries[path] = (stamp, faces)
                        continue
                try:
                    faces = _read_faces(path)
                except Exception:
                    faces = []
                entries[path] = (stamp, faces)
        self._write_cache({path: {"stamp": stamp, "faces": [asdict(f) for f in faces]}
                           for path, (stamp, faces) in entries.items()})
        return [face for _stamp, faces in entries.values() for face in faces]

    def _read_cache(self) -> Dict[str, dict]:
        if not self.cache_path:
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self, entries: Dict[str, dict]) -> None:
        if not self.cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(entries, fh)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass   # a cache we can't write just means scanning next time

    # -- Queries -----------------------------------------------------------

    def usable_faces(self) -> List[Face]:
        return [f for f in self.faces() if f.usable and f.latin and not f.variable]

    def exact(self, font_name: str, bold: bool = False,
              italic: bool = False) -> Optional[Face]:
        """The installed face with this name, if there is one."""
        found = self.match(font_name, bold, italic)
        return found[0] if found else None

    def match(self, font_name: str, bold: bool = False,
              italic: bool = False) -> Optional[Tuple[Face, bool]]:
        """(face, by_full_name), or None.

        Tries the full name first ("Arial-BoldMT" -> Arial Bold): that
        names the exact face (by_full_name True). Otherwise a bare family
        name ("Calibri", or "Arial,Bold" style names) picks the face of
        that family closest to ``bold`` and ``italic``; the caller should
        check it really has that weight and slant."""
        wanted = name_keys(font_name)
        if not wanted:
            return None
        faces = self.usable_faces()
        for face in faces:
            if wanted & (name_keys(face.postscript) | name_keys(face.full)):
                return face, True
        family = [f for f in faces if wanted & name_keys(f.family)]
        if not family:
            # "Arial,Bold": the family, plus a style
            base = font_name.split(",")[0].split("-")[0]
            if base != font_name:
                found = self.match(base, bold or "bold" in font_name.lower(),
                                   italic or any(s in font_name.lower()
                                                 for s in ("italic", "oblique")))
                return (found[0], False) if found else None
            return None
        return pick_style(family, bold, italic), False

    def family_faces(self, family: str) -> List[Face]:
        return [f for f in self.usable_faces() if f.family == family]

    def families(self) -> List[str]:
        return sorted({f.family for f in self.usable_faces()
                       if f.family and not f.family.startswith(".")},
                      key=str.lower)


def pick_style(faces: Iterable[Face], bold: bool, italic: bool) -> Optional[Face]:
    """The face closest to regular/bold and upright/italic."""
    target = 700 if bold else 400

    def distance(face: Face):
        return (face.italic != italic, abs(face.weight - target),
                "narrow" in face.style.lower() or "condensed" in face.style.lower(),
                len(face.style))
    faces = list(faces)
    return min(faces, key=distance) if faces else None


_DEFAULT_INDEX: Optional[FontIndex] = None


def default_index() -> FontIndex:
    global _DEFAULT_INDEX
    if _DEFAULT_INDEX is None:
        _DEFAULT_INDEX = FontIndex()
    return _DEFAULT_INDEX


def set_default_index(index: Optional[FontIndex]) -> None:
    """Tests use their own index of a few known fonts."""
    global _DEFAULT_INDEX
    _DEFAULT_INDEX = index
    load_face.cache_clear()


# ---------------------------------------------------------------------------
# Loading and subsetting
# ---------------------------------------------------------------------------

@lru_cache(maxsize=32)
def load_face(face: Face) -> bytes:
    """The face as a standalone font file (extracted from its .ttc)."""
    if not face.path.lower().endswith((".ttc", ".otc")):
        with open(face.path, "rb") as fh:
            return fh.read()
    font = TTCollection(face.path).fonts[face.index]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def face_font(face: Face) -> pymupdf.Font:
    """The face, loaded straight from its file (fast, even for a face
    inside a .ttc). For comparing and measuring; to write text with, use
    subset_font()."""
    fz = pymupdf.mupdf.fz_new_font_from_file(None, face.path, face.index, 0)
    font = pymupdf.Font.__new__(pymupdf.Font)
    font.this = fz
    return font


def subset_font(face: Face, text: str) -> pymupdf.Font:
    """A copy of ``face`` with only the characters in ``text``, to write
    new text with (so the PDF doesn't grow by the size of the whole font)."""
    font = TTFont(io.BytesIO(load_face(face)))
    options = Options()
    options.name_IDs = ["*"]          # keep the font's names...
    options.name_languages = ["*"]
    # ...including Mac-platform records: Helvetica, Menlo, Optima and many
    # other Apple fonts keep their names only there, and without them the
    # PDF would call the font "Regular" or "Bold".
    options.name_legacy = True
    options.notdef_outline = True
    options.layout_features = []
    options.hinting = False
    options.drop_tables += ["DSIG"]
    subsetter = Subsetter(options=options)
    subsetter.populate(unicodes={ord(c) for c in text})
    subsetter.subset(font)
    out = io.BytesIO()
    font.save(out)
    return pymupdf.Font(fontbuffer=out.getvalue())


# ---------------------------------------------------------------------------
# Finding the closest-looking font
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Reference:
    """What's known about the PDF's font, to compare installed fonts with."""

    font: Optional[pymupdf.Font]       # the embedded font program, if any
    chars: str                          # characters it's known to draw
    widths: Tuple[Tuple[str, float], ...] = ()   # (char, advance/em) from the PDF
    bold: bool = False
    italic: bool = False
    mono: bool = False
    serif: Optional[bool] = None


def ink_height(font: pymupdf.Font, ch: str) -> float:
    """How far ``ch`` reaches above the baseline, in ems, measured from its
    drawn ink. (PyMuPDF 1.24's Font.glyph_bbox gives the whole font's box
    for every glyph, so it can't be used for this.)"""
    if not font.has_glyph(ord(ch)):
        return 0.0
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=200, height=200)
        writer = pymupdf.TextWriter(page.rect)
        writer.append((20, 150), ch, font=font, fontsize=100)
        writer.write_text(page)
        pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
        image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()
    box = image.point(lambda v: 255 if v < 128 else 0).getbbox()
    return max(0.0, (150 - box[1]) / 100) if box else 0.0


def _reference_heights(ref: Reference) -> Tuple[float, float]:
    if ref.font is None:
        return 0.0, 0.0
    x_height = ink_height(ref.font, "x") if "x" in ref.chars else 0.0
    cap = ink_height(ref.font, "H") if "H" in ref.chars else 0.0
    return x_height, cap


def _sample(chars: str) -> str:
    seen = []
    for ch in chars:
        if ch.isalnum() and ch not in seen:
            seen.append(ch)
    letters = [c for c in seen if c.isalpha()]
    return "".join((letters + [c for c in seen if not c.isalpha()])[:SAMPLE_LIMIT])


def _render_cells(font: pymupdf.Font, chars: str) -> List[Image.Image]:
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=CELL * len(chars), height=CELL)
        writer = pymupdf.TextWriter(page.rect)
        for i, ch in enumerate(chars):
            if font.has_glyph(ord(ch)):
                writer.append((i * CELL + 8, CELL - 18), ch, font=font,
                              fontsize=GLYPH_SIZE)
        writer.write_text(page)
        pix = page.get_pixmap(dpi=36, colorspace=pymupdf.csGRAY)
        image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()
    step = image.width / len(chars)
    cells = []
    for i in range(len(chars)):
        cell = image.crop((round(i * step), 0, round((i + 1) * step), image.height))
        cells.append(cell.point(lambda v: 255 if v < 128 else 0))   # ink mask
    return cells


def _ink(image: Image.Image) -> int:
    return image.histogram()[255]


def _shape_distance(ref_cells, cand_cells) -> float:
    """1 - mean intersection-over-union of the glyphs' ink."""
    scores = []
    for a, b in zip(ref_cells, cand_cells):
        union = _ink(ImageChops.lighter(a, b))
        if union == 0:
            continue
        scores.append(_ink(ImageChops.darker(a, b)) / union)
    return 1.0 - (sum(scores) / len(scores)) if scores else 1.0


def _width_distance(ref_widths: Dict[str, float], font: pymupdf.Font) -> float:
    errors = []
    for ch, width in ref_widths.items():
        if width > 0 and font.has_glyph(ord(ch)):
            errors.append(abs(font.glyph_advance(ord(ch)) - width) / width)
    return sum(errors) / len(errors) if errors else 1.0


def _kind_matches(face: Face, ref: Reference) -> bool:
    if face.mono != ref.mono or face.italic != ref.italic:
        return False
    if ref.serif is not None and face.serif is not None and face.serif != ref.serif:
        return False
    name = f"{face.family} {face.style}".lower()
    # Decorative fonts score well on a few letters and look nothing alike.
    return not any(word in name for word in (
        "symbol", "dings", "emoji", "braille", "ornament", "lastresort",
        "hand", "script", "stencil", "shadow", "outline", "inline"))


def closest_faces(ref: Reference, index: FontIndex,
                  limit: int = 1) -> List[Tuple[float, Face]]:
    """Installed faces ranked by how alike they look (best first), as
    (distance, face). Distance 0 is identical; under ~0.15 is close."""
    target = 700 if ref.bold else 400
    faces = [f for f in index.usable_faces()
             if _kind_matches(f, ref) and not f.other_script]
    # Regular text gets a regular-ish face, bold a bold one.
    faces = [f for f in faces if abs(f.weight - target) <= WEIGHT_TOLERANCE] or faces
    if not faces:
        return []
    x_height, cap = _reference_heights(ref)

    widths = dict(ref.widths)
    sample = _sample(ref.chars)
    if ref.font is not None:
        for ch in sample:
            widths.setdefault(ch, ref.font.glyph_advance(ord(ch)))

    def rough(face: Face) -> float:
        score = abs(face.weight - target) / 400
        # A font that doesn't record its heights gets an average penalty
        # rather than a free pass.
        for wanted, have, weight in ((x_height, face.x_height, 3),
                                     (cap, face.cap_height, 2)):
            if wanted:
                score += (abs(have - wanted) / wanted if have else 0.1) * weight
        return score

    # 1. Every candidate: weight, heights and character widths (cheap).
    # Each font is let go once measured, so only one is loaded at a time.
    scored = []
    for face in faces:
        try:
            font = face_font(face)
        except Exception:
            continue
        width_error = _width_distance(widths, font) if widths else 0.0
        del font
        scored.append((rough(face) + width_error * 6, width_error, face))
    scored.sort(key=lambda item: item[0])

    # 2. The best few: compare the glyphs themselves.
    ref_cells = (_render_cells(ref.font, sample)
                 if ref.font is not None and sample else None)
    ranked = []
    for rough_score, width_error, face in scored[:SHORTLIST]:
        weight_gap = abs(face.weight - target) / 1000
        if ref_cells is not None:
            try:
                cells = _render_cells(face_font(face), sample)
            except Exception:
                continue
            shape = _shape_distance(ref_cells, cells)
            distance = 0.7 * shape + 0.3 * min(width_error * 4, 1.0) + weight_gap
        else:
            distance = min(width_error * 4, 1.0) + rough_score * 0.02 + weight_gap
        ranked.append((distance, face))
    ranked.sort(key=lambda item: item[0])
    return ranked[:limit]
