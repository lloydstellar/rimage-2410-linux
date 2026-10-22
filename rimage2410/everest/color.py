"""Everest 400 colour conversion: RGB label to colour planes with 8-bit heat energy.

This module only builds arrays; it does not talk to any device.

Output: one uint8 array per plane with shape (lines, 1920). Each line is 1889 pixels of
converted image plus 31 zeros. The image is mirrored, because the retransfer film flips it
back when it is transferred to the disc.
"""

from __future__ import annotations

import functools
import json
import os

import numpy as np

from ..geometry import CANVAS_PX, LINE_PX

DATA = os.path.join(os.path.dirname(__file__), "data")
PAD_RIGHT = LINE_PX - CANVAS_PX          # 31 zeros after each line
PLANES = {"mono": (0,), "cmy": (1, 2, 3), "cmyw": (1, 2, 3, 4)}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _index() -> dict:
    with open(os.path.join(DATA, "index.json")) as f:
        return json.load(f)


@functools.lru_cache(maxsize=None)
def table(name: str, dtype: str = "u1") -> np.ndarray:
    """A table from everest/data, with aliases resolved. dtype: u1, <u2, <u4, <i4."""
    idx = _index()
    while "alias_of" in idx.get(name, {}):
        name = idx[name]["alias_of"]
    path = os.path.join(DATA, name + ".bin")
    raw = np.fromfile(path, dtype=np.uint8)
    dt = np.dtype(dtype)
    raw = raw[: len(raw) // dt.itemsize * dt.itemsize]
    return raw.view(dt).copy()


def lum_gray(rgb: np.ndarray) -> np.ndarray:
    """gray = lum_r[R] + lum_g[G] + lum_b[B] (16-bit sum), as used for the black ribbon."""
    lr, lg, lb = table("lum_r_u16x256", "<u2"), table("lum_g_u16x256", "<u2"), table("lum_b_u16x256", "<u2")
    s = lr[rgb[..., 0]].astype(np.uint32) + lg[rgb[..., 1]] + lb[rgb[..., 2]]
    return (s & 0xFFFF).astype(np.uint32)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def _as_rgb(label_rgb) -> np.ndarray:
    if hasattr(label_rgb, "convert"):
        arr = np.asarray(label_rgb.convert("RGB"), dtype=np.uint8)
    else:
        arr = np.asarray(label_rgb, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError("expected an RGB image (height, width, 3)")
    if arr.shape[1] != CANVAS_PX:
        raise ValueError(f"the label must be {CANVAS_PX} pixels wide (400 dpi, 120 mm)")
    return arr


def _stamp(flags: int, mono: bool) -> list[int]:
    """7 stamp bytes at the end of the first output line (C or K plane)."""
    if mono:
        return [1, 6, 0, 0, 0, 0, flags & 0xFF]
    return [1, 6, 0, 0, 0, flags & 3, ((flags & 0xFF) >> 4) + 1]


def _finish(planes: dict[int, np.ndarray], flags: int, mono: bool) -> dict[int, np.ndarray]:
    """The image is already mirrored; pad the lines to 1920 and add the stamp."""
    out = {}
    for p, a in planes.items():
        h = a.shape[0]
        full = np.zeros((h, LINE_PX), dtype=np.uint8)
        full[:, :CANVAS_PX] = a
        out[p] = full
    first = 0 if mono else 1
    if first in out and out[first].shape[0] > 0:
        out[first][0, CANVAS_PX - 7:CANVAS_PX] = _stamp(flags, mono)
    return out


# ---------------------------------------------------------------------------
# Simple mode (flag 0x200: invert only)
# ---------------------------------------------------------------------------

def _simple(rgb: np.ndarray, ribbon: str, white_mask: np.ndarray | None) -> dict[int, np.ndarray]:
    if ribbon == "mono":
        k = 255 - (lum_gray(rgb) >> 8)
        return {0: np.clip(k, 0, 255).astype(np.uint8)}
    planes = {1: 255 - rgb[..., 0], 2: 255 - rgb[..., 1], 3: 255 - rgb[..., 2]}
    if ribbon == "cmyw":
        if white_mask is not None:
            planes[4] = (255 - np.asarray(white_mask, dtype=np.uint8))
        else:
            white = np.all(rgb == 255, axis=2)
            planes[4] = np.where(white, 0, 255).astype(np.uint8)
    return {p: a.astype(np.uint8) for p, a in planes.items()}


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def convert(label_rgb, *, ribbon: str = "cmy", method=0, white_mask: np.ndarray | None = None,
            colormatch: int = 1, alt: bool = False, heat: bool = True, median: bool = True) -> dict[int, np.ndarray]:
    """Label (RGB, 1889 wide) to colour planes.

    ribbon: 'cmy' -> {1: C, 2: M, 3: Y}; 'cmyw' -> {1..4}; 'mono' -> {0: K}.
    method: 0 or 2 (halftone methods) or 'simple' (invert only).
    colormatch: 0 (off), 1 or 2 (3D LUT set). alt: table set 0x10 (only useful with method 2).
    white_mask: optional (only with 'cmyw'), uint8 (height, 1889):
    0xFF = no white underneath (white pixel), 0 = white underneath. W = 255 - white_mask.
    Without a mask every non-white pixel gets white underneath.
    heat: heat compensation (off = flag 0x100). median: median filter (off = flag 0x800).
    """
    if ribbon not in PLANES:
        raise ValueError("ribbon must be 'cmy', 'cmyw' or 'mono'")
    rgb = _as_rgb(label_rgb)[:, ::-1, :]            # mirror
    wm = None if white_mask is None else np.asarray(white_mask, dtype=np.uint8)[:, ::-1]
    mono = ribbon == "mono"

    if method == "simple":
        flags = 0x200 | (0 if mono else colormatch & 0xF)
        planes = _simple(rgb, ribbon, wm)
        return _finish(planes, flags, mono)

    from . import halftone                            # full pipeline (method 0 and 2)
    if method not in (0, 2):
        raise ValueError("method must be 0, 2 or 'simple'")
    flags = halftone.make_flags(ribbon, colormatch, alt, heat, median)
    planes = halftone.process(rgb, ribbon=ribbon, method=method, flags=flags, white_mask=wm)
    return _finish(planes, flags, mono)
