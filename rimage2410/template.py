"""Build label templates: Photoshop (PSD) and PNG.

The PSD template is 1889 x 1889 pixels at 400 ppi (exactly 120 mm), 8-bit RGB with an
embedded sRGB profile. The Everest 400 works in RGB and converts to the ribbon colour
panels itself, so design in RGB, not CMYK.

The guide layers are drawn outside the printable area (the 118 mm line just outside it,
the 24 mm line just inside it, the grey overlay only where nothing is printed). If you
forget to hide them, they still do not get printed. Only the "GUIDE: safe zone" layer lies in the
printable area, and it is hidden by default.

Usage: python -m rimage2410.template out.psd
"""

from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageFont

from .geometry import CANVAS_PX, DISC_MM, DPI, HOLE_MM, HUB_INNER_MM, PRINT_INNER_MM, PRINT_OUTER_MM
from .psd import Layer, write_psd

SS = 3   # supersampling for soft edges
GAP_MM = 0.4   # gap (radius) between the guide layers and the edge of the printable area


def _font(size: int):
    for p in ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _rings(rings: list[tuple[float, float, tuple[int, int, int, int]]], dashed: bool = False) -> Image.Image:
    """Rings (inner mm, outer mm, colour) as an RGBA layer."""
    n = CANVAS_PX * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = (n - 1) / 2
    px = n / DISC_MM
    for inner, outer, color in rings:
        ro, ri = outer * px / 2, inner * px / 2
        if dashed:
            mask = Image.new("L", (n, n), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse([c - ro, c - ro, c + ro, c + ro], fill=255)
            md.ellipse([c - ri, c - ri, c + ri, c + ri], fill=0)
            # dashes: leave out a sector every 6 degrees
            for a in range(0, 360, 12):
                md.pieslice([c - ro - 2, c - ro - 2, c + ro + 2, c + ro + 2], a + 6, a + 12, fill=0)
            img.paste(Image.new("RGBA", (n, n), color), (0, 0), mask)
        else:
            d.ellipse([c - ro, c - ro, c + ro, c + ro], fill=color)
            d.ellipse([c - ri, c - ri, c + ri, c + ri], fill=(0, 0, 0, 0))
    return img.resize((CANVAS_PX, CANVAS_PX), Image.LANCZOS)


def _overlay(inner_mm: float = PRINT_INNER_MM, outer_mm: float = PRINT_OUTER_MM) -> Image.Image:
    """Grey where nothing is printed; lighter in the hub (printed only with 'print hub')."""
    n = CANVAS_PX * SS
    img = Image.new("RGBA", (n, n), (120, 120, 120, 150))
    d = ImageDraw.Draw(img)
    c = (n - 1) / 2
    px = n / DISC_MM

    def disc(mm, fill):
        r = mm * px / 2
        d.ellipse([c - r, c - r, c + r, c + r], fill=fill)

    disc(outer_mm + 2 * GAP_MM, (0, 0, 0, 0))
    disc(inner_mm, (0, 0, 0, 0))                  # hub zone: separate layer
    disc(HUB_INNER_MM - 2 * GAP_MM, (120, 120, 120, 150))
    disc(HOLE_MM, (40, 40, 40, 200))
    return img.resize((CANVAS_PX, CANVAS_PX), Image.LANCZOS)


def _mm(v: float) -> str:
    return f"{v:g}"


def _texts(inner_mm: float = PRINT_INNER_MM, outer_mm: float = PRINT_OUTER_MM) -> Image.Image:
    """Notes in the four corners, outside the disc. A mask clears everything within 120 mm,
    so this layer never reaches the printable area."""
    n = CANVAS_PX
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    big, small = _font(28), _font(19)
    ink = (30, 30, 30, 255)
    corners = {
        "tl": [("Rimage 2410 label template", big), ("1889 x 1889 px, 400 ppi", small), ("= 120 mm, RGB 8 bit, sRGB", small)],
        "tr": [("Red: printable", small), (f"{_mm(outer_mm)} to {_mm(inner_mm)} mm", small), ("Blue: hub 17 mm", small), ("(only with 'print hub')", small)],
        "bl": [("Grey: not printed", small), ("Black: hole 15 mm", small), ("Design in 'Your design'", small)],
        "br": [("Export as PNG or JPEG", small), ("in sRGB. Guide layers are", small), ("not printed, except", small), ("'Safe zone' (hidden)", small)],
    }
    m = 18
    for corner, lines in corners.items():
        y = m if corner[0] == "t" else n - m - sum(f.size + 8 for _, f in lines)
        for text, f in lines:
            w = d.textlength(text, font=f)
            x = m if corner[1] == "l" else n - m - w
            d.text((x, y), text, font=f, fill=ink)
            y += f.size + 8
    mask = Image.new("L", (n, n), 255)
    md = ImageDraw.Draw(mask)
    c, r = (n - 1) / 2, n / 2 + 4
    md.ellipse([c - r, c - r, c + r, c + r], fill=0)
    img.putalpha(Image.fromarray(np.minimum(np.asarray(img.getchannel("A")), np.asarray(mask))))
    return img


def build_layers(inner_mm: float = PRINT_INNER_MM, outer_mm: float = PRINT_OUTER_MM) -> list[Layer]:
    """Template layers for a print area from inner_mm to outer_mm (depends on the disc type)."""
    n = CANVAS_PX
    white = Image.new("RGBA", (n, n), (255, 255, 255, 255))
    design = Image.new("RGBA", (n, n), (255, 255, 255, 0))
    lines = _rings([
        (DISC_MM - 0.5, DISC_MM, (90, 90, 90, 255)),                                  # edge of the disc
        (outer_mm + 2 * GAP_MM, outer_mm + 2 * GAP_MM + 0.5, (220, 0, 0, 255)),   # just outside the print edge
        (HUB_INNER_MM - 2 * GAP_MM - 0.5, HUB_INNER_MM - 2 * GAP_MM, (0, 110, 220, 255)),     # just inside 17 mm
        (HOLE_MM, HOLE_MM + 0.4, (0, 0, 0, 255)),                                     # centre hole
    ])
    hubzone = _rings([(HUB_INNER_MM, inner_mm - 2 * GAP_MM, (120, 150, 200, 90)),
                      (inner_mm - 2 * GAP_MM - 0.5, inner_mm - 2 * GAP_MM, (220, 0, 0, 255))])
    safe = _rings([(114.0 - 0.4, 114.0, (255, 140, 0, 255)), (28.0, 28.4, (255, 140, 0, 255))], dashed=True)
    return [
        Layer("Background: white = no ink", white),
        Layer("Your design", design),
        Layer("GUIDE: area not printed", _overlay(inner_mm, outer_mm)),
        Layer(f"GUIDE: lines 120, {_mm(outer_mm)}, 17 and 15 mm", lines),
        Layer(f"GUIDE: hub zone 17 to {_mm(inner_mm)} mm (hide if you print the hub)", hubzone),
        Layer("GUIDE: notes", _texts(inner_mm, outer_mm)),
        Layer("GUIDE: safe zone for text, 114 and 28 mm (printable, hidden by default)", safe, visible=False),
    ]


def srgb_icc() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def make_psd_template(path: str, inner_mm: float = PRINT_INNER_MM, outer_mm: float = PRINT_OUTER_MM) -> None:
    c = CANVAS_PX / 2
    write_psd(path, build_layers(inner_mm, outer_mm), dpi=DPI, icc=srgb_icc(), guides=[(c, "v"), (c, "h")])


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "rimage2410-label-template.psd"
    make_psd_template(out)
    print(f"written: {out} ({os.path.getsize(out)} bytes)")
