#!/usr/bin/env python3
"""Ribbon patch for Rimage autoloader firmware 6.033A.

The autoloader of the Rimage Professional 2410, 3410 and 5410N reads data
from the ribbon in the Everest 400 and refuses to pick a disc when that data
falls outside a fixed range (error 33, "Installed ribbon is not authorized
for this system"). This script widens that range in the firmware file
INTEGRA-XX10-6033.bin, so the machine accepts any ribbon the Everest 400 can
print with. It changes 5 bytes and updates the SHA-1 checksum stored in the
file. Nothing else changes.

Only the exact original file from Rimage is accepted, checked by SHA-256. The
result is checked byte by byte and must match the known patched file.

Usage:
    python3 ribbon_patch.py INTEGRA-XX10-6033.bin
    python3 ribbon_patch.py INTEGRA-XX10-6033.bin -o patched.bin
    python3 ribbon_patch.py --check SOME-FILE.bin

No dependencies besides Python 3.8 or later.
"""

import argparse
import hashlib
import os
import sys
import tempfile

SIZE = 64000

ORIGINAL_SHA256 = "e5d558a6375c462c523204d5506d5dc7dcbbb630268b0949269df94bb4024d67"
PATCHED_SHA256 = "4bf97b837654c0b1545052f29f88c7a810ee0da5024465a1d5bbd24adb14a2dd"

VERSION_OFFSET = 0x6FCA
VERSION = b"RIMAGE 5410     VERSION 6.033A"
BOOT_OFFSET = 0x0100
BOOT = b"RIMAGE 5410     BOOT    6.000A"

# The firmware stores a SHA-1 of the application area right behind it.
SHA1_START, SHA1_END = 0x3C00, 0xF7EC
SHA1_OFFSET = SHA1_END
ORIGINAL_SHA1 = "0cdfd0654bb37976e3dc667a49f86a5ec5f90dfe"
PATCHED_SHA1 = "0f7f35d82a2702396cb0e7368340d3638a4eb23e"

# Flashing tools only look at this part of the file; it must stay the same.
HEADER_END = 0x4000

# (offset, original byte, patched byte). These are the immediate operands of
# five MOV instructions in the ribbon check: the upper limit 0x02100000
# becomes 0xFFFF0000 and the lower limit 0x01B80040 becomes 0.
PATCH = [
    (0xB6D3, 0x10, 0xFF),
    (0xB6D5, 0x02, 0xFF),
    (0xB6DD, 0x40, 0x00),
    (0xB6E1, 0xB8, 0x00),
    (0xB6E3, 0x01, 0x00),
]

# The opcode in front of each operand (MOV R1/R0/R7/R5/R4, #imm).
OPCODES = {0xB6D2: 0x79, 0xB6D4: 0x78, 0xB6DC: 0x7F, 0xB6E0: 0x7D, 0xB6E2: 0x7C}


class PatchError(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def stored_sha1(data):
    return data[SHA1_OFFSET:SHA1_OFFSET + 20].hex()


def computed_sha1(data):
    return hashlib.sha1(data[SHA1_START:SHA1_END]).hexdigest()


def identify(data):
    """Return 'original', 'patched' or 'unknown'."""
    digest = sha256(data)
    if digest == ORIGINAL_SHA256:
        return "original"
    if digest == PATCHED_SHA256:
        return "patched"
    return "unknown"


def explain_unknown(data):
    """Why a file is not the supported firmware, as a list of lines."""
    lines = []
    if len(data) != SIZE:
        lines.append("Size is %d bytes, expected %d." % (len(data), SIZE))
        if len(data) < SHA1_OFFSET + 20:
            lines.append("The file is too short to be autoloader firmware. "
                         "Is the download complete?")
            return lines
    if data[BOOT_OFFSET:BOOT_OFFSET + len(BOOT)] != BOOT:
        lines.append("This does not look like firmware for the 2410, 3410 or 5410N "
                     "(no '%s' at 0x%04X)." % (BOOT.decode(), BOOT_OFFSET))
        return lines
    version = data[VERSION_OFFSET:VERSION_OFFSET + len(VERSION)]
    if version != VERSION:
        found = list(_version_strings(data))
        lines.append("This is not version 6.033A%s. The patch only fits 6.033A."
                     % (" (found: %s)" % ", ".join(found) if found else ""))
        return lines
    if stored_sha1(data) != computed_sha1(data):
        lines.append("Version 6.033A, but the built-in SHA-1 checksum does not match. "
                     "The file is damaged or modified. Download it again.")
    else:
        lines.append("Version 6.033A with a valid checksum, but not the file this "
                     "patch was made for. It has been modified by someone else.")
    return lines


def _version_strings(data):
    marker = b" VERSION "
    pos = data.find(marker)
    while pos != -1:
        start = data.rfind(b"RIMAGE", max(0, pos - 20), pos)
        if start != -1:
            yield data[start:pos + len(marker) + 6].decode("ascii", "replace")
        pos = data.find(marker, pos + 1)


def check_original(data):
    """Raise PatchError unless data is the supported original firmware."""
    kind = identify(data)
    if kind == "patched":
        raise PatchError("This file is already patched.")
    if kind != "original":
        raise PatchError("\n".join(["This is not the original INTEGRA-XX10-6033.bin."]
                                   + explain_unknown(data)))
    # The hash already proves all of this. Checked anyway, so that a mistake
    # in the constants above can never produce a bad file.
    if len(data) != SIZE:
        raise PatchError("Unexpected size.")
    if data[VERSION_OFFSET:VERSION_OFFSET + len(VERSION)] != VERSION:
        raise PatchError("Version string not found.")
    if stored_sha1(data) != computed_sha1(data) or stored_sha1(data) != ORIGINAL_SHA1:
        raise PatchError("Built-in SHA-1 checksum of the original does not match.")
    for offset, opcode in OPCODES.items():
        if data[offset] != opcode:
            raise PatchError("Unexpected instruction at 0x%04X." % offset)
    for offset, old, _new in PATCH:
        if data[offset] != old:
            raise PatchError("Unexpected byte 0x%02X at 0x%04X, expected 0x%02X."
                             % (data[offset], offset, old))


def apply_patch(data):
    """Return the patched firmware. data must be the original."""
    check_original(data)
    out = bytearray(data)
    for offset, _old, new in PATCH:
        out[offset] = new
    out[SHA1_OFFSET:SHA1_OFFSET + 20] = hashlib.sha1(out[SHA1_START:SHA1_END]).digest()
    out = bytes(out)
    verify_patched(data, out)
    return out


def verify_patched(original, patched):
    """Raise PatchError unless patched is exactly original plus the patch."""
    if len(patched) != SIZE:
        raise PatchError("Patched file has the wrong size.")
    changed = {i for i in range(SIZE) if original[i] != patched[i]}
    allowed = {offset for offset, _old, _new in PATCH} | set(range(SHA1_OFFSET, SHA1_OFFSET + 20))
    if changed - allowed:
        raise PatchError("Bytes outside the patch changed: %s"
                         % ", ".join("0x%04X" % i for i in sorted(changed - allowed)[:10]))
    for offset, _old, new in PATCH:
        if patched[offset] != new:
            raise PatchError("Patch byte at 0x%04X was not written." % offset)
    if patched[:HEADER_END] != original[:HEADER_END]:
        raise PatchError("File header changed.")
    if patched[VERSION_OFFSET:VERSION_OFFSET + len(VERSION)] != VERSION:
        raise PatchError("Version string changed.")
    if stored_sha1(patched) != computed_sha1(patched):
        raise PatchError("Built-in SHA-1 checksum of the patched file is wrong.")
    if stored_sha1(patched) != PATCHED_SHA1:
        raise PatchError("Built-in SHA-1 checksum differs from the expected value.")
    if sha256(patched) != PATCHED_SHA256:
        raise PatchError("SHA-256 of the patched file differs from the expected value.")


def write_checked(path, original, patched, overwrite):
    """Write through a temporary file, read it back and verify before keeping it."""
    if os.path.exists(path) and not overwrite:
        raise PatchError("%s already exists. Use --force to replace it." % path)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".ribbon-patch-", suffix=".bin", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(patched)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, "rb") as f:
            verify_patched(original, f.read())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read(path):
    try:
        if os.path.getsize(path) > 1 << 20:
            raise PatchError("%s is larger than 1 MB. This is not autoloader firmware." % path)
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise PatchError("Cannot read %s: %s" % (path, e.strerror or e))


def default_output(path):
    directory, name = os.path.split(path)
    if "6033" in name and "6033b" not in name:
        name = name.replace("6033", "6033b", 1)
    else:
        stem, ext = os.path.splitext(name)
        name = stem + "-patched" + (ext or ".bin")
    return os.path.join(directory, name)


def cmd_check(path):
    data = read(path)
    kind = identify(data)
    print("%s\n  SHA-256 %s" % (path, sha256(data)))
    if kind == "original":
        print("  Original Rimage firmware 6.033A. This file can be patched.")
        return 0
    if kind == "patched":
        print("  Patched firmware 6.033A (ribbon patch). Checksum is valid.")
        return 0
    for line in explain_unknown(data):
        print("  " + line)
    return 1


def cmd_patch(path, output, overwrite):
    data = read(path)
    if identify(data) == "patched":
        print("%s is already patched. Nothing to do." % path)
        return 0
    output = output or default_output(path)
    if os.path.abspath(output) == os.path.abspath(path):
        raise PatchError("The output must be a different file. Keep the original.")
    patched = apply_patch(data)
    write_checked(output, data, patched, overwrite)
    print("Input   %s\n        original 6.033A, SHA-256 %s" % (path, ORIGINAL_SHA256))
    print("Patched %d bytes and the built-in SHA-1 checksum." % len(PATCH))
    print("Output  %s\n        SHA-256 %s (verified)" % (output, PATCHED_SHA256))
    print("Keep the original file. You need it to go back.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Ribbon patch for Rimage autoloader firmware 6.033A "
                    "(INTEGRA-XX10-6033.bin).")
    parser.add_argument("firmware", help="INTEGRA-XX10-6033.bin from Rimage")
    parser.add_argument("-o", "--output", help="where to write the patched file "
                        "(default: INTEGRA-XX10-6033b.bin next to the input)")
    parser.add_argument("--check", action="store_true",
                        help="only tell whether the file is the original, "
                             "the patched version or something else")
    parser.add_argument("--force", action="store_true",
                        help="replace the output file if it exists")
    args = parser.parse_args(argv)
    try:
        if args.check:
            return cmd_check(args.firmware)
        return cmd_patch(args.firmware, args.output, args.force)
    except PatchError as e:
        print("Error: %s" % e, file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
