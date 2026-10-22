"""Calibration label to measure the alignment of the print on the disc.

At 8 positions (every 45 degrees) a ruler crosses the inner and the outer edge of the printable
layer. Each ruler has ticks every 0.25 mm; the red tick (0) sits exactly on the nominal edge of
the white layer (FalconMedia: 23 and 118 mm). Long tick = whole mm, medium tick = half mm.

Reading: at which tick does the white of the disc start (inner) or end (outer)? Count from the
red tick: + is away from the centre, - towards it. If the print is shifted d mm in some
direction, you read -d in that direction and +d on the opposite side (same for inner and
outer). The offset is therefore (opposite side - this side) / 2.

Behind the rulers a light colour covers the normal print area, so you also see any white
edges as on a real label.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

from .geometry import CANVAS_PX, DPI, MEDIA

DIRECTIONS = ["T", "TR", "R", "BR", "B", "BL", "L", "TL"]   # top, top right, ... (clockwise)
# colour patches (sRGB) to judge colour casts: grey and black should look neutral
PATCHES = [
    [("black", (0, 0, 0)), ("grey 25", (64, 64, 64)), ("grey 50", (128, 128, 128)),
     ("grey 75", (192, 192, 192)), ("blue", (0, 0, 255)), ("navy", (20, 40, 120))],
    [("cyan", (0, 255, 255)), ("magenta", (255, 0, 255)), ("yellow", (255, 255, 0)),
     ("red", (255, 0, 0)), ("green", (0, 160, 0)), ("sky", (90, 150, 220))],
]
TINT = (175, 212, 236)
STEP_MM = 0.25


def _px(mm: float) -> float:
    return mm / 25.4 * DPI


def _font(size_px: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _text(img: Image.Image, text: str, centre: tuple[float, float], angle_deg: float, size_px: int,
          fill=(0, 0, 0)) -> None:
    """Text centred on centre, rotated so it reads from the middle of the disc."""
    font = _font(size_px)
    box = font.getbbox(text)
    w, h = box[2] - box[0] + 4, box[3] - box[1] + 4
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((2 - box[0], 2 - box[1]), text, font=font, fill=fill + (255,))
    tile = tile.rotate(-angle_deg, expand=True, resample=Image.BICUBIC)
    img.paste(tile, (round(centre[0] - tile.width / 2), round(centre[1] - tile.height / 2)), tile)


def _ruler(img: Image.Image, draw: ImageDraw.ImageDraw, angle_deg: float, edge_mm: float,
           lo: float, hi: float) -> None:
    """Ruler along the radius in direction angle_deg (0 = top, clockwise) around radius edge_mm."""
    c = (CANVAS_PX - 1) / 2
    a = math.radians(angle_deg)
    ux, uy = math.sin(a), -math.cos(a)          # radial direction
    tx, ty = -uy, ux                            # perpendicular to it
    n_lo, n_hi = round(lo / STEP_MM), round(hi / STEP_MM)
    for i in range(n_lo, n_hi + 1):
        v = i * STEP_MM
        r = _px(edge_mm + v)
        if i == 0:
            half, width, colour = _px(2.2), 3, (220, 0, 0)
        elif i % 4 == 0:
            half, width, colour = _px(1.6), 2, (0, 0, 0)
        elif i % 2 == 0:
            half, width, colour = _px(1.1), 2, (0, 0, 0)
        else:
            half, width, colour = _px(0.6), 2, (0, 0, 0)
        px, py = c + ux * r, c + uy * r
        draw.line([(px - tx * half, py - ty * half), (px + tx * half, py + ty * half)], fill=colour, width=width)
        if i == 0 or i % 4 == 0:
            side = -1 if i == 0 else 1
            lx, ly = px + tx * side * _px(3.3), py + ty * side * _px(3.3)
            _text(img, "0" if i == 0 else f"{v:+.0f}", (lx, ly), angle_deg, round(_px(1.3)),
                  (220, 0, 0) if i == 0 else (0, 0, 0))


def alignment_label(inner_mm: float = 23.0, outer_mm: float = 118.0,
                    tint_inner_mm: float = MEDIA["falcon"]["inner"], tint_outer_mm: float = MEDIA["falcon"]["outer"],
                    patches: bool = True) -> Image.Image:
    """Calibration label (RGB, CANVAS_PX x CANVAS_PX). inner_mm and outer_mm: nominal edges of the white layer.
    patches: also draw rows of colour patches above the centre."""
    img = Image.new("RGB", (CANVAS_PX, CANVAS_PX), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    c = (CANVAS_PX - 1) / 2
    ro, ri = _px(tint_outer_mm / 2), _px(tint_inner_mm / 2)
    draw.ellipse([c - ro, c - ro, c + ro, c + ro], fill=TINT)
    draw.ellipse([c - ri, c - ri, c + ri, c + ri], fill=(255, 255, 255))
    max_out = (CANVAS_PX / 2 - 2) * 25.4 / DPI - outer_mm / 2 - 0.05
    for k, name in enumerate(DIRECTIONS):
        ang = k * 45
        _ruler(img, draw, ang, outer_mm / 2, -1.5, math.floor(max_out / STEP_MM) * STEP_MM)
        _ruler(img, draw, ang, inner_mm / 2, -1.5, 1.5)
        a = math.radians(ang)
        for r_mm, size in ((outer_mm / 2 - 5.5, 2.4), (inner_mm / 2 + 4.6, 1.8)):
            r = _px(r_mm)
            _text(img, name, (c + math.sin(a) * r, c - math.cos(a) * r), ang, round(_px(size)))
    _text(img, "TOP", (c, c - _px(38)), 0, round(_px(3.5)))
    if patches:
        w, h, gap = 11.0, 5.0, 1.0
        for row, items in enumerate(PATCHES):
            y0 = -34.0 + row * (h + 2.5)
            for col, (name, rgb) in enumerate(items):
                x0 = -(6 * w + 5 * gap) / 2 + col * (w + gap)
                draw.rectangle([c + _px(x0), c + _px(y0), c + _px(x0 + w), c + _px(y0 + h)], fill=rgb)
                _text(img, name, (c + _px(x0 + w / 2), c + _px(y0 + h + 1.3)), 0, round(_px(1.5)))
    for i, line in enumerate(["Alignment: on each ruler, read the tick where",
                              "the white of the disc starts (inner) or ends (outer).",
                              "Red = 0, + = away from the centre, ticks 0.25 mm."]):
        _text(img, line, (c, c + _px(24 + 3.2 * i)), 0, round(_px(2.0)))
    return img


def offset_from_readings(readings: dict[str, float]) -> tuple[float, float]:
    """Offset of the print (x to the right, y up, mm) from the readings per direction.

    readings: direction (T, TR, ...) to the value read. For each direction, reading = -(d . u).
    Least squares over all given directions.
    """
    sxx = sxy = syy = bx = by = 0.0
    for name, val in readings.items():
        a = math.radians(DIRECTIONS.index(name) * 45)
        ux, uy = math.sin(a), math.cos(a)        # y up
        sxx += ux * ux; sxy += ux * uy; syy += uy * uy
        bx += -val * ux; by += -val * uy
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9:
        raise ValueError("too few directions: give at least two directions that are not opposite each other")
    return ((syy * bx - sxy * by) / det, (sxx * by - sxy * bx) / det)


RAMP_ENERGIES = (16, 32, 48, 64, 80, 96, 112, 128, 144)
RAMP_MAX = 144          # the colour conversion reaches this for full black; never send anything hotter


def ramp_planes(energies=RAMP_ENERGIES, offset_mm=(0.0, 0.0), plane_shift=None) -> tuple[dict, Image.Image]:
    """Test patches with a fixed energy per colour plane, bypassing the colour conversion.

    Rows: C only, M only, Y only, and C+M+Y together (should be grey). Returns the planes
    (ready for everest.job.build_job) and a rough RGB preview (255 - energy per channel).
    """
    import numpy as np
    from . import alignment
    from .everest import color, halftone
    if max(energies) > RAMP_MAX:
        raise ValueError(f"energy higher than {RAMP_MAX}")
    n = CANVAS_PX
    c = (n - 1) / 2
    E = {p: np.zeros((n, n), np.uint8) for p in (1, 2, 3)}
    text = Image.new("L", (n, n), 0)
    td = ImageDraw.Draw(text)

    def put_text(s, x_mm, y_mm, size_mm):
        font = _font(round(_px(size_mm)))
        b = font.getbbox(s)
        td.text((c + _px(x_mm) - (b[0] + b[2]) / 2, c + _px(y_mm) - (b[1] + b[3]) / 2), s, font=font, fill=255)

    w, gap = 7.0, 1.0
    rows = [("C", (1,), -38.0), ("M", (2,), -29.0), ("Y", (3,), -20.0), ("CMY", (1, 2, 3), 14.0)]
    x_start = -31.0
    for name, planes, y0 in rows:
        put_text(name, x_start - 4.5, y0 + w / 2, 2.4)
        for i, e in enumerate(energies):
            x0 = x_start + i * (w + gap)
            ys = slice(round(c + _px(y0)), round(c + _px(y0 + w)))
            xs = slice(round(c + _px(x0)), round(c + _px(x0 + w)))
            for p in planes:
                E[p][ys, xs] = e
    for i, e in enumerate(energies):
        x = x_start + i * (w + gap) + w / 2
        put_text(str(e), x, -40.6, 1.8)
        put_text(str(e), x, 23.6, 1.8)
    put_text("Energy per colour plane, no colour conversion", 0, -46.0, 2.2)
    put_text("At which number does each colour become visible?", 0, 30.0, 2.0)
    mask = np.asarray(text) > 127
    for p in E:
        E[p][mask] = RAMP_MAX
    preview = Image.fromarray(np.dstack([255 - E[1], 255 - E[2], 255 - E[3]]), "RGB")
    # same post-processing as a normal job: alignment, mirroring, 1920-byte lines, stamp
    dy = -round(offset_mm[1] / 25.4 * DPI)
    dx = round(offset_mm[0] / 25.4 * DPI)
    for p in E:
        E[p] = np.roll(np.roll(E[p], dy, axis=0), dx, axis=1)[:, ::-1]
    flags = halftone.make_flags("cmy", 1, False, True, True)
    planes = color._finish(E, flags, False)
    planes = alignment.shift_planes(planes, plane_shift or {})
    return planes, preview


def gain_test(gains=(1.0, 1.15, 1.3, 1.5), plane: int = 1, method=0, colormatch: int = 1,
              offset_mm=(0.0, 0.0), plane_shift=None) -> tuple[dict, Image.Image]:
    """Test disc: four bands with the same colour patches (PATCHES), each band with a different gain
    on one colour plane (C by default). Returns the planes for build_job and the label (RGB)."""
    import numpy as np
    from . import alignment
    from .everest import color
    names = {1: "cyan", 2: "magenta", 3: "yellow"}
    img = Image.new("RGB", (CANVAS_PX, CANVAS_PX), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    c = (CANVAS_PX - 1) / 2
    w, h, gap = 8.0, 4.5, 1.0
    tops = (-51.0, -36.5, 13.5, 28.0)
    bands = []
    for top, g in zip(tops, gains):
        _text(img, f"{names[plane]} x{g:.2f}", (c, c + _px(top + 1.0)), 0, round(_px(2.0)))
        for row, items in enumerate(PATCHES):
            y0 = top + 2.5 + row * (h + gap)
            for col, (_n, rgb) in enumerate(items):
                x0 = -(6 * w + 5 * gap) / 2 + col * (w + gap)
                draw.rectangle([c + _px(x0), c + _px(y0), c + _px(x0 + w), c + _px(y0 + h)], fill=rgb)
        bands.append((top + 2.0, top + 2.5 + 2 * (h + gap), g))
    _text(img, "Order: black, grey 25, 50, 75, blue, navy /", (c, c - _px(5 + 15.5)), 0, round(_px(1.6)))
    _text(img, "cyan, magenta, yellow, red, green, sky", (c, c + _px(-15.5 + 2.2 - 5)), 0, round(_px(1.6)))
    shifted = alignment.shift(img, *offset_mm)
    planes = color.convert(shifted, ribbon="cmy", method=method, colormatch=colormatch)
    dy = -round(offset_mm[1] / 25.4 * DPI)
    for y_top, y_bot, g in bands:
        if g == 1.0:
            continue
        rows = slice(max(1, round(c + _px(y_top)) + dy), round(c + _px(y_bot)) + dy)
        a = planes[plane][rows].astype(np.float32) * g
        planes[plane][rows] = np.clip(np.rint(a), 0, 255).astype(np.uint8)
    planes = alignment.shift_planes(planes, plane_shift or {})
    return planes, img
