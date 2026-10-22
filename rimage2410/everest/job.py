"""Print jobs and status commands for the Rimage Everest 400.

A job as the printer receives it: first ESC!m2 (quality and media), then the header blocks
with media byte 0 in ESC&A, the colour planes (ESC&S) and the end block. Each block is 16
bytes: ESC '&' code, 12 argument bytes, 'P'. This module only builds bytes; sending happens
elsewhere.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

ESC = b"\x1b"
BLOCK_LEN = 16

RIBBONS = {
    # name: (ribbon type field, ESC&U code, plane numbers)
    "cmy": (0, 2, (1, 2, 3)),
    "cmyw": (1, 3, (1, 2, 3, 4)),
    "mono": (2, 0, (0,)),
}
QUALITY_CODES = {0, 1, 0x10, 0x11}   # ESC&M byte 3


class JobError(ValueError):
    pass


def block(code: int, args: bytes = b"") -> bytes:
    """One block: ESC '&' code, 12 argument bytes (padded with 0), 'P'."""
    if len(args) > 12:
        raise JobError("at most 12 argument bytes")
    return ESC + b"&" + bytes([code]) + args.ljust(12, b"\0") + b"P"


@dataclass(frozen=True)
class JobParams:
    width: int = 1889
    height: int = 1889
    media: str = "0"          # '0' = CD/DVD
    quality: int = 1          # ESC&M byte 3
    ribbon: str = "cmy"
    copies: int = 1
    media_in_header: bool = False   # True = media byte in ESC&A, False = media byte 0 (media goes in ESC!m2)

    def validate(self) -> None:
        if self.ribbon not in RIBBONS:
            raise JobError(f"unknown ribbon: {self.ribbon}")
        if self.quality not in QUALITY_CODES:
            raise JobError(f"unknown quality code: {self.quality}")
        if not (1 <= self.copies <= 999):
            raise JobError("copies must be 1..999")
        if len(self.media) != 1:
            raise JobError("media must be a single character")
        if not (1 <= self.width <= 0x762 and 1 <= self.height <= 0x780):
            raise JobError("size out of range")


def m2_command(params: JobParams) -> bytes:
    """ESC!m2 <quality> <ESC&M byte 4 = 0> <media character> 00 C."""
    return ESC + b"!m2" + bytes([params.quality, 0, ord(params.media), 0]) + b"C"


def header_blocks(params: JobParams) -> bytes:
    params.validate()
    ribbon_type, u_code, _planes = RIBBONS[params.ribbon]
    media_byte = ord(params.media) if params.media_in_header else 0
    out = [
        block(0x65),                                                         # ESC&e
        block(0x41, struct.pack("<HHB", params.height, params.width, media_byte)),  # ESC&A
        block(0x4D, bytes([params.quality, 0])),                             # ESC&M
        block(0x45),                                                         # ESC&E
        block(0x4C, struct.pack("<H", 0)),                                   # ESC&L
        block(0x55, bytes([u_code])),                                        # ESC&U
        block(0x52, struct.pack("<H", params.copies - 1)),                   # ESC&R
        block(0x46, bytes([0])),                                             # ESC&F
    ]
    return b"".join(out)


def plane_block(plane: int, data_len: int, last: bool) -> bytes:
    """ESC&S: byte 3 = plane, byte 4 = 0x80 on the last plane, bytes 5-8 = length."""
    return block(0x53, bytes([plane, 0x80 if last else 0]) + struct.pack("<I", data_len))


def end_block() -> bytes:
    return block(0x0C)


def build_job(planes: dict[int, np.ndarray], *, ribbon: str = "cmy", quality: int = 1,
              copies: int = 1, media: str = "0", width: int = 1889, height: int | None = None,
              media_in_header: bool = False, include_m2: bool = True, include_end: bool = True) -> bytes:
    """Full job: [ESC!m2] + header + ESC&S planes + [end block].

    Behind the autoloader (2410) the job is sent without ESC!m2 and without the end block
    (form feed); the autoloader starts the retransfer itself. With ESC!m2 and the end block
    it is the form for a standalone printer.
    """
    if ribbon not in RIBBONS:
        raise JobError(f"unknown ribbon: {ribbon}")
    expected = RIBBONS[ribbon][2]
    if tuple(sorted(planes)) != expected:
        raise JobError(f"ribbon {ribbon} expects planes {expected}, got {tuple(sorted(planes))}")
    shapes = {p: a.shape for p, a in planes.items()}
    if len(set(shapes.values())) != 1:
        raise JobError(f"planes have different sizes: {shapes}")
    for p, a in planes.items():
        if a.dtype != np.uint8 or a.ndim != 2:
            raise JobError(f"plane {p} must be a 2D uint8 array")
    rows = next(iter(planes.values())).shape[0]
    params = JobParams(width=width, height=height or min(rows, 0x780), media=media, quality=quality,
                       ribbon=ribbon, copies=copies, media_in_header=media_in_header)
    parts = [m2_command(params)] if include_m2 else []
    parts.append(header_blocks(params))
    order = list(expected)
    for i, p in enumerate(order):
        data = np.ascontiguousarray(planes[p]).tobytes()
        parts.append(plane_block(p, len(data), i == len(order) - 1))
        parts.append(data)
    if include_end:
        parts.append(end_block())
    return b"".join(parts)


def parse_job(data: bytes) -> list[dict]:
    """Split a job into commands and blocks (for tests and inspection)."""
    out: list[dict] = []
    i = 0
    while i < len(data):
        if data[i:i + 3] == ESC + b"!m":
            out.append({"kind": "m2", "bytes": data[i:i + 9]})
            i += 9
            continue
        blk = data[i:i + BLOCK_LEN]
        if len(blk) < BLOCK_LEN or blk[:2] != ESC + b"&" or blk[15:16] != b"P":
            raise JobError(f"no valid block at position {i}")
        code = blk[2]
        entry = {"kind": "block", "code": code, "name": chr(code) if 32 < code < 127 else f"0x{code:02X}",
                 "args": blk[3:15], "offset": i}
        i += BLOCK_LEN
        if code == 0x53:
            plane, flag = blk[3], blk[4]
            (length,) = struct.unpack("<I", blk[5:9])
            entry.update(plane=plane, last=bool(flag & 0x80), length=length)
            if i + length > len(data):
                raise JobError("plane data is shorter than announced")
            i += length
        out.append(entry)
    return out


# ---- status commands (read only) -------------------------------------------

STATUS_COMMANDS = {
    # name: (bytes, expected reply length)
    "r3": (ESC + b"!r3C", 13),    # status: state, ribbon, film, error code, switch
    "r4": (ESC + b"!r4C", 65),    # printer information (versions, serial number)
    "r6": (ESC + b"!r6C", 20),    # consumables: remaining prints, film
    "i6": (ESC + b"!i6C", 10),    # progress: printed and total copies
    # read EEPROM bank 2 (alignment): bank as a binary byte, reply is a 10-byte header + 200 bytes.
    # Read only; writing (ESC!g1) is never allowed.
    "g2": (ESC + b"!g2\x02\x00\x00\x00C", 210),
    "g2b3": (ESC + b"!g2\x03\x00\x00\x00C", 550),   # bank 3 (540 bytes): colour density
}


def status_command(name: str) -> bytes:
    """Only the read-only status commands r3, r4, r6, i6, g2 and g2b3; everything else is refused."""
    if name not in STATUS_COMMANDS:
        raise JobError(f"status command '{name}' not allowed (only {', '.join(STATUS_COMMANDS)})")
    return STATUS_COMMANDS[name][0]


STATE_TEXT = {
    "0": "Ready", "1": "Warming up", "2": "Printing", "3": "Printing", "4": "Waiting for the disc to be removed",
    "5": "Initialising", "6": "Printing", "7": "Printing", "8": "Waiting for the disc to be removed",
    "9": "Printing", "A": "Printing", "B": "Printing", "C": "Printing", "D": "Printing", "E": "Error",
}
RIBBON_TEXT = {**{c: "CMY (500)" for c in "0369"}, **{c: "CMY + white (325)" for c in "147A"},
               **{c: "Black (1000)" for c in "258B"}, "E": "No ribbon"}
FILM_TEXT = {"0": "Retransfer film present", "1": "Retransfer film present", "E": "No retransfer film",
             "F": "Retransfer film unknown"}


@dataclass(frozen=True)
class PrinterStatus:
    raw: str
    state: str
    ribbon: str
    film: str
    error: str
    switch: str

    @property
    def text(self) -> str:
        parts = [STATE_TEXT.get(self.state, f"state {self.state}"),
                 RIBBON_TEXT.get(self.ribbon, f"ribbon {self.ribbon}"),
                 FILM_TEXT.get(self.film, f"film {self.film}")]
        if self.state == "E":
            parts.append(f"error code {self.error}")
        return ", ".join(parts)


def parse_r3(resp: bytes) -> PrinterStatus:
    """Reply to ESC!r3C: ESC!r3 + '7' + 'C' + 7 bytes (d0..d6)."""
    if resp[:4] != ESC + b"!r3" or resp[4:6] != b"7C" or len(resp) < 13:
        raise JobError(f"unexpected r3 reply: {resp!r}")
    d = resp[6:13].decode("ascii", "replace")
    return PrinterStatus(raw=d, state=d[1], ribbon=d[2], film=d[3], error=d[4:6], switch=d[6])


# ---- ribbon and consumables ----------------------------------------------------

RIBBON_FROM_D2 = {**{c: "cmy" for c in "0369"}, **{c: "cmyw" for c in "147A"},
                  **{c: "mono" for c in "258B"}}
RIBBON_CAPACITY = {"cmy": 500, "cmyw": 325, "mono": 1000}
RIBBON_NAMES = {"cmy": "colour (CMY)", "cmyw": "colour with white (CMY + W)", "mono": "black"}


def ribbon_from_status(st: "PrinterStatus") -> str | None:
    """Ribbon type from ESC!r3C (d2): 'cmy', 'cmyw', 'mono', or None for no ribbon or an unknown one."""
    return RIBBON_FROM_D2.get(st.ribbon)


@dataclass(frozen=True)
class Consumables:
    counter_a: int | None          # colour planes printed, 3 per CMY print (same as autoloader counter !f;6C)
    counter_b: int | None          # prints made (same as !f;5C)
    film_percent: int | None       # remaining retransfer film in percent
    ribbon_remaining: int | None   # remaining prints on the ribbon (FFFF = unknown, for example an older ribbon)
    film_value: int | None         # remaining prints on the retransfer film (for example 491 at 98%; same as !f;8C)

    def ribbon_percent(self, ribbon: str | None) -> int | None:
        cap = RIBBON_CAPACITY.get(ribbon or "")
        if cap is None or self.ribbon_remaining is None:
            return None
        return min(100, round(self.ribbon_remaining * 100 / cap))


def parse_r6(resp: bytes) -> Consumables:
    """Reply to ESC!r6C: ESC!r6 + '13' + 'C' + 13 bytes.

    Little-endian. The values match the autoloader counters !f;6C, !f;5C and !f;8C. The last
    value is the number of prints left on the retransfer film (491 at 98%, as the 2410 display
    shows it).
    """
    if resp[:4] != ESC + b"!r6" or resp[4:7] != b"13C" or len(resp) < 20:
        raise JobError(f"unexpected r6 reply: {resp!r}")
    d = resp[7:20]
    a, b = struct.unpack("<II", d[0:8])
    film = d[8]
    rib, film2 = struct.unpack("<HH", d[9:13])
    return Consumables(counter_a=a, counter_b=b, film_percent=None if film == 0xFF else film,
                       ribbon_remaining=None if rib == 0xFFFF else rib,
                       film_value=None if film2 == 0xFFFF else film2)


@dataclass
class Alignment:
    """Print alignment stored in EEPROM bank 2 of the printer.

    The unit is probably steps of about 0.1 mm. Displayed values:
    horizontal = h_base + h_adjust, vertical colour = v_color_raw / 2, vertical black = v_mono_raw / 2.
    """
    horizontal: int
    vertical_color: float
    vertical_mono: float
    h_base: int
    h_adjust: int
    v_color_ref: int
    v_mono_ref: int
    raw: bytes


def parse_g2(resp: bytes) -> Alignment:
    if len(resp) < 10 + 0x20 or resp[2:5] != b"g21" or resp[5] != 2:
        raise JobError(f"unexpected reply to ESC!g2: {resp[:12]!r}")
    n = struct.unpack_from("<H", resp, 7)[0]
    data = resp[10:10 + n]
    s16 = lambda o: struct.unpack_from("<h", data, o)[0]  # noqa: E731
    return Alignment(horizontal=s16(0) + s16(6), vertical_color=s16(0x0E) / 2, vertical_mono=s16(0x16) / 2,
                     h_base=s16(0), h_adjust=s16(6), v_color_ref=s16(0x08), v_mono_ref=s16(0x10), raw=bytes(data))
