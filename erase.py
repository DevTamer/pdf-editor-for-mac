"""Erase engine for PDF Editor: erase words, or everything in an area, and
fill what's left behind.

No Tk here, so all of this is testable without a display.

What erasing does
-----------------
1. **Text** in the selection is removed character by character with the same
   thin-band, text-only redaction the editor uses for edits (see
   textedit.py): images and vector graphics are untouched, nothing is
   painted, and a character count verifies that exactly the selected
   characters went. Real text sits on top of whatever is behind it, so
   removing it reveals the actual background, which is the best fill there is.
2. **Images** under the erased area are filled *in the image's own pixels*:
   the scan or photo is decoded, the area is filled from its surroundings,
   and the image is rewritten in place, stored the way it was (a JPEG scan
   stays a same-size JPEG), so anything drawn on top of it stays on top.
   This is what erases words in scanned pages. Their visible letters are
   pixels, usually with an invisible OCR text layer on top, and that layer
   is removed too.
3. **Anything else left in an area** (vector logos, stamps, lines) is
   covered with a filled patch, but only if something is actually left to
   hide. Both that check and the fill use a copy of the page with all text
   removed, so nearby text never smears into the fill.

Fills come in two kinds. ``content`` spreads the surrounding pixels inward,
which handles gradients and paper tone, then adds back the grain of the
neighbouring area (paper texture, scanner noise) so the fill doesn't show
as a smooth patch. It ignores ink outliers, so a neighbouring letter isn't
smeared in. ``color`` paints the median surrounding colour.

PDF vector graphics can't be erased selectively with PyMuPDF redactions:
"remove if covered" doesn't remove fully covered shapes, and "remove if
touched" also removes page-sized shapes that merely contain the area. So
graphics are covered, never deleted.
"""

from __future__ import annotations

import io
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pymupdf
from PIL import Image, ImageChops, ImageFilter, ImageStat, JpegImagePlugin

from textedit import (
    BAND_BOTTOM, BAND_INSET, BAND_TOP, EditError, _char_counts, _counted,
)

FILL_CONTENT = "content"
FILL_COLOR = "color"
FILL_MODES = (FILL_CONTENT, FILL_COLOR)

# Rendering resolution for patches and the "anything left?" check.
PATCH_DPI = 144
# A pixel counts as "something left" if it differs this much (0-255, max
# over channels) from the fill that would replace it...
RESIDUAL_DELTA = 48
# ...and an area needs a patch if more than this fraction of its pixels do.
RESIDUAL_FRACTION = 0.002
# Extra room (as a fraction of the font size) around an invisible OCR
# word's box when filling the scan behind it: scanned ink rarely lines up
# exactly with the OCR layer.
OCR_PAD = 0.18


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Char:
    c: str
    bbox: Tuple[float, float, float, float]
    origin: Tuple[float, float]
    size: float


@dataclass(frozen=True)
class Word:
    """A whitespace-delimited word on a page, in reading order."""

    index: int                              # position in reading order
    chars: Tuple[Char, ...]
    invisible: bool                         # OCR layer text (not drawn)

    @property
    def text(self) -> str:
        return "".join(c.c for c in self.chars)

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (min(c.bbox[0] for c in self.chars),
                min(c.bbox[1] for c in self.chars),
                max(c.bbox[2] for c in self.chars),
                max(c.bbox[3] for c in self.chars))

    @property
    def rect(self) -> pymupdf.Rect:
        return pymupdf.Rect(self.bbox)


def _invisible_origins(page: pymupdf.Page) -> set:
    """Origins of characters drawn invisibly (render mode 3), as OCR text
    layers are."""
    found = set()
    for trace in page.get_texttrace():
        if trace["type"] == 3:
            for ch in trace["chars"]:
                found.add((round(ch[2][0], 1), round(ch[2][1], 1)))
    return found


def page_words(page: pymupdf.Page) -> List[Word]:
    """Every word on the page, in the order text extraction reads them.
    Horizontal text only (other directions can't be erased precisely)."""
    invisible = _invisible_origins(page)
    words: List[Word] = []
    current: List[Char] = []

    def flush():
        if current:
            inv = all((round(c.origin[0], 1), round(c.origin[1], 1)) in invisible
                      for c in current)
            words.append(Word(len(words), tuple(current), inv))
            current.clear()

    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line["dir"]
            if abs(dx - 1) > 1e-3 or abs(dy) > 1e-3:
                flush()
                continue
            for span in line["spans"]:
                for ch in span["chars"]:
                    if ch["c"].isspace():
                        flush()
                        continue
                    char = Char(ch["c"], tuple(ch["bbox"]), tuple(ch["origin"]),
                                span["size"])
                    # A word continues across spans only on the same
                    # baseline with no gap (e.g. a mid-word style change).
                    if current and (abs(current[-1].origin[1] - char.origin[1]) > 0.5
                                    or char.bbox[0] - current[-1].bbox[2] > 0.5):
                        flush()
                    current.append(char)
            flush()
    flush()
    return words


def word_at(words: Sequence[Word], x: float, y: float,
            tolerance: float = 1.0) -> Optional[Word]:
    for w in words:
        x0, y0, x1, y1 = w.bbox
        if x0 - tolerance <= x <= x1 + tolerance and y0 - tolerance <= y <= y1 + tolerance:
            return w
    return None


def words_between(words: Sequence[Word], a: Word, b: Word) -> List[Word]:
    """Words from ``a`` to ``b`` inclusive, in reading order, like a text
    selection dragged from one to the other."""
    lo, hi = sorted((a.index, b.index))
    return [w for w in words if lo <= w.index <= hi]


def chars_in_area(words: Sequence[Word], area: pymupdf.Rect) -> List[Char]:
    """Characters whose centre lies inside ``area``."""
    out = []
    for w in words:
        for c in w.chars:
            cx, cy = (c.bbox[0] + c.bbox[2]) / 2, (c.bbox[1] + c.bbox[3]) / 2
            if area.x0 <= cx <= area.x1 and area.y0 <= cy <= area.y1:
                out.append(c)
    return out


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

@dataclass
class ErasePlan:
    """What to erase on one page.

    Either ``words`` (click / text-selection erase) or ``area`` (rectangle
    erase, which also takes every character centred inside it).
    """

    page_num: int
    words: List[Word] = field(default_factory=list)
    area: Optional[pymupdf.Rect] = None
    fill: str = FILL_CONTENT

    def chars(self, page_words_now: Sequence[Word]) -> List[Char]:
        chars = [c for w in self.words for c in w.chars]
        if self.area is not None:
            seen = {id(c) for c in chars}
            chars += [c for c in chars_in_area(page_words_now, self.area)
                      if id(c) not in seen]
        return chars

    @property
    def label(self) -> str:
        if self.area is not None:
            return "Erase area"
        text = " ".join(w.text for w in self.words)
        if len(text) > 28:
            text = text[:27] + "…"
        return f"Erase “{text}”"


# ---------------------------------------------------------------------------
# Fills (Pillow)
# ---------------------------------------------------------------------------

def _hist_median(hist: Sequence[int]) -> int:
    total = sum(hist)
    if not total:
        return 255
    running = 0
    for value, count in enumerate(hist):
        running += count
        if running * 2 >= total:
            return value
    return 255


def _known_mask(img: Image.Image, hole: Tuple[int, int, int, int]) -> Image.Image:
    """Mask ("L", 255 = use as a source for filling) of the pixels around
    the hole that look like background: ink and other outliers far from the
    surroundings' median brightness are left out, so they don't smear in."""
    gray = img.convert("L")
    ring = [a - b for a, b in zip(gray.histogram(), gray.crop(hole).histogram())]
    med = _hist_median(ring)
    spread_hist = [0] * 256
    for value, count in enumerate(ring):
        spread_hist[abs(value - med)] += count
    threshold = max(48, 3 * _hist_median(spread_hist))
    mask = gray.point([255 if abs(v - med) <= threshold else 0 for v in range(256)])
    mask.paste(0, hole)
    return mask


def fill_hole(img: Image.Image, hole: Tuple[int, int, int, int],
              mode: str = FILL_CONTENT) -> Image.Image:
    """Return ``img`` with the ``hole`` box (pixel coordinates, exclusive
    end) filled from the surrounding pixels. ``img`` should include a
    margin of surroundings around the hole."""
    if mode not in FILL_MODES:
        raise ValueError(f"unknown fill mode {mode!r}")
    rgb = img.convert("RGB")
    known = _known_mask(rgb, hole)
    if known.getbbox() is None:  # nothing usable around: use everything
        known = Image.new("L", rgb.size, 255)
        known.paste(0, hole)

    base = tuple(int(v) for v in ImageStat.Stat(rgb, mask=known).median)
    if mode == FILL_COLOR:
        out = rgb.copy()
        out.paste(base, hole)
        return out.convert(img.mode)

    # Diffusion fill: repeatedly blur, keep the known pixels as they are,
    # and let the unknown ones (the hole, plus ignored outliers around it)
    # take on the blurred values. Large radii first carry colour across the
    # hole; small ones settle the detail. Only the hole is changed in the
    # result; outliers outside it keep their original pixels.
    unknown = ImageChops.invert(known)
    current = Image.composite(Image.new("RGB", rgb.size, base), rgb, unknown)
    source = current
    radius = max(1, max(hole[2] - hole[0], hole[3] - hole[1]) // 2)
    while True:
        for _ in range(4):
            blurred = current.filter(ImageFilter.BoxBlur(radius))
            current = Image.composite(blurred, source, unknown)
        if radius == 1:
            break
        radius = max(1, radius // 2)
    # Measure grain on `source` (hole and outliers already replaced by the
    # base colour), so dark ink doesn't masquerade as texture.
    current = _add_texture(current, source, known, hole)
    hole_mask = Image.new("L", rgb.size, 0)
    hole_mask.paste(255, hole)
    return Image.composite(current, rgb, hole_mask).convert(img.mode)


# Below this much grain (standard deviation of fine detail, 0-255), the
# surroundings count as clean and the fill stays smooth.
MIN_GRAIN = 1.5


def _add_texture(smooth: Image.Image, rgb: Image.Image, known: Image.Image,
                 hole: Tuple[int, int, int, int]) -> Image.Image:
    # `rgb` must already have the hole and outliers neutralised.
    """Give a smooth fill the grain of its surroundings (paper texture,
    scanner noise) by mirroring the fine detail of the strip next to the
    hole into it. Ink and other outliers in that strip contribute nothing."""
    x0, y0, x1, y1 = hole
    w, h = x1 - x0, y1 - y0
    # Fine detail = image minus a blurred copy, centred on 128.
    detail = ImageChops.subtract(rgb, rgb.filter(ImageFilter.GaussianBlur(2)),
                                 scale=1, offset=128)
    neutral = Image.new("RGB", rgb.size, (128, 128, 128))
    detail = Image.composite(detail, neutral, known)
    stats = ImageStat.Stat(detail, mask=known)
    if not stats.count[0] or max(stats.stddev) < MIN_GRAIN:
        return smooth

    # The strip above the hole, else below; tiled if shorter than the hole.
    above = min(h, y0)
    below = min(h, rgb.height - y1)
    if above >= below and above > 0:
        strip = detail.crop((x0, y0 - above, x1, y0)).transpose(
            Image.Transpose.FLIP_TOP_BOTTOM)
    elif below > 0:
        strip = detail.crop((x0, y1, x1, y1 + below)).transpose(
            Image.Transpose.FLIP_TOP_BOTTOM)
    else:
        return smooth
    texture = Image.new("RGB", (w, h), (128, 128, 128))
    for top in range(0, h, strip.height):
        texture.paste(strip, (0, top))
    region = smooth.crop(hole)
    textured = ImageChops.add(region, texture, scale=1, offset=-128)
    out = smooth.copy()
    out.paste(textured, hole[:2])
    return out


def _residual(before: Image.Image, after: Image.Image,
              hole: Tuple[int, int, int, int]) -> float:
    """Fraction of hole pixels that differ noticeably between the two."""
    a = before.convert("RGB").crop(hole)
    b = after.convert("RGB").crop(hole)
    # Max over channels, per pixel.
    diff = ImageChops.difference(a, b)
    channels = diff.split()
    worst = channels[0]
    for ch in channels[1:]:
        worst = ImageChops.lighter(worst, ch)
    over = sum(worst.histogram()[RESIDUAL_DELTA + 1:])
    return over / max(1, a.width * a.height)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def _remove_chars(page: pymupdf.Page, chars: Sequence[Char]) -> None:
    """Text-only redaction of exactly these characters (verified)."""
    if not chars:
        return
    expected = _char_counts(page) - _counted("".join(c.c for c in chars))
    # One band per stretch of adjacent characters on the same baseline.
    stretches: List[List[Char]] = []
    for c in chars:
        prev = stretches[-1][-1] if stretches else None
        if (prev is not None and abs(prev.origin[1] - c.origin[1]) <= 0.5
                and abs(prev.size - c.size) < 0.05
                and -0.5 <= c.bbox[0] - prev.bbox[2] <= 0.5):
            stretches[-1].append(c)
        else:
            stretches.append([c])
    for run in stretches:
        oy, size = run[0].origin[1], run[0].size
        page.add_redact_annot(
            pymupdf.Rect(run[0].bbox[0] + BAND_INSET, oy - BAND_TOP * size,
                         run[-1].bbox[2] - BAND_INSET, oy - BAND_BOTTOM * size),
            fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    if _char_counts(page) != expected:
        raise EditError("Erasing would also have removed text outside the "
                        "selection, so nothing was erased.")


def _image_use_counts(doc: pymupdf.Document) -> Counter:
    counts: Counter = Counter()
    for page in doc:
        for info in page.get_image_info(xrefs=True):
            if info["xref"]:
                counts[info["xref"]] += 1
    return counts


def _editable_image(doc, info, use_counts) -> bool:
    xref = info["xref"]
    if not xref or use_counts[xref] != 1:
        return False                      # inline, or shown more than once
    if (doc.xref_get_key(xref, "SMask")[0] != "null"
            or doc.xref_get_key(xref, "Mask")[0] != "null"):
        return False                      # has transparency / colour-key mask
    if doc.xref_get_key(xref, "ImageMask")[1] == "true":
        return False                      # stencil mask
    return True


def _fill_in_image(doc: pymupdf.Document, page: pymupdf.Page, info,
                   targets: Sequence[pymupdf.Rect], mode: str) -> bool:
    """Fill ``targets`` (page coordinates) inside one placed image's own
    pixels. Returns False if the image couldn't be handled."""
    try:
        pix = pymupdf.Pixmap(doc, info["xref"])
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    except Exception:
        return False
    mode_name = "L" if pix.n == 1 else "RGB"
    img = Image.frombytes(mode_name, (pix.width, pix.height), pix.samples)
    to_pixels = ~pymupdf.Matrix(info["transform"])
    changed = False
    for target in targets:
        corners = [p * to_pixels for p in (target.tl, target.tr, target.bl, target.br)]
        xs = [p.x * pix.width for p in corners]
        ys = [p.y * pix.height for p in corners]
        hole = (max(0, int(min(xs))), max(0, int(min(ys))),
                min(pix.width, int(max(xs)) + 1), min(pix.height, int(max(ys)) + 1))
        if hole[2] <= hole[0] or hole[3] <= hole[1]:
            continue
        margin = max(4, (hole[2] - hole[0] + hole[3] - hole[1]) // 4)
        box = (max(0, hole[0] - margin), max(0, hole[1] - margin),
               min(pix.width, hole[2] + margin), min(pix.height, hole[3] + margin))
        local = (hole[0] - box[0], hole[1] - box[1], hole[2] - box[0], hole[3] - box[1])
        img.paste(fill_hole(img.crop(box), local, mode), box[:2])
        changed = True
    if not changed:
        return True
    _write_image(doc, info["xref"], img)
    return True


JPEG_QUALITY = 90  # only used if the original JPEG's tables can't be read


def _write_image(doc: pymupdf.Document, xref: int, img: Image.Image) -> None:
    """Replace an image's pixels in place, stored the way the original was
    so the file doesn't balloon.

    - JPEG stays JPEG, re-encoded with the original's own quantisation
      tables and chroma subsampling (same quality, same size).
    - 1-bit black-and-white stays 1-bit.
    - Anything else becomes deflated 8-bit samples.

    The image object keeps its number. (PyMuPDF's Page.replace_image adds
    a second copy of the image that survives garbage collection, which
    doubles the size of a scanned page.) Callers must flush MuPDF's image
    cache afterwards (pymupdf.TOOLS.store_shrink).
    """
    filters = doc.xref_get_key(xref, "Filter")[1] or ""
    bits = doc.xref_get_key(xref, "BitsPerComponent")[1]
    gray = img.mode == "L"
    if "DCTDecode" in filters:
        buf = io.BytesIO()
        try:
            original = Image.open(io.BytesIO(doc.xref_stream_raw(xref)))
            img.save(buf, "JPEG", qtables=original.quantization,
                     subsampling=JpegImagePlugin.get_sampling(original))
        except Exception:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=JPEG_QUALITY)
        data, filter_name, bpc = buf.getvalue(), "/DCTDecode", "8"
    elif bits == "1" and gray:
        one_bit = img.point(lambda v: 255 if v >= 128 else 0).convert("1")
        data, filter_name, bpc = zlib.compress(one_bit.tobytes()), "/FlateDecode", "1"
    else:
        data, filter_name, bpc = zlib.compress(img.tobytes()), "/FlateDecode", "8"

    doc.update_stream(xref, data, compress=False)
    for key, value in (("Filter", filter_name), ("DecodeParms", "null"),
                       ("Decode", "null"), ("Width", str(img.width)),
                       ("Height", str(img.height)), ("BitsPerComponent", bpc),
                       ("ColorSpace", "/DeviceGray" if gray else "/DeviceRGB")):
        doc.xref_set_key(xref, key, value)


def _text_free_render(doc: pymupdf.Document, page_num: int,
                      clip: pymupdf.Rect) -> Image.Image:
    """Render ``clip`` of the page with all text removed (on a copy)."""
    scratch = pymupdf.open()
    try:
        scratch.insert_pdf(doc, from_page=page_num, to_page=page_num)
        copy = scratch[0]
        copy.add_redact_annot(copy.rect, fill=False)
        copy.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        pix = copy.get_pixmap(dpi=PATCH_DPI, clip=clip, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        scratch.close()


def _patch_area(doc: pymupdf.Document, page: pymupdf.Page, page_num: int,
                area: pymupdf.Rect, mode: str) -> bool:
    """Cover whatever non-text content is left in ``area`` with a filled
    patch. Returns whether a patch was needed."""
    scale = PATCH_DPI / 72
    margin = max(6.0, min(area.width, area.height) / 3)
    clip = (area + (-margin, -margin, margin, margin)) & page.rect
    img = _text_free_render(doc, page_num, clip)
    hole = (round((area.x0 - clip.x0) * scale), round((area.y0 - clip.y0) * scale),
            round((area.x1 - clip.x0) * scale), round((area.y1 - clip.y0) * scale))
    hole = (max(0, hole[0]), max(0, hole[1]),
            min(img.width, hole[2]), min(img.height, hole[3]))
    if hole[2] <= hole[0] or hole[3] <= hole[1]:
        return False
    filled = fill_hole(img, hole, mode)
    if _residual(img, filled, hole) <= RESIDUAL_FRACTION:
        return False
    buf = io.BytesIO()
    filled.crop(hole).save(buf, "PNG")
    target = pymupdf.Rect(clip.x0 + hole[0] / scale, clip.y0 + hole[1] / scale,
                          clip.x0 + hole[2] / scale, clip.y0 + hole[3] / scale)
    page.insert_image(target, stream=buf.getvalue(), overlay=True)
    return True


def _padded(rect: pymupdf.Rect, pad: float) -> pymupdf.Rect:
    return rect + (-pad, -pad, pad, pad)


def apply_erase(doc: pymupdf.Document, page_num: int,
                plan: ErasePlan) -> Dict[str, int]:
    """Erase as planned. Returns counts of what was done (for messages and
    tests). Raises EditError if the selection is stale or the text removal
    can't be verified; the caller must then restore the document."""
    page = doc[page_num]
    if page.rotation:
        raise EditError("Erasing on rotated pages isn't supported yet.")
    words_now = page_words(page)
    present = {(w.text, tuple(round(v, 1) for v in w.bbox)) for w in words_now}
    for w in plan.words:
        if (w.text, tuple(round(v, 1) for v in w.bbox)) not in present:
            raise EditError("The page changed since that text was selected. "
                            "Select it again.")
    if plan.area is not None and (plan.area.is_empty or plan.area.is_infinite):
        raise EditError("Nothing to erase in that area.")

    chars = plan.chars(words_now)
    # Where scanned pixels need filling: under invisible OCR words, and the
    # whole area for a rectangle erase.
    image_targets: List[pymupdf.Rect] = [
        _padded(w.rect, OCR_PAD * w.chars[0].size) for w in plan.words if w.invisible]
    if plan.area is not None:
        image_targets.append(pymupdf.Rect(plan.area))
        ocr_in_area = [c for c in chars_in_area(
            [w for w in words_now if w.invisible], plan.area)]
        image_targets += [_padded(pymupdf.Rect(c.bbox), OCR_PAD * c.size)
                          for c in ocr_in_area]

    _remove_chars(page, chars)
    done = {"chars": len(chars), "images": 0, "patches": 0}

    patch_targets: List[pymupdf.Rect] = []
    if image_targets:
        use_counts = _image_use_counts(doc)
        for info in page.get_image_info(xrefs=True):
            placed = pymupdf.Rect(info["bbox"])
            hits = [t & placed for t in image_targets if t.intersects(placed)]
            if not hits:
                continue
            if _editable_image(doc, info, use_counts) and _fill_in_image(
                    doc, page, info, hits, plan.fill):
                done["images"] += 1
            else:
                patch_targets += hits
        if done["images"]:
            # MuPDF caches decoded images: without emptying that cache the
            # page (and any freshly fetched copy of it) still shows the old
            # pixels. (page.refresh() / doc.reload_page() would also do it,
            # but they fail if anything else holds a reference to the page.)
            pymupdf.TOOLS.store_shrink(100)

    if plan.area is not None:
        patch_targets.append(pymupdf.Rect(plan.area))
    for target in patch_targets:
        if _patch_area(doc, page, page_num, target, plan.fill):
            done["patches"] += 1

    if not any(done.values()):
        raise EditError("There was nothing to erase there.")
    return done
