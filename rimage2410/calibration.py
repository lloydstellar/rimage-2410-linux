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
# grey patches for the black ribbon: same layout as PATCHES, percentage black
GREY_PATCHES = [
    [(f"{k}%", (round(255 * (1 - k / 100)),) * 3) for k in (0, 10, 20, 30, 40, 50)],
    [(f"{k}%", (round(255 * (1 - k / 100)),) * 3) for k in (60, 70, 80, 90, 100, 5)],
]
TINT = (175, 212, 236)
STEP_MM = 0.25


def _px(mm: float) -> float:
    return mm / 25.4 * DPI


def _font(size_px: int, bold: bool = True) -> ImageFont.ImageFont:
    names = ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf") if bold else ("DejaVuSans.ttf", "LiberationSans-Regular.ttf")
    for name in names:
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
                    patches: bool = True, mono: bool = False) -> Image.Image:
    """Calibration label (RGB, CANVAS_PX x CANVAS_PX). inner_mm and outer_mm: nominal edges of the white layer.
    patches: also draw rows of colour patches above the centre. mono: grey patches instead of colour
    (for the black ribbon; the red zero tick prints dark grey there, but stays longer and thicker)."""
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
        for row, items in enumerate(GREY_PATCHES if mono else PATCHES):
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
    on one colour plane (C by default). Returns the planes for build_job and the label (RGB).
    plane 0: black ribbon, with grey patches (GREY_PATCHES). The gain uses the same cap as
    plane_gain in printer.json (alignment.gain_planes)."""
    import numpy as np
    from . import alignment
    from .everest import color
    names = {0: "black", 1: "cyan", 2: "magenta", 3: "yellow"}
    mono = plane == 0
    patch_rows = GREY_PATCHES if mono else PATCHES
    img = Image.new("RGB", (CANVAS_PX, CANVAS_PX), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    c = (CANVAS_PX - 1) / 2
    w, h, gap = 8.0, 4.5, 1.0
    tops = (-51.0, -36.5, 13.5, 28.0)
    bands = []
    for top, g in zip(tops, gains):
        _text(img, f"{names[plane]} x{g:.2f}", (c, c + _px(top + 1.0)), 0, round(_px(2.0)))
        for row, items in enumerate(patch_rows):
            y0 = top + 2.5 + row * (h + gap)
            for col, (_n, rgb) in enumerate(items):
                x0 = -(6 * w + 5 * gap) / 2 + col * (w + gap)
                draw.rectangle([c + _px(x0), c + _px(y0), c + _px(x0 + w), c + _px(y0 + h)], fill=rgb)
        bands.append((top + 2.0, top + 2.5 + 2 * (h + gap), g))
    if mono:
        _text(img, "Order: 0, 10, 20, 30, 40, 50% /", (c, c - _px(5 + 15.5)), 0, round(_px(1.6)))
        _text(img, "60, 70, 80, 90, 100, 5% black", (c, c + _px(-15.5 + 2.2 - 5)), 0, round(_px(1.6)))
    else:
        _text(img, "Order: black, grey 25, 50, 75, blue, navy /", (c, c - _px(5 + 15.5)), 0, round(_px(1.6)))
        _text(img, "cyan, magenta, yellow, red, green, sky", (c, c + _px(-15.5 + 2.2 - 5)), 0, round(_px(1.6)))
    shifted = alignment.shift(img, *offset_mm)
    planes = color.convert(shifted, ribbon="mono" if mono else "cmy", method=method, colormatch=colormatch)
    dy = -round(offset_mm[1] / 25.4 * DPI)
    for y_top, y_bot, g in bands:
        if g == 1.0:
            continue
        rows = slice(max(1, round(c + _px(y_top)) + dy), round(c + _px(y_bot)) + dy)
        v = planes[plane][rows].astype(np.float32)      # same formula as alignment.gain_planes
        planes[plane][rows] = np.maximum(v, np.minimum(np.rint(v * g), alignment.GAIN_CAP)).astype(np.uint8)
    planes = alignment.shift_planes(planes, plane_shift or {})
    return planes, img


RAMP_FINE = (88, 92, 96, 100, 104, 108, 112, 116, 120)    # around the threshold found with the colour ribbon


def ramp_planes_mono(energies=RAMP_ENERGIES, fine=RAMP_FINE, offset_mm=(0.0, 0.0)) -> tuple[dict, Image.Image]:
    """Ramp disc for the black ribbon: fixed energy in plane 0, bypassing the colour conversion.

    Rows: patches from 16 to 144, patches in small steps around the threshold (88 to 120), and
    single 1-pixel dots (1 in 3 x 3) at the same energies as the first row. The last row shows
    whether small dots print; light greys are made of them.
    Returns the planes (ready for everest.job.build_job with ribbon 'mono') and a rough preview.
    """
    import numpy as np
    from .everest import color, halftone
    if max(max(energies), max(fine)) > RAMP_MAX:
        raise ValueError(f"energy higher than {RAMP_MAX}")
    n = CANVAS_PX
    c = (n - 1) / 2
    K = np.zeros((n, n), np.uint8)
    text = Image.new("L", (n, n), 0)
    td = ImageDraw.Draw(text)

    def put_text(s, x_mm, y_mm, size_mm):
        font = _font(round(_px(size_mm)))
        b = font.getbbox(s)
        td.text((c + _px(x_mm) - (b[0] + b[2]) / 2, c + _px(y_mm) - (b[1] + b[3]) / 2), s, font=font, fill=255)

    dots = np.zeros((n, n), bool)
    dots[::3, ::3] = True
    w, gap, x_start = 7.0, 1.0, -31.0
    rows = [("solid", energies, -38.0, -40.6, False), ("fine", fine, -29.0, -20.6, False),
            ("dots", energies, 14.0, 23.6, True)]
    for name, values, y0, y_num, dotted in rows:
        put_text(name, x_start - 5.0, y0 + w / 2, 2.0)
        for i, e in enumerate(values):
            x0 = x_start + i * (w + gap)
            ys = slice(round(c + _px(y0)), round(c + _px(y0 + w)))
            xs = slice(round(c + _px(x0)), round(c + _px(x0 + w)))
            K[ys, xs] = np.where(dots[ys, xs], e, 0) if dotted else e
            put_text(str(e), x0 + w / 2, y_num, 1.8)
    put_text("Black ribbon: energy per patch, no conversion", 0, -46.0, 2.2)
    put_text("At which number does it show, when is it solid?", 0, 30.0, 2.0)
    K[np.asarray(text) > 127] = RAMP_MAX
    preview = Image.fromarray(255 - K, "L").convert("RGB")
    dy = -round(offset_mm[1] / 25.4 * DPI)
    dx = round(offset_mm[0] / 25.4 * DPI)
    K = np.roll(np.roll(K, dy, axis=0), dx, axis=1)[:, ::-1]
    flags = halftone.make_flags("mono")
    return color._finish({0: K}, flags, True), preview


def grey_label() -> Image.Image:
    """Grey ladder for the black ribbon (RGB), sent through the normal conversion.

    Above the centre: patches from 0 to 100% black in steps of 10%, a row of light greys
    (2 to 40%) and a smooth gradient. Below the centre: text in 6, 8 and 10 point and lines of
    1, 2 and 3 pixels, horizontal and vertical.
    """
    img = Image.new("RGB", (CANVAS_PX, CANVAS_PX), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    c = (CANVAS_PX - 1) / 2

    def rect(x0, y0, x1, y1, fill):
        draw.rectangle([c + _px(x0), c + _px(y0), c + _px(x1) - 1, c + _px(y1) - 1], fill=fill)

    def grey(pct):
        return (round(255 * (1 - pct / 100)),) * 3

    _text(img, "Grey ladder black ribbon", (c, c - _px(52)), 0, round(_px(2.6)))
    w, h, gap = 7.0, 5.0, 0.8
    for row, (pcts, y0) in enumerate((((0, 10, 20, 30, 40, 50), -48.0), ((60, 70, 80, 90, 100), -40.0),
                                      ((2, 4, 6, 8, 10, 15, 20, 25, 30, 40), -32.0))):
        x_left = -(len(pcts) * w + (len(pcts) - 1) * gap) / 2
        for i, pct in enumerate(pcts):
            x0 = x_left + i * (w + gap)
            rect(x0, y0, x0 + w, y0 + h, grey(pct))
            if pct == 0:                            # white patch: thin outline so you can see where it is
                draw.rectangle([c + _px(x0), c + _px(y0), c + _px(x0 + w) - 1, c + _px(y0 + h) - 1], outline=(0, 0, 0))
            _text(img, f"{pct}", (c + _px(x0 + w / 2), c + _px(y0 + h + 1.1)), 0, round(_px(1.3)))
    # gradient from white to black, 80 mm wide
    x0, x1, y0, y1 = -40.0, 40.0, -23.0, -18.0
    a, b = round(c + _px(x0)), round(c + _px(x1))
    for x in range(a, b):
        draw.line([(x, c + _px(y0)), (x, c + _px(y1) - 1)], fill=grey(100 * (x - a) / (b - a - 1)))
    _text(img, "0%  gradient  100%", (c, c + _px(-15.8)), 0, round(_px(1.4)))
    # text in 6, 8 and 10 point (1 point = 1/72 inch)
    for k, (pt, y) in enumerate(((10, 15.0), (8, 20.0), (6, 24.5))):
        font = _font(round(pt / 72 * DPI), bold=False)
        s = f"{pt} pt: The quick brown fox 0123456789"
        bb = font.getbbox(s)
        draw.text((c - (bb[0] + bb[2]) / 2, c + _px(y) - (bb[1] + bb[3]) / 2), s, font=font, fill=(0, 0, 0))
    # lines of 1, 2 and 3 pixels: horizontal (left) and vertical (right)
    for k, width in enumerate((1, 2, 3)):
        xl = c + _px(-36.0 + k * 9.0)
        for j in range(6):
            y = round(c + _px(30.0) + j * 3 * width * 2)
            draw.rectangle([xl, y, xl + _px(7.0), y + width - 1], fill=(0, 0, 0))
        xr = round(c + _px(10.0 + k * 9.0))
        for j in range(6):
            x = xr + j * 3 * width * 2
            draw.rectangle([x, c + _px(30.0), x + width - 1, c + _px(37.0)], fill=(0, 0, 0))
        _text(img, f"{width} px", (xl + _px(3.5), c + _px(39.5)), 0, round(_px(1.4)))
        _text(img, f"{width} px", (xr + _px(3.5), c + _px(39.5)), 0, round(_px(1.4)))
    return img
