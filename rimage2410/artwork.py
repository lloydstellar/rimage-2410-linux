"""Checks for supplied label artwork (Everest 400).

The printer takes RGB, 1889 x 1889 pixels = 120 mm at 400 dpi. Only the ring from 24 to 118 mm
is printed (see geometry.py). Colour conversion works on sRGB values, so an embedded colour
profile (Adobe RGB, Display P3, CMYK) is converted to sRGB first.

Checks:
- file: format, colour mode, colour profile, bit depth, transparency;
- resolution on the disc (taking scale and cropping into account);
- content that gets lost: a lot of detail (text, logo) in the centre hole, outside the printable
  edge or close to it; how much is cropped from a non-square image;
- large white areas: white means no ink.
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass

import numpy as np
from PIL import Image, ImageCms, ImageOps

from .geometry import CANVAS_PX, DISC_MM, DPI, HOLE_MM, HUB_INNER_MM, PRINT_INNER_MM, PRINT_OUTER_MM

GOOD_PPI = 300          # no remark from here up
LOW_PPI = 200           # below this the print looks visibly soft
SAFE_MARGIN_MM = 2.0    # keep text at least 2 mm inside the printable edge (see the PSD template)
MAX_PIXELS = 60_000_000

_SRGB = ImageCms.createProfile("sRGB")


@dataclass
class Finding:
    level: str          # 'error', 'warning' or 'info'
    code: str
    message: str


def _mm(v: float) -> str:
    return f"{v:g}"


def _profile_name(icc: bytes | None) -> str | None:
    if not icc:
        return None
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
    except (ImageCms.PyCMSError, OSError, ValueError):
        return "unreadable profile"


def _is_srgb(name: str | None) -> bool:
    return bool(name) and "srgb" in name.lower().replace(" ", "")


def to_srgb(img: Image.Image) -> tuple[Image.Image, list[Finding]]:
    """Apply EXIF rotation and convert to 8-bit sRGB (RGB or RGBA). Also returns findings."""
    notes: list[Finding] = []
    img = ImageOps.exif_transpose(img)
    icc = img.info.get("icc_profile")
    name = _profile_name(icc)
    mode = img.mode
    if mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        notes.append(Finding("info", "bit_depth", "16 bits or more per channel; converted to 8 bits, which is what the printer uses"))
        arr = np.asarray(img, dtype=np.float64)
        top = 65535.0 if mode.startswith("I;16") or arr.max() > 255 else 255.0
        img = Image.fromarray(np.clip(arr / top * 255.0, 0, 255).astype(np.uint8), "L")
        mode = "L"
    alpha = None
    if mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        alpha = img.getchannel("A")
        img = img.convert("RGB")
    elif mode == "CMYK":
        notes.append(Finding("warning", "cmyk",
                             "CMYK file: converted to RGB" + (f" with the embedded profile '{name}'" if icc else
                                                             " without a profile, so colours may be off")
                             + ". Supply RGB (sRGB) if you can: the printer converts to its ribbon itself."))
    if icc and not _is_srgb(name):
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            base = img if img.mode in ("RGB", "CMYK", "L") else img.convert("RGB")
            img = ImageCms.profileToProfile(base, src, _SRGB, renderingIntent=ImageCms.Intent.PERCEPTUAL,
                                            outputMode="RGB")
            if mode != "CMYK":
                notes.append(Finding("info", "profile", f"colour profile '{name}' converted to sRGB"))
        except (ImageCms.PyCMSError, OSError, ValueError):
            notes.append(Finding("warning", "profile", f"colour profile '{name}' could not be converted; "
                                                       "colours may be off"))
            img = img.convert("RGB")
    else:
        if not icc and mode not in ("L", "1"):
            notes.append(Finding("info", "profile", "no colour profile embedded; assuming sRGB"))
        img = img.convert("RGB")
    if alpha is not None:
        img = img.convert("RGBA")
        img.putalpha(alpha)
    return img, notes


def source_info(img: Image.Image, filename: str = "") -> dict:
    """What was supplied, in plain terms."""
    icc = img.info.get("icc_profile")
    fmt = (img.format or "").upper()
    return {"format": fmt, "width": img.width, "height": img.height, "mode": img.mode,
            "profile": _profile_name(icc) or "none", "file": filename}


def effective_ppi(w: int, h: int, fit: str, scale: float) -> float:
    """Source pixels per inch on the disc."""
    f = (max if fit == "cover" else min)(CANVAS_PX / w, CANVAS_PX / h) * max(scale, 0.01)
    return DPI / f


def _radius_mm(n: int) -> np.ndarray:
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    c = (n - 1) / 2
    return np.sqrt((xx - c) ** 2 + (yy - c) ** 2) * (DISC_MM / n)


def check(img_srgb: Image.Image, placed: Image.Image, *, fit: str, scale: float, print_hub: bool,
          source_size: tuple[int, int] | None = None, inner_mm: float = PRINT_INNER_MM,
          outer_mm: float = PRINT_OUTER_MM) -> list[Finding]:
    """Run the checks on the placed image (placed: square canvas before the ring mask).

    source_size: size of the original file, if img_srgb is a scaled-down copy.
    """
    out: list[Finding] = []
    w, h = source_size or img_srgb.size
    if w * h > MAX_PIXELS:
        out.append(Finding("warning", "large", f"very large file ({w} x {h} px); 1889 x 1889 px is enough"))
    ppi = effective_ppi(w, h, fit, scale)
    if ppi < LOW_PPI:
        out.append(Finding("warning", "resolution",
                           f"too few pixels: about {ppi:.0f} ppi on the disc (the printer prints at 400). "
                           "The label will look soft. Supply at least 1889 x 1889 px."))
    elif ppi < GOOD_PPI:
        out.append(Finding("info", "resolution",
                           f"about {ppi:.0f} ppi on the disc: usable, but 1889 x 1889 px (400 ppi) is sharper"))
    if abs(w - h) > 0.02 * max(w, h):
        ratio = min(w, h) / max(w, h)
        if fit == "cover":
            out.append(Finding("info", "crop", f"not square: {100 * (1 - ratio):.0f}% of the image "
                                               "is cropped at the sides"))
        else:
            out.append(Finding("info", "borders", "not square: the background colour fills the space beside the image"))
    if img_srgb.mode == "RGBA" and np.asarray(img_srgb.getchannel("A")).min() < 250:
        out.append(Finding("info", "transparency", "the image has transparent areas; they are filled "
                                                   "with the background colour"))

    # detail per zone (strong edges in the greyscale), measured on the placed image
    n = placed.width
    g = np.asarray(placed.convert("L"), dtype=np.float32)
    edges = np.zeros_like(g, dtype=bool)
    edges[:, :-1] |= np.abs(np.diff(g, axis=1)) > 40
    edges[:-1, :] |= np.abs(np.diff(g, axis=0)) > 40
    r = _radius_mm(n)
    inner = HUB_INNER_MM if print_hub else inner_mm
    ring = (r >= inner / 2) & (r < outer_mm / 2)
    centre = r < inner / 2
    outside = r >= outer_mm / 2
    band = (r >= (outer_mm / 2 - SAFE_MARGIN_MM)) & (r < outer_mm / 2)
    d_ring = float(edges[ring].mean()) if ring.any() else 0.0
    d_centre = float(edges[centre].mean()) if centre.any() else 0.0
    if (d_centre > 0.06 and d_centre > 1.2 * d_ring) or (d_centre > 0.015 and d_centre > 3 * d_ring):
        where = "17 mm (the hub)" if print_hub else f"{_mm(inner_mm)} mm"
        out.append(Finding("warning", "centre",
                           f"there is a lot of detail in the centre (within {where}), such as text or a logo. "
                           f"It will not be printed: the centre hole is {HOLE_MM:.0f} mm and printing starts at {where}. "
                           "Move or shrink the image, or make your own disc design with the template."))
    lost = _busy_sectors(edges, r, outside & (r < DISC_MM * 0.75), 0.06, d_ring)
    if lost:
        out.append(Finding("warning", "outer_edge",
                           f"detail outside {_mm(outer_mm)} mm will be lost ({', '.join(lost)}), such as small "
                           "text in the corners. Shrink the image or keep text inside the ring."))
    near = _busy_sectors(edges, r, band, 0.12, d_ring)
    if near:
        out.append(Finding("info", "safe_zone",
                           f"a lot of detail in the last {SAFE_MARGIN_MM:.0f} mm before the edge ({', '.join(near)}); "
                           "move text a little further in, in case the print shifts slightly"))
    rgb = np.asarray(placed.convert("RGB"))
    white = (rgb > 245).all(axis=-1)
    frac_white = float(white[ring].mean()) if ring.any() else 0.0
    if frac_white > 0.3:
        out.append(Finding("info", "white", f"{100 * frac_white:.0f}% of the printable ring is white. White means "
                                            "no ink: the bare disc surface shows there."))
    return out


_DIRECTIONS = ["right", "bottom right", "bottom", "bottom left", "left", "top left", "top", "top right"]


def _busy_sectors(edges: np.ndarray, r: np.ndarray, zone: np.ndarray, minimum: float,
                  d_ring: float = 0.0) -> list[str]:
    """Directions (8 sectors of 45 degrees) where the zone has a lot of detail: above `minimum`, or
    (for a calm image) much more than the average in the printable ring."""
    n = edges.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    c = (n - 1) / 2
    ang = (np.degrees(np.arctan2(yy - c, xx - c)) + 22.5) % 360       # 0 = right, clockwise
    names = []
    for i, name in enumerate(_DIRECTIONS):
        z = zone & (ang >= i * 45) & (ang < (i + 1) * 45)
        d = float(edges[z].mean()) if z.sum() > 50 else 0.0
        if d > minimum or (d > 0.015 and d > 3 * d_ring):
            names.append(name)
    return names


def findings_dicts(items: list[Finding]) -> list[dict]:
    order = {"error": 0, "warning": 1, "info": 2}
    return [asdict(f) for f in sorted(items, key=lambda f: order.get(f.level, 3))]
