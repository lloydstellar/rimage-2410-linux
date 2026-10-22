"""Small PSD (Photoshop) writer for the label template.

Writes an 8-bit RGB document with layers (RGBA, RLE compressed), resolution, an embedded
ICC profile and guides. It covers only what the template needs, not the full PSD format.

Format: Adobe Photoshop File Formats Specification (header, color mode data,
image resources 1005/1032/1039, layer and mask information, image data).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np
from PIL import Image


@dataclass
class Layer:
    name: str
    image: Image.Image          # RGBA, same size as the document
    visible: bool = True
    opacity: int = 255


def _packbits_row(row: np.ndarray) -> bytes:
    """PackBits (Macintosh RLE) for one row of bytes."""
    n = row.size
    if n == 0:
        return b""
    # runs: start of each sequence of equal values
    change = np.flatnonzero(np.diff(row)) + 1
    starts = np.concatenate(([0], change))
    lengths = np.diff(np.concatenate((starts, [n])))
    out = bytearray()
    literal = bytearray()

    def flush_literal():
        i = 0
        while i < len(literal):
            chunk = literal[i:i + 128]
            out.append(len(chunk) - 1)
            out.extend(chunk)
            i += 128
        literal.clear()

    for s, length in zip(starts.tolist(), lengths.tolist()):
        v = int(row[s])
        if length >= 3:
            flush_literal()
            while length > 0:
                k = min(length, 128)
                if k >= 2:
                    out.append(257 - k)
                    out.append(v)
                else:
                    out.append(0)
                    out.append(v)
                length -= k
        else:
            literal.extend([v] * length)
    flush_literal()
    return bytes(out)


def _rle_channel(ch: np.ndarray) -> tuple[list[int], bytes]:
    counts, data = [], bytearray()
    for row in ch:
        enc = _packbits_row(row)
        counts.append(len(enc))
        data.extend(enc)
    return counts, bytes(data)


def _pascal(name: str, pad: int) -> bytes:
    raw = name.encode("mac_roman", "replace")[:255]
    b = bytes([len(raw)]) + raw
    return b + b"\0" * ((-len(b)) % pad)


def _resource(rid: int, data: bytes) -> bytes:
    body = data + (b"\0" if len(data) % 2 else b"")
    return b"8BIM" + struct.pack(">H", rid) + b"\0\0" + struct.pack(">I", len(data)) + body


def _luni(name: str) -> bytes:
    """Unicode layer name (additional layer info 'luni')."""
    u = name.encode("utf-16-be")
    data = struct.pack(">I", len(name)) + u
    data += b"\0" * ((-len(data)) % 4)
    return b"8BIM" + b"luni" + struct.pack(">I", len(data)) + data


def write_psd(path: str, layers: list[Layer], *, dpi: float = 400.0, icc: bytes | None = None,
              guides: list[tuple[float, str]] | None = None) -> None:
    """Write a PSD. layers: bottom to top. guides: (position in pixels, 'h' or 'v')."""
    w, h = layers[0].image.size
    for lay in layers:
        if lay.image.size != (w, h) or lay.image.mode != "RGBA":
            raise ValueError(f"layer {lay.name}: must be RGBA of {w} x {h}")

    header = b"8BPS" + struct.pack(">H6sHIIHH", 1, b"\0" * 6, 3, h, w, 8, 3)
    color_mode = struct.pack(">I", 0)

    res = bytearray()
    fixed = int(round(dpi * 65536))
    res += _resource(1005, struct.pack(">IHHIHH", fixed, 1, 1, fixed, 1, 1))
    if icc:
        res += _resource(1039, icc)
    if guides:
        g = struct.pack(">IIII", 1, 576, 576, len(guides))
        for pos, direction in guides:
            g += struct.pack(">IB", int(round(pos * 32)), 0 if direction == "v" else 1)
        res += _resource(1032, g)
    resources = struct.pack(">I", len(res)) + bytes(res)

    # layers
    records = bytearray()
    channel_data = bytearray()
    for lay in layers:
        arr = np.asarray(lay.image, dtype=np.uint8)
        chans = [(-1, arr[:, :, 3]), (0, arr[:, :, 0]), (1, arr[:, :, 1]), (2, arr[:, :, 2])]
        rec = struct.pack(">iiiiH", 0, 0, h, w, len(chans))
        blobs = []
        for cid, ch in chans:
            counts, data = _rle_channel(ch)
            blob = struct.pack(">H", 1) + struct.pack(f">{len(counts)}H", *counts) + data
            blobs.append(blob)
            rec += struct.pack(">hI", cid, len(blob))
        flags = 0x08 | (0x00 if lay.visible else 0x02)   # bit 3: pixel data relevant; bit 1: hidden
        rec += b"8BIMnorm" + struct.pack(">BBBB", lay.opacity, 0, flags, 0)
        extra = struct.pack(">I", 0) + struct.pack(">I", 0) + _pascal(lay.name, 4) + _luni(lay.name)
        rec += struct.pack(">I", len(extra)) + extra
        records += rec
        for blob in blobs:
            channel_data += blob
    layer_info = struct.pack(">h", len(layers)) + bytes(records) + bytes(channel_data)
    if len(layer_info) % 2:
        layer_info += b"\0"
    lmi = struct.pack(">I", len(layer_info)) + layer_info + struct.pack(">I", 0)
    layer_section = struct.pack(">I", len(lmi)) + lmi

    # merged image (visible layers on white)
    comp = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    for lay in layers:
        if lay.visible:
            img = lay.image
            if lay.opacity != 255:
                a = np.asarray(img).copy()
                a[:, :, 3] = (a[:, :, 3].astype(np.uint16) * lay.opacity // 255).astype(np.uint8)
                img = Image.fromarray(a, "RGBA")
            comp = Image.alpha_composite(comp, img)
    carr = np.asarray(comp.convert("RGB"), dtype=np.uint8)
    all_counts, all_data = [], bytearray()
    for c in range(3):
        counts, data = _rle_channel(carr[:, :, c])
        all_counts += counts
        all_data += data
    image_data = struct.pack(">H", 1) + struct.pack(f">{len(all_counts)}H", *all_counts) + bytes(all_data)

    with open(path, "wb") as f:
        f.write(header + color_mode + resources + layer_section + image_data)
