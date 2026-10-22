"""Tool for test sessions: inspect commands and responses of the autoloader.

Only the "send" subcommand talks to a device; the others just show bytes and meanings.

  python -m rimage2410.robot report e1          HID report for ESC!e1C (02 <length> ...)
  python -m rimage2410.robot report f1 200 2    HID report for Position Medium(200, Pick)
  python -m rimage2410.robot sense 002009       explain changer sense 2, printer sense 9
  python -m rimage2410.robot check '!g5C'       check whether a command is blocked
  python -m rimage2410.robot send e0            REALLY send to the autoloader (read only);
                                                every packet is logged to hid-<date>.log
                                                in the current directory
  python -m rimage2410.robot send f1 260 0 --confirm   a motion command, only with --confirm
"""

from __future__ import annotations

import sys

from . import commands as c
from . import sense as S


def hid_report(data: bytes) -> bytes:
    """Output report: 0x02, length, data (max. 58 bytes)."""
    if len(data) > 58:
        raise ValueError("longer than 58 bytes: needs several reports")
    return bytes([0x02, len(data)]) + data


def build(args: list[str]) -> c.Command:
    kind = args[0]
    if kind == "e0":
        return c.inquiry()
    if kind == "e1":
        return c.request_sense()
    if kind == "e7":
        return c.version_bytes()
    if kind == "e2":
        return c.fan_control(args[1] != "0")
    if kind == "f0":
        return c.read_element_status(int(args[1]))
    if kind == "f1":
        return c.position_medium(int(args[1]), int(args[2]))
    if kind == "f2":
        return c.open_close_element(int(args[1]), args[2] == "1")
    if kind == "f:":
        return c.get_string(int(args[1]))
    if kind == "f;":
        return c.get_counter(args[1])
    if kind == "f<":
        return c.everest_temperature()
    raise SystemExit(f"unknown command: {kind}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "report":
        cmd = build(argv[1:])
        rep = hid_report(cmd.data)
        print(f"{cmd.name}: {cmd.text}  (class {cmd.safety.value}, expects {cmd.expect} bytes)")
        print("HID-report:", rep.hex(" "))
        return 0
    if argv[0] == "sense":
        v = argv[1]
        ch, pr = int(v[:3]), int(v[3:6])
        print(f"changer {ch}: {S.changer_text(ch)}")
        print(f"printer {pr}: {S.printer_text(pr)}")
        return 0
    if argv[0] == "check":
        raw = argv[1].encode("ascii")
        raw = raw if raw.startswith(c.ESC) else c.ESC + raw
        try:
            c.check_allowed(raw)
            print("allowed (note: motion commands only with explicit confirmation)")
        except c.BlockedCommand as e:
            print(f"BLOCKED: {e}")
        return 0
    if argv[0] == "send":
        return send(argv[1:])
    print(__doc__)
    return 2


def send(args: list[str]) -> int:
    """Send one command to the real autoloader and explain the response."""
    import os
    import time
    from ..hardware import HidLoader, HardwareError
    ok = "--confirm" in args
    args = [a for a in args if a != "--confirm"]
    cmd = build(args)
    logfile = os.path.abspath(f"hid-{time.strftime('%Y-%m-%d')}.log")

    def log(line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with open(logfile, "a") as f:
            f.write(f"{stamp} {line}\n")
        print(f"  {line}")

    print(f"{cmd.name}: {cmd.text} (class {cmd.safety.value})")
    loader = HidLoader(log=log, allow_motion=lambda _c: ok)
    try:
        resp = loader.transact(cmd.data, cmd.expect, cmd.timeout_s)
    except HardwareError as e:
        print(f"ERROR: {e}")
        log(f"error: {e}")
        return 1
    finally:
        loader.close()
    print("response:", resp.replace(c.ESC, b"<ESC>").decode("ascii", "replace"))
    if cmd.data[1:4] == b"!e1" and len(resp) >= 10:
        s = c.parse_sense(cmd, resp)
        print(f"changer {s.changer}: {S.changer_text(s.changer)} | printer {s.printer}: {S.printer_text(s.printer)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
