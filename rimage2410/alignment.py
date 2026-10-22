"""Alignment of the print on the disc: a fixed correction per printer.

A printer may place the image slightly off-centre on the disc. Measure the offset with the
calibration disc (calibration.py) and store the correction; the image is then shifted back just
before the print job is built. The alignment stored in the printer's EEPROM is never changed
(writing it with ESC!g1 is not allowed).

All corrections live in <data dir>/printer.json. The defaults are no correction.

"offset_x_mm" and "offset_y_mm": x to the right, y up, in mm, seen from the top of the artwork.
A positive y moves the image up. A measured offset d needs a correction of -d.

"plane_shift_lines": {"1": 1, ...}: move a colour plane (1 = C, 2 = M, 3 = Y) down by this many
image lines (0.0635 mm each). Use it when one colour lands consistently higher or lower than the
others. It can only remove the average misregistration, not the variation between prints.

"plane_gain": {"1": 1.2}: gain per colour plane after the colour conversion. Use it when small
dots of one colour do not transfer well to the film. Too much gain overheats the print head and
gives broken areas, so a pixel is never raised above GAIN_CAP, and a pixel that is already
higher stays as it is.
"""

from __future__ import annotations

import json
import os

import numpy as np
from PIL import Image

from .geometry import DPI

FILE = "printer.json"
LIMIT_MM = 2.0


def load(data_dir: str) -> tuple[float, float]:
    """Correction (x, y) in mm from the data directory; (0, 0) when nothing is set."""
    try:
        with open(os.path.join(data_dir, FILE), encoding="utf-8") as f:
            d = json.load(f)
        return _check(float(d.get("offset_x_mm", 0.0)), float(d.get("offset_y_mm", 0.0)))
    except FileNotFoundError:
        return (0.0, 0.0)


def save(data_dir: str, x_mm: float, y_mm: float) -> None:
    x_mm, y_mm = _check(x_mm, y_mm)
    path = os.path.join(data_dir, FILE)
    d = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    d.update(offset_x_mm=x_mm, offset_y_mm=y_mm)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


def load_plane_shift(data_dir: str) -> dict[int, int]:
    try:
        with open(os.path.join(data_dir, FILE), encoding="utf-8") as f:
            d = json.load(f).get("plane_shift_lines", {})
    except FileNotFoundError:
        return {}
    out = {int(k): int(v) for k, v in d.items()}
    if any(abs(v) > 16 for v in out.values()):
        raise ValueError("colour plane shift larger than 16 lines (1 mm)")
    return out


def save_plane_shift(data_dir: str, shifts: dict[int, int]) -> None:
    path = os.path.join(data_dir, FILE)
    d = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    d["plane_shift_lines"] = {str(k): int(v) for k, v in shifts.items()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


GAIN_CAP = 200


def load_plane_gain(data_dir: str) -> dict[int, float]:
    try:
        with open(os.path.join(data_dir, FILE), encoding="utf-8") as f:
            d = json.load(f).get("plane_gain", {})
    except FileNotFoundError:
        return {}
    out = {int(k): float(v) for k, v in d.items()}
    if any(not 0.5 <= v <= 1.5 for v in out.values()):
        raise ValueError("colour plane gain outside 0.5 to 1.5")
    return out


def save_plane_gain(data_dir: str, gains: dict[int, float]) -> None:
    path = os.path.join(data_dir, FILE)
    d = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    d["plane_gain"] = {str(k): float(v) for k, v in gains.items()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


def gain_planes(planes: dict[int, np.ndarray], gains: dict[int, float],
                cap: int = GAIN_CAP) -> dict[int, np.ndarray]:
    """Multiply the energy per colour plane, with a cap against overheating.

    Value v becomes max(v, min(v * g, cap)): never lower than the original, and the gain never
    pushes it above cap. The stamp in line 0 is kept."""
    out = dict(planes)
    for p, g in gains.items():
        if p not in planes or g == 1.0:
            continue
        a = planes[p]
        v = a.astype(np.float32)
        b = np.maximum(v, np.minimum(np.rint(v * g), cap)).astype(np.uint8)
        b[0] = a[0]
        out[p] = b
    return out


def shift_planes(planes: dict[int, np.ndarray], shifts: dict[int, int]) -> dict[int, np.ndarray]:
    """Shift colour planes vertically; positive = down. Padded with 0 (no ink).

    Line 0 of the first plane holds the stamp from the colour conversion; it is kept."""
    out = dict(planes)
    for p, n in shifts.items():
        if p not in planes or n == 0:
            continue
        a = planes[p]
        b = np.zeros_like(a)
        if n > 0:
            b[n:] = a[:-n]
        else:
            b[:n] = a[-n:]
        b[0] = a[0]                             # keep the stamp and the first line
        out[p] = b
    return out


def _check(x_mm: float, y_mm: float) -> tuple[float, float]:
    if abs(x_mm) > LIMIT_MM or abs(y_mm) > LIMIT_MM:
        raise ValueError(f"alignment larger than {LIMIT_MM:g} mm; that is no longer a small correction")
    return (x_mm, y_mm)


def shift(img: Image.Image, x_mm: float, y_mm: float) -> Image.Image:
    """Shift the image by whole pixels (0.0635 mm), padded with white."""
    dx = round(x_mm / 25.4 * DPI)
    dy = -round(y_mm / 25.4 * DPI)              # image y runs downwards
    if dx == 0 and dy == 0:
        return img
    out = Image.new(img.mode, img.size, (255,) * len(img.getbands()))
    out.paste(img, (dx, dy))
    return out
