"""Turn an image into the print image for the Everest 400, plus a preview of the disc.

Print image: RGB, 1889 x 1889 pixels = 120 x 120 mm at 400 dpi (see geometry.py). Only the
ring between the inner and outer diameter is printed. Outside it the image is white, which
means no ink on the Everest. The default is 118 mm outer and 24 mm inner. With print_hub the
hub is printed too, down to 17 mm.
"""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw

from .geometry import (CANVAS_PX, DISC_MM, HOLE_MM, HUB_INNER_MM, PRINT_INNER_MM,
                       PRINT_OUTER_MM)

MIN_GOOD_PX = 1400   # below this is under about 300 dpi at 118 mm


def ring_mask(size: int, outer_mm: float, inner_mm: float, supersample: int = 4) -> Image.Image:
    """Mask (L) of the printable ring on a canvas of `size` pixels for 120 mm, with soft edges."""
    big = size * supersample
    scale = big / DISC_MM
    m = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(m)
    c = (big - 1) / 2
    ro = outer_mm * scale / 2
    ri = inner_mm * scale / 2
    d.ellipse([c - ro, c - ro, c + ro, c + ro], fill=255)
    if ri > 0:
        d.ellipse([c - ri, c - ri, c + ri, c + ri], fill=0)
    return m.resize((size, size), Image.LANCZOS)


def analyze_image(img: Image.Image) -> list[str]:
    """Warnings about the source image."""
    w, h = img.size
    warnings = []
    if min(w, h) < MIN_GOOD_PX:
        dpi = min(w, h) / (PRINT_OUTER_MM / 25.4)
        warnings.append(f"image is small ({w} x {h} px, about {dpi:.0f} dpi at 118 mm); "
                        f"at least {MIN_GOOD_PX} px recommended, 1889 px is ideal")
    if abs(w - h) > 0.02 * max(w, h):
        warnings.append("image is not square; part of it is cropped or borders are added")
    return warnings


def place_image(img: Image.Image, *, fit: str = "cover", scale: float = 1.0,
                offset_x: float = 0.0, offset_y: float = 0.0, rotate: float = 0.0,
                background: tuple[int, int, int] = (255, 255, 255), canvas: int = CANVAS_PX,
                srgb: bool = False) -> Image.Image:
    """Place the image on a square canvas of `canvas` pixels (= 120 mm), without the ring mask.

    fit: 'cover' fills the whole disc, 'contain' shows everything. scale: extra zoom (1.0 = fit).
    offset_x/offset_y in millimetres, positive = right/down. rotate in degrees, counterclockwise.
    srgb=True: img has already been through artwork.to_srgb.
    """
    from .artwork import to_srgb
    if fit not in ("cover", "contain"):
        raise ValueError("fit must be 'cover' or 'contain'")
    if not srgb:
        img, _notes = to_srgb(img)
    if img.mode == "RGBA":
        base = Image.new("RGBA", img.size, background + (255,))
        img = Image.alpha_composite(base, img)
    img = img.convert("RGB")

    n = canvas
    w, h = img.size
    f = (max if fit == "cover" else min)(n / w, n / h) * max(scale, 0.01)
    nw, nh = max(1, round(w * f)), max(1, round(h * f))
    resized = img.resize((nw, nh), Image.LANCZOS)
    if rotate:
        resized = resized.rotate(rotate, resample=Image.BICUBIC, expand=True, fillcolor=background)
    out = Image.new("RGB", (n, n), background)
    px_per_mm = n / DISC_MM
    px = round((n - resized.width) / 2 + offset_x * px_per_mm)
    py = round((n - resized.height) / 2 + offset_y * px_per_mm)
    out.paste(resized, (px, py))
    return out


def prepare_label(img: Image.Image, *, fit: str = "cover", scale: float = 1.0,
                  offset_x: float = 0.0, offset_y: float = 0.0, rotate: float = 0.0,
                  background: tuple[int, int, int] = (255, 255, 255),
                  outer_mm: float = PRINT_OUTER_MM, inner_mm: float = PRINT_INNER_MM,
                  print_hub: bool = False, canvas: int = CANVAS_PX, srgb: bool = False) -> Image.Image:
    """Turn an image into the print image (RGB, 1889 x 1889; or smaller via canvas, for the preview).

    Colour profiles are converted to sRGB (artwork.to_srgb). Outside the printable ring the image
    is white, which means no ink on the Everest.
    """
    placed = place_image(img, fit=fit, scale=scale, offset_x=offset_x, offset_y=offset_y, rotate=rotate,
                         background=background, canvas=canvas, srgb=srgb)
    n = placed.width
    inner = HUB_INNER_MM if print_hub else inner_mm
    mask = ring_mask(n, outer_mm, inner)
    white = Image.new("RGB", (n, n), (255, 255, 255))
    return Image.composite(placed, white, mask)


def _silver(size: int) -> Image.Image:
    """Silver disc surface with a soft radial and conical sheen."""
    import numpy as np
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2
    dx, dy = xx - c, yy - c
    r = np.sqrt(dx * dx + dy * dy) / (size / 2)
    ang = np.arctan2(dy, dx)
    shine = 0.5 + 0.5 * np.cos(2 * ang + 0.6)            # two bright sectors
    base = 188 + 30 * shine - 12 * r
    rgb = np.stack([base, base + 3, base + 8], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype("uint8"), "RGB")


def render_preview(label: Image.Image, *, size: int = 800,
                   outer_mm: float = PRINT_OUTER_MM, inner_mm: float = PRINT_INNER_MM,
                   print_hub: bool = False) -> Image.Image:
    """Preview of the printed disc (RGBA): silver where nothing is printed, centre hole and
    outside transparent."""
    disc = _silver(size).convert("RGBA")
    art = label.convert("RGB").resize((size, size), Image.LANCZOS).convert("RGBA")
    printed = ring_mask(size, outer_mm, HUB_INNER_MM if print_hub else inner_mm)
    out = Image.composite(art, disc, printed)

    # slightly translucent stacking ring around the hole, as on real discs
    stack = ring_mask(size, 22.0, HOLE_MM)
    if not print_hub:
        tint = Image.new("RGBA", (size, size), (225, 228, 232, 255))
        out = Image.composite(tint, out, stack.point(lambda v: v * 0.55))

    # thin dark rim along the outside
    edge = ImageChops.subtract(ring_mask(size, DISC_MM, 0), ring_mask(size, DISC_MM - 0.6, 0))
    shade = Image.new("RGBA", (size, size), (110, 112, 118, 255))
    out = Image.composite(shade, out, edge)

    alpha = ImageChops.subtract(ring_mask(size, DISC_MM, 0), ring_mask(size, HOLE_MM, 0))
    out.putalpha(alpha)
    return out


def make_template(size: int = CANVAS_PX) -> Image.Image:
    """Template (RGB) with guide lines: disc, printable area, hub and centre hole."""
    img = Image.new("RGB", (size, size), (255, 255, 255))
    d = ImageDraw.Draw(img)
    c = (size - 1) / 2
    px = size / DISC_MM
    rings = [(DISC_MM, (150, 150, 150), "120 mm disc"), (PRINT_OUTER_MM, (220, 0, 0), "118 mm printable"),
             (PRINT_INNER_MM, (220, 0, 0), "24 mm printable"), (HUB_INNER_MM, (0, 120, 220), "17 mm hub"),
             (HOLE_MM, (0, 0, 0), "15 mm hole")]
    for mm, color, _name in rings:
        r = mm * px / 2
        d.ellipse([c - r, c - r, c + r, c + r], outline=color, width=max(1, size // 600))
    d.line([c, 0, c, size], fill=(200, 200, 200), width=1)
    d.line([0, c, size, c], fill=(200, 200, 200), width=1)
    y = 10
    for mm, color, name in rings:
        d.text((10, y), name, fill=color)
        y += 14
    return img
