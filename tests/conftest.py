"""Shared fixtures for the PDF Editor tests.

Most test PDFs are generated on the fly with PyMuPDF so their exact
content and positions are known. One real-world fixture is committed:
tests/fixtures/coretext_sample.pdf, produced by macOS's own text system
(CoreText/Quartz) with TrueType subset fonts, the way documents from
Pages, TextEdit or "Print to PDF" embed fonts. It's regenerated with:

    swift tests/fixtures/make_coretext_sample.swift tests/fixtures/coretext_sample.pdf
"""

import sys
from pathlib import Path

import pymupdf
import pytest

# Make editor.py / textedit.py importable without installing anything.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORETEXT_SAMPLE = FIXTURES / "coretext_sample.pdf"

PAGE_WIDTH = 500
PAGE_HEIGHT = 600

# Page 1 layout (baselines in PDF points). Lines 2-4 are set solid
# (line spacing == font size), the tightest real-world spacing, to prove
# edits don't touch neighbouring lines.
TITLE = "Quarterly Report"                   # embedded subset font, blue
LINE_ABOVE = "The line above the target"     # Helvetica 12, y=150
TARGET = "Target line to edit"               # Helvetica 12, y=162
LINE_BELOW = "The line below the target"     # Helvetica 12, y=174
REGULAR_PART = "Status: "                    # Helvetica 12, y=220 ...
BOLD_PART = "APPROVED"                       # ... followed by Helvetica-Bold
SECOND_PAGE_TEXT = "Second page stays exactly the same"

BLUE = (0.0, 0.2, 0.8)


def build_sample_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_font(fontname="F1", fontbuffer=pymupdf.Font("tiro").buffer)
    page.insert_text((50, 80), TITLE, fontname="F1", fontsize=20, color=BLUE)
    page.insert_text((50, 150), LINE_ABOVE, fontname="helv", fontsize=12)
    page.insert_text((50, 162), TARGET, fontname="helv", fontsize=12)
    page.insert_text((50, 174), LINE_BELOW, fontname="helv", fontsize=12)
    # A vector line straight through the target line.
    page.draw_line((40, 158), (400, 158), color=(0, 0.6, 0), width=0.5)
    # A small image to the right of the target line.
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.set_rect(pix.irect, (200, 30, 30))
    page.insert_image(pymupdf.Rect(420, 150, 440, 170), pixmap=pix)
    # A regular run immediately followed by a bold run on one line.
    page.insert_text((50, 220), REGULAR_PART, fontname="helv", fontsize=12)
    regular_width = pymupdf.get_text_length(REGULAR_PART, "helv", 12)
    page.insert_text((50 + regular_width, 220), BOLD_PART, fontname="hebo",
                     fontsize=12)

    page2 = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page2.insert_text((50, 100), SECOND_PAGE_TEXT, fontname="helv", fontsize=12)

    # Embed only the glyphs actually used, as real-world PDFs do.
    doc.subset_fonts()
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()


@pytest.fixture(autouse=True)
def small_glyph_heights():
    """Match the app, which turns tight text boxes on at startup."""
    pymupdf.TOOLS.set_small_glyph_heights(True)
    yield
    pymupdf.TOOLS.set_small_glyph_heights(True)


@pytest.fixture
def sample_pdf_path(tmp_path) -> Path:
    path = tmp_path / "sample.pdf"
    build_sample_pdf(path)
    return path


@pytest.fixture
def sample_doc(sample_pdf_path):
    doc = pymupdf.open(str(sample_pdf_path))
    yield doc
    if not doc.is_closed:
        doc.close()


@pytest.fixture
def coretext_doc():
    doc = pymupdf.open(str(CORETEXT_SAMPLE))
    yield doc
    doc.close()


@pytest.fixture
def coretext_path(tmp_path) -> Path:
    """A writable copy of the CoreText sample."""
    path = tmp_path / "coretext.pdf"
    path.write_bytes(CORETEXT_SAMPLE.read_bytes())
    return path
