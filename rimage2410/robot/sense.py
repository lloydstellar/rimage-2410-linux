"""Meaning of the sense codes from Request Sense (!e1) and how we respond to them.

Changer sense: the autoloader's own error codes.
Printer sense: the Everest status codes as passed on by the autoloader, grouped by how
recovery works. The printer sense arrives in decimal: 21 = 0x15 = ribbon out.
"""

CHANGER_SENSE = {
    0: "No error",
    1: "Recovered after retry",
    2: "Not ready",
    3: "No disc, or the gripper missed",
    4: "Mechanical error, the disc may have dropped",
    5: "Invalid command or parameter",
    6: "Door open (or was opened)",
    7: "Element not found (no recorder tray)",
    8: "No carousel found",
    9: "No connection to the printer",
    10: "Carousel not in position",
    11: "Lift not in position",
    12: "Recorder detection sensor faulty",
    13: "Retransfer failed",
    14: "Printer is busy and blocks the arm",
    15: "Communication error with the computer",
    16: "Flash error",
    17: "Rotation error",
    18: "Mailslot error",
    19: "Perfect Print error",
    20: "Disc pushed into slot 1",
    21: "Disc in slot not detected",
    22: "Output bin or slot full",
    25: "Drop bin open",
    26: "Gripper does not release the disc",
    27: "Disc is not clamped in the printer",
    30: "Invalid firmware",
    31: "Calibration required",
    32: "Firmware not valid for this product",
    33: "Installed ribbon is not authorized for this system",
    34: "Old printer cannot be used in this system",
}
for _c in range(40, 48):
    CHANGER_SENSE[_c] = f"Pay-Per-Click error ({_c})"

PRINTER_SENSE = {
    0x00: "Ready",
    0x01: "Warming up (heat roller)",
    0x02: "Printing",
    0x03: "Printing",
    0x04: "Disc in printer, printed",
    0x05: "Initializing",
    0x06: "Printing",
    0x07: "Busy",
    0x08: "Busy",
    0x09: "Label on film, waiting for disc",
    0x0A: "Set the printer switch to 1",
    0x0C: "Not initialized",
    0x14: "Error: press the printer button",
    0x15: "Ribbon out",
    0x16: "Invalid ribbon",
    0x17: "Retransfer film out",
    0x18: "Cover open",
    0x19: "No ribbon",
    0x1A: "No retransfer film",
    0x1B: "#1 not locked",
    0x1C: "#2 not locked",
    0x1D: "Print head down error",
    0x1F: "Print head up error",
    0x20: "Tray close error",
    0x21: "Tray open error",
    0x22: "Clamp error",
    0x23: "Data error",
    0x24: "#3 not locked",
    0x25: "Open error 37",
    0x26: "Retransfer up sensor",
    0x27: "Ink ribbon sensor",
    0x29: "Halogen lamp faulty",
    0x2A: "Retransfer down sensor",
    0x2B: "Heat roller faulty",
    0x2C: "Tray encoder",
    0x2D: "Retransfer motor faulty",
    0x2E: "Print head thermistor",
    0x2F: "Retransfer thermistor",
    0x30: "Thermistor 48",
    0x31: "EEPROM error",
    0x33: "Retransfer stopped",
    0x34: "Heat roller motion",
    0x35: "Heat roller fan",
    0x36: "Heat roller power",
    0xAA: "Offline",
    0xAC: "Printer busy (RTS)",
    0xBC: "Printer in firmware update mode",
    250: "Label lost between printing and retransfer",
    251: "Retransfer takes too long",
    252: "Printer became ready while printing (label lost)",
    254: "Communication error with the printer",
    255: "Printer does not respond",
}

PRINTER_BUSY = {0x02, 0x03, 0x06, 0x07, 0x08}
PRINTER_WAITING_FOR_DISC = 0x09
PRINTER_WARMING = 0x01
PRINTER_INITIALIZING = 0x05
PRINTER_DISC_PRESENT = 0x04
PRINTER_SWITCH = 0x0A

# Grouping used for recovery
PRINTER_BUTTON = {20}                                       # button on the printer
PRINTER_RIBBON_OUT = 21
PRINTER_FILM_OUT = 23
PRINTER_SUPPLY = {21, 22, 23, 26}                           # ribbon or film out, invalid or missing
PRINTER_WAIT_REINIT = {24, 27, 28, 36}                      # cover open, not locked: wait
PRINTER_AUTO_RESET = {25, 29, 31, 32, 33, 38, 39, *range(41, 49), 254}   # reset automatically
PRINTER_FIRMWARE_MODE = 188                                 # never continue: the printer expects firmware
PRINTER_CLAMP = 34

CHANGER_DOOR = {6, 25}
CHANGER_NOT_READY = 2
CHANGER_NO_DISC = 3
CHANGER_PRINTER_FULL = 14
CHANGER_BIN_FULL = 22
CHANGER_CLAMP = 27


def changer_text(code: int) -> str:
    return CHANGER_SENSE.get(code, f"Unknown loader code {code}")


def printer_text(code: int) -> str:
    return PRINTER_SENSE.get(code, f"Unknown printer code {code}")


def printer_is_error(code: int) -> bool:
    """Values from 20 (0x14) upwards are errors."""
    return code >= 0x14 and code not in (0xAC,)


def changer_needs_door(code: int) -> bool:
    return code in CHANGER_DOOR


def printer_recovery(code: int) -> str:
    """How to recover from a printer error: 'button', 'supply', 'reinit', 'reset', 'firmware' or 'power'."""
    if code == PRINTER_FIRMWARE_MODE:
        return "firmware"
    if code in PRINTER_BUTTON:
        return "button"
    if code in PRINTER_SUPPLY:
        return "supply"
    if code in PRINTER_WAIT_REINIT:
        return "reinit"
    if code in PRINTER_AUTO_RESET:
        return "reset"
    return "power"
