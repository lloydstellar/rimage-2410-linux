"""Inspect an Everest print job: list of blocks and an approximate colour preview.

Usage: python -m rimage2410.everest.inspect job.prn [preview.png]
The preview turns the heat values per plane into RGB (C -> 255-R and so on). It is a
check image, not an accurate rendering of the print.
"""

from __future__ import annotations

import sys

import numpy as np
from PIL import Image

from . import job

LINE = 1920
IMAGE = 1889


def planes_from_job(data: bytes) -> dict[int, np.ndarray]:
    planes = {}
    for b in job.parse_job(data):
        if b.get("kind") == "block" and b["code"] == 0x53:
            off = b["offset"] + job.BLOCK_LEN
            planes[b["plane"]] = np.frombuffer(data[off:off + b["length"]], np.uint8).reshape(-1, LINE)
    return planes


def preview(data: bytes) -> Image.Image:
    """Approximate colour preview. The lines in a job are mirrored; this flips them back."""
    p = planes_from_job(data)
    if 0 in p:
        k = p[0][:, :IMAGE][:, ::-1]
        return Image.fromarray(255 - k).convert("RGB")
    c, m, y = (p[i][:, :IMAGE][:, ::-1] for i in (1, 2, 3))
    return Image.fromarray(np.stack([255 - c, 255 - m, 255 - y], -1))


def describe(data: bytes) -> list[str]:
    lines = []
    for b in job.parse_job(data):
        if b["kind"] == "m2":
            lines.append(f"ESC!m2  {b['bytes'][4:8].hex(' ')}")
        elif b["code"] == 0x53:
            lines.append(f"ESC&S   plane {b['plane']}{' (last)' if b['last'] else ''}, {b['length']} bytes")
        else:
            lines.append(f"ESC&{b['name']:<4} {b['args'].hex(' ')}")
    return lines


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    data = open(argv[0], "rb").read()
    print("\n".join(describe(data)))
    if len(argv) > 1:
        preview(data).save(argv[1])
        print(f"preview saved: {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
