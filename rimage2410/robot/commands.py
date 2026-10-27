"""Commands for the autoloader of the Rimage 2410 (firmware 6.033).

Every command is ASCII: ESC '!' <group> <sub> [parameters] 'C'. Responses
echo the first four bytes. This module only builds and parses bytes; it never
talks to a device itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, IntEnum

ESC = b"\x1b"


class Element(IntEnum):
    """Element numbers used by the autoloader protocol."""

    BIN_1 = 200
    BIN_2 = 201
    BIN_3 = 202
    BIN_4 = 203
    GRIPPER = 220
    DRIVE_ANY = 240
    DRIVE_1 = 241
    DRIVE_2 = 242
    DRIVE_3 = 243
    DRIVE_4 = 244
    PRINTER = 260
    RIBBON = 270
    ROTATOR = 280
    MAILSLOT = 300


class Action(IntEnum):
    """Actions for Position Medium."""

    MOVE = 0          # only move there
    PLACE = 1         # place a disc
    PICK = 2          # pick a disc
    FIND = 3
    RETRANSFER = 4    # printer: transfer the label from the film to the disc
    ADVANCE = 5
    RESET = 6         # printer: soft reset
    ASYNC_RETRANSFER = 7
    HARD_RESET = 8    # printer: hard reset
    PLACE_NO_CLAMP = 9


class Safety(str, Enum):
    READ = "read"         # query only
    CONFIG = "config"     # sets a state in RAM (for example standby)
    MOTION = "motion"     # moves the mechanics
    BLOCKED = "blocked"   # never send (persistent writes, firmware, calibration)


@dataclass(frozen=True)
class Command:
    name: str          # Rimage name or our own description
    data: bytes        # complete bytes, including ESC
    expect: int        # expected response length in bytes
    safety: Safety
    timeout_s: float = 10.0

    @property
    def text(self) -> str:
        return self.data.replace(ESC, b"<ESC>").decode("ascii")


class BlockedCommand(ValueError):
    """A command on the block list."""


# Groups and subcommands that must never be sent.
_BLOCKED_PREFIXES = (
    b"!f6", b"!f7",            # calibration offsets
    b"!f9",                    # persistent writes (serial numbers, parameters, display name)
    b"!g",                     # bootloader, firmware update, saving parameters
    b"!h",                     # reset and firmware
    b"!k5", b"!k6", b"!k8", b"!kb",   # persistent writes (PPC, VKEY, model)
    b"!b",                     # baud rate (firmware update on serial models)
    b"!z0",                    # writes 32 bytes straight into an EEPROM (I2C 0xA0/0xA2)
)


def check_allowed(data: bytes) -> None:
    """Raise BlockedCommand if the bytes are a blocked command."""
    if not data.startswith(ESC + b"!"):
        raise BlockedCommand(f"not an ESC! command: {data!r}")
    body = data[1:]
    for prefix in _BLOCKED_PREFIXES:
        if body.startswith(prefix):
            raise BlockedCommand(f"blocked command: {data!r}")


def _cmd(name: str, ascii_body: str, expect: int, safety: Safety, timeout_s: float = 10.0) -> Command:
    data = ESC + ascii_body.encode("ascii")
    check_allowed(data)
    return Command(name, data, expect, safety, timeout_s)


# ---- queries --------------------------------------------------------------

def inquiry(first: bool = True) -> Command:
    """Inquiry (!e0). Read 39 bytes first; the length field gives the rest."""
    return _cmd("Inquiry", "!e0C", 39 if first else 103, Safety.READ)


def request_sense() -> Command:
    """Request Sense (!e1): changer sense and printer sense, 3 digits each."""
    return _cmd("Request Sense", "!e1C", 11, Safety.READ)


def version_bytes() -> Command:
    """!e7: two hex bytes (version or hardware revision)."""
    return _cmd("Version", "!e7C", 12, Safety.READ)


def read_element_status(element: int) -> Command:
    """Read Element Status (!f0): occupied/empty plus a 5-digit value."""
    return _cmd("Read Element Status", f"!f0{int(element):03d}C", 12, Safety.READ)


def get_string(which: int) -> Command:
    """Get String (!f:): 000 loader serial number, 260 printer, 270 ribbon, 099 parameters."""
    if which not in (0, 99, 260, 270):
        raise ValueError("Get String accepts 000, 099, 260 and 270")
    expect = 16 if which == 270 else 14
    return _cmd("Get String", f"!f:{which:03d}C", expect, Safety.READ)


def get_counter(index: str) -> Command:
    """Get Counter (!f;): one ASCII character as the counter number."""
    if len(index) != 1 or not index.isprintable():
        raise ValueError("the counter number is one character")
    # response: ESC!f; + number + '1'/'0' + 5 or 10 digits + C
    expect = 12 if index in "0123" else 17
    return _cmd("Get Counter", f"!f;{index}C", expect, Safety.READ)


def everest_temperature() -> Command:
    """Get Everest Temperature (!f<0): three numbers, the second is the heat roller."""
    return _cmd("Get Everest Temperature", "!f<0C", 28, Safety.READ)


# ---- printer standby ----------------------------------------------------

def fan_control(on: bool) -> Command:
    """Fan Control (!e2): '2' = printer awake, '0' = standby. No motion."""
    return _cmd("Fan Control", f"!e2{'2' if on else '0'}C", 6, Safety.CONFIG)


# ---- motion ----------------------------------------------------------------

def position_medium(element: int, action: int, timeout_s: float = 60.0) -> Command:
    """Position Medium (!f1): arm to an element, with an action (move, place, pick...)."""
    if not 0 <= int(action) <= 9:
        raise ValueError("action 0..9")
    return _cmd("Position Medium", f"!f1{int(element):03d}{int(action)}C", 6, Safety.MOTION, timeout_s)


def open_close_element(element: int, open_: bool, timeout_s: float = 60.0) -> Command:
    """Open/Close Element (!f2): on the 2410 only the printer tray (260)."""
    if int(element) != Element.PRINTER:
        raise ValueError("firmware 6.033 accepts !f2 only for element 260")
    return _cmd("Open/Close Element", f"!f2{int(element):03d}{1 if open_ else 0}C", 6, Safety.MOTION, timeout_s)


def home() -> Command:
    """Move the arm home; it stops above the printer (!f12600C)."""
    return position_medium(Element.PRINTER, Action.MOVE)


def start_retransfer(asynchronous: bool = False) -> Command:
    """Transfer the label from the retransfer film to the disc (!f12604C or !f12607C)."""
    act = Action.ASYNC_RETRANSFER if asynchronous else Action.RETRANSFER
    return position_medium(Element.PRINTER, act, 500.0 if asynchronous else 250.0)


def printer_reset(hard: bool = False) -> Command:
    """Soft (!f12606C) or hard (!f12608C) reset of the Everest through the autoloader."""
    return position_medium(Element.PRINTER, Action.HARD_RESET if hard else Action.RESET, 200.0)


# ---- parsing responses ----------------------------------------------------

class ResponseError(ValueError):
    pass


def _check_echo(cmd: Command, resp: bytes) -> None:
    if len(resp) < 4 or resp[:4] != cmd.data[:4]:
        raise ResponseError(f"response does not echo the header: {resp!r} for {cmd.text}")


def parse_simple(cmd: Command, resp: bytes) -> bool:
    """6-byte response ESC ! g s '1'|'0' C. True for '1'."""
    _check_echo(cmd, resp)
    if len(resp) < 6:
        raise ResponseError(f"response too short: {resp!r}")
    return resp[4:5] == b"1"


@dataclass(frozen=True)
class Sense:
    changer: int
    printer: int


def parse_sense(cmd: Command, resp: bytes) -> Sense:
    """ESC!e1 + 3 digits changer + 3 digits printer (+ C)."""
    _check_echo(cmd, resp)
    m = re.match(rb"(\d{3})(\d{3})", resp[4:10])
    if not m:
        raise ResponseError(f"unexpected sense response: {resp!r}")
    return Sense(int(m.group(1)), int(m.group(2)))


@dataclass(frozen=True)
class ElementStatus:
    ok: bool
    occupied: bool
    value: int


def parse_element_status(cmd: Command, resp: bytes) -> ElementStatus:
    """ESC!f0 + '1' + state (1 digit) + value (5 digits) + C."""
    _check_echo(cmd, resp)
    m = re.match(rb"([01])(\d)(\d{5})", resp[4:11])
    if not m:
        raise ResponseError(f"unexpected element response: {resp!r}")
    return ElementStatus(m.group(1) == b"1", m.group(2) != b"0", int(m.group(3)))


@dataclass(frozen=True)
class Inquiry:
    length: int
    text: str


def parse_inquiry(cmd: Command, resp: bytes) -> Inquiry:
    """ESC!e0 + length (digits) + C + text."""
    _check_echo(cmd, resp)
    m = re.match(rb"(\d+)C", resp[4:])
    if not m:
        raise ResponseError(f"unexpected Inquiry response: {resp!r}")
    length = int(m.group(1))
    start = 4 + m.end()
    return Inquiry(length, resp[start:start + length].decode("ascii", "replace"))


def parse_string(cmd: Command, resp: bytes) -> str:
    """ESC!f: + '1' + C + text."""
    _check_echo(cmd, resp)
    if resp[4:6] != b"1C":
        raise ResponseError(f"Get String failed: {resp!r}")
    return resp[6:].rstrip(b"\x00").decode("ascii", "replace").strip()


def parse_temperature(cmd: Command, resp: bytes) -> list[int]:
    """Get Everest Temperature: ESC!f< + status (byte 4) + marker (byte 5, '3')
    + three numbers. Returns the three numbers; the second is the heat roller."""
    _check_echo(cmd, resp)
    return [int(x) for x in re.findall(rb"[+-]?\d+", resp[6:])]
