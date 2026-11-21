"""Generates packaging/icon.icns: a document page with lines of text, one
being edited (highlighted, with a text cursor).

    python packaging/make_icon.py

Needs Pillow (already a dependency) and macOS's `iconutil`.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
SIZE = 1024


def draw_icon(size: int = SIZE) -> Image.Image:
    s = size / 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Soft shadow under the page.
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (200 * s, 120 * s, 840 * s, 940 * s), radius=48 * s, fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28 * s)))

    d = ImageDraw.Draw(img)
    # Page with a folded corner.
    page = [(184 * s, 84 * s), (680 * s, 84 * s), (824 * s, 228 * s),
            (824 * s, 904 * s), (184 * s, 904 * s)]
    d.polygon(page, fill=(255, 255, 255, 255))
    d.polygon([(680 * s, 84 * s), (680 * s, 228 * s), (824 * s, 228 * s)],
              fill=(214, 222, 235, 255))

    # Lines of "text".
    grey = (176, 186, 201, 255)
    for i, (x1, y) in enumerate([(700, 300), (760, 380), (620, 460),
                                 (740, 700), (560, 780)]):
        d.rounded_rectangle((260 * s, y * s, x1 * s, (y + 36) * s),
                            radius=18 * s, fill=grey)

    # The line being edited: highlighted, darker, with a cursor.
    d.rounded_rectangle((236 * s, 548 * s, 788 * s, 652 * s), radius=22 * s,
                        fill=(47, 122, 229, 40), outline=(47, 122, 229, 255),
                        width=round(8 * s))
    d.rounded_rectangle((268 * s, 582 * s, 612 * s, 618 * s), radius=18 * s,
                        fill=(30, 41, 59, 255))
    d.rounded_rectangle((636 * s, 520 * s, 656 * s, 680 * s), radius=8 * s,
                        fill=(47, 122, 229, 255))
    return img


def main() -> None:
    if shutil.which("iconutil") is None:
        raise SystemExit("iconutil not found (it ships with macOS).")
    base = draw_icon()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for pt in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = pt * scale
                name = f"icon_{pt}x{pt}{'@2x' if scale == 2 else ''}.png"
                base.resize((px, px), Image.LANCZOS).save(iconset / name)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o",
                        str(HERE / "icon.icns")], check=True)
    print(f"Wrote {HERE / 'icon.icns'}")


if __name__ == "__main__":
    main()
