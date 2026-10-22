"""Hardware connections and dry-run versions.

The real connections talk to the autoloader over HID, to the Everest 400 over the USB printer
class, and to the recorders as optical drives. The dry-run versions below keep a simple
simulated state (bins, gripper, recorder trays, printer) and answer in the format of
firmware 6.033, so the job sequence and the web interface can be tested without a machine.
"""

from __future__ import annotations

import errno
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .robot.commands import ESC, check_allowed


class Loader(Protocol):
    def transact(self, command: bytes, expect: int, timeout_s: float = 10.0) -> bytes: ...


class Printer(Protocol):
    def send_job(self, job: bytes) -> None: ...
    def query(self, command: bytes, expect: int) -> bytes: ...


class Burner(Protocol):
    device: str
    def burn(self, plan, log: Callable[[str], None]) -> None: ...
    def eject(self) -> None: ...
    def close_tray(self) -> None: ...
    def tray_open(self) -> bool | None: ...      # None = unknown


class HardwareError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Real autoloader over HID (hidraw)
# ---------------------------------------------------------------------------

RIMAGE_VID, AUTOLOADER_PID = 0x0CC3, 0x000D
# Commands that only read or move nothing. Everything else needs allow_motion (per command).
READ_ONLY_PREFIXES = (b"!e0", b"!e1", b"!e7", b"!f0", b"!f:", b"!f;", b"!f<",
                      b"!k0", b"!k1", b"!k2", b"!k3", b"!ka")


def find_hidraw(vid: int = RIMAGE_VID, pid: int = AUTOLOADER_PID) -> str | None:
    """Find the /dev/hidrawN of the autoloader through sysfs."""
    base = "/sys/class/hidraw"
    if not os.path.isdir(base):
        return None
    want = f"{vid:08X}:{pid:08X}"
    for name in sorted(os.listdir(base)):
        try:
            with open(os.path.join(base, name, "device", "uevent")) as f:
                if want in f.read().upper():
                    return f"/dev/{name}"
        except OSError:
            continue
    return None


class HidLoader:
    """The autoloader through /dev/hidrawN.

    Out: report 0x02, byte 1 = length (max. 58), then the data; 61 bytes per report.
    In:  report 0x01, byte 1 = length, then the data; reports until the expected number of bytes has arrived.

    Safety: the block list always applies (check_allowed). On top of that this class only lets
    read commands through, unless allow_motion(command) returns True, so a movement needs
    explicit consent. Every packet goes to log (hex and ASCII).
    """

    def __init__(self, path: str | None = None, log: Callable[[str], None] | None = None,
                 allow_motion: Callable[[bytes], bool] | None = None):
        self.path = path or find_hidraw()
        if not self.path:
            raise HardwareError("autoloader (0cc3:000d) not found; is it connected and is the udev rule active?")
        self.log = log or (lambda _m: None)
        self.allow_motion = allow_motion or (lambda _c: False)
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.lock = threading.Lock()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def _show(b: bytes) -> str:
        return b.hex(" ") + "  |" + "".join(chr(x) if 32 <= x < 127 else "." for x in b) + "|"

    def _read_report(self, timeout_s: float) -> bytes | None:
        import select
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout_s))
        if not r:
            return None
        return os.read(self.fd, 64)

    def _flush(self) -> None:
        for _ in range(11):
            rep = self._read_report(0.0)
            if not rep:
                return
            self.log(f"hid old   {self._show(rep)}")

    def transact(self, command: bytes, expect: int, timeout_s: float = 10.0) -> bytes:
        check_allowed(command)
        body = command[1:]
        if not body.startswith(READ_ONLY_PREFIXES) and not self.allow_motion(command):
            raise HardwareError(f"{body.decode('ascii', 'replace')} moves or changes something; "
                                "only allowed with consent (allow_motion)")
        with self.lock:
            try:
                return self._exchange(command, expect, timeout_s)
            except OSError as e:
                if e.errno not in _GONE:
                    raise
                # 2410 switched off and on, or USB briefly gone: reconnect. A read may be retried
                # once; never blindly repeat a movement (it may already have been carried out).
                self.log(f"hid gone ({e.strerror}); reconnecting")
                self.reopen()
                if body.startswith(READ_ONLY_PREFIXES):
                    return self._exchange(command, expect, timeout_s)
                raise HardwareError(f"connection to the autoloader was lost during "
                                    f"{body.decode('ascii', 'replace')}; not repeated") from e

    def reopen(self) -> None:
        try:
            self.close()
        except OSError:
            self.fd = None
        path = find_hidraw()
        if not path:
            raise HardwareError("autoloader (0cc3:000d) not found; is the 2410 switched on?")
        self.path = path
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.log(f"hid reconnected: {self.path}")

    def _exchange(self, command: bytes, expect: int, timeout_s: float) -> bytes:
        self._flush()
        for i in range(0, len(command), 58):
            chunk = command[i:i + 58]
            rep = bytes([0x02, len(chunk)]) + chunk
            rep = rep + b"\0" * (61 - len(rep))
            self.log(f"hid out   {self._show(rep[:2 + len(chunk)])}")
            os.write(self.fd, rep)
        data = b""
        deadline = time.monotonic() + timeout_s
        while len(data) < expect:
            rep = self._read_report(deadline - time.monotonic())
            if rep is None:
                raise HardwareError(f"timeout: received {len(data)} of {expect} bytes "
                                    f"({self._show(data) if data else 'nothing'})")
            self.log(f"hid in    {self._show(rep[:2 + rep[1]] if len(rep) > 1 else rep)}")
            if rep[0] != 0x01:
                raise HardwareError(f"unexpected report ID {rep[0]:#04x}")
            data += rep[2:2 + rep[1]]
        while True:                      # whatever follows immediately belongs to it too
            rep = self._read_report(0.05)
            if not rep:
                break
            self.log(f"hid in    {self._show(rep[:2 + rep[1]] if len(rep) > 1 else rep)}")
            if rep[0] == 0x01:
                data += rep[2:2 + rep[1]]
        return data


# Which recorder belongs to which autoloader element (241 = upper recorder, 242 = lower recorder).
# Key: serial number of the USB-SATA bridge of the recorder, which stays the same when the 2410 moves
# to another port or computer. Empty by default; set RIMAGE_RECORDERS="SERIAL=241,SERIAL=242".
RECORDER_SERIALS: dict[str, int] = {}
# Default mapping when no serial numbers match: the port on the internal hub.
RECORDER_HUB_PORTS = {"2": 241, "1": 242}


def _serials_from_env() -> dict[str, int]:
    """Parse RIMAGE_RECORDERS ("SERIAL=241,SERIAL=242"); malformed parts are ignored."""
    result: dict[str, int] = {}
    for part in os.environ.get("RIMAGE_RECORDERS", "").split(","):
        serial, sep, element = part.strip().partition("=")
        serial, element = serial.strip(), element.strip()
        if not sep or not serial or not element.isdigit():
            continue
        result[serial] = int(element)
    return result


def find_recorders(serials: dict[str, int] | None = None) -> list[tuple[str, int]]:
    """Recorders of the 2410 as (device, element), sorted by element (241 first).

    First through /dev/disk/by-id, using the serial number of each recorder's USB bridge. When
    serials is None, the mapping comes from RECORDER_SERIALS plus the environment variable
    RIMAGE_RECORDERS, for example "ABC123=241,DEF456=242". Without a match the port on
    the internal hub decides (sysfs path ending in .1 or .2, see RECORDER_HUB_PORTS). The names
    sr0/sr1 are not fixed: Linux numbers them in order of detection.
    """
    if serials is None:
        serials = {**RECORDER_SERIALS, **_serials_from_env()}
    found: dict[int, str] = {}
    by_id = "/dev/disk/by-id"
    if os.path.isdir(by_id):
        for name in os.listdir(by_id):
            for serial, el in serials.items():
                if serial in name:
                    found[el] = os.path.realpath(os.path.join(by_id, name))
    if len(found) < 2 and os.path.isdir("/sys/block"):
        for blk in sorted(os.listdir("/sys/block")):
            if not blk.startswith("sr"):
                continue
            path = os.path.realpath(f"/sys/block/{blk}/device")
            m = re.search(r"/\d+-[\d.]+\.(\d+):1\.0/", path)
            if m and m.group(1) in RECORDER_HUB_PORTS:
                found.setdefault(RECORDER_HUB_PORTS[m.group(1)], f"/dev/{blk}")
    return [(dev, el) for el, dev in sorted(found.items())]


_GONE = (errno.ENODEV, errno.EIO, errno.EBADF, errno.ENOENT)


def find_usblp(vid: int = 0x044E, pid: int = 0x1311) -> str | None:
    """Find the /dev/usb/lpN of the Everest 400 through sysfs."""
    base = "/sys/class/usbmisc"
    if not os.path.isdir(base):
        return None
    for name in sorted(os.listdir(base)):
        dev = os.path.realpath(os.path.join(base, name, "device", ".."))
        try:
            with open(os.path.join(dev, "idVendor")) as f1, open(os.path.join(dev, "idProduct")) as f2:
                if int(f1.read(), 16) == vid and int(f2.read(), 16) == pid:
                    return f"/dev/usb/{name}"
        except (OSError, ValueError):
            continue
    return None


class UsbPrinter:
    """The Everest 400 through /dev/usb/lpN (usblp, bidirectional).

    query: only the read-only status queries (everest.job.STATUS_COMMANDS). send_job: the print
    job, only with allow_jobs=True. Every packet goes to log.
    """

    def __init__(self, path: str | None = None, log: Callable[[str], None] | None = None,
                 allow_jobs: bool = False):
        self.path = path or find_usblp()
        if not self.path:
            raise HardwareError("Everest 400 (044e:1311) not found")
        self.log = log or (lambda _m: None)
        self.allow_jobs = allow_jobs
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.lock = threading.Lock()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def reopen(self) -> None:
        """Reconnect: after a restart the printer registers as a new USB device, and the old
        device file then returns ENODEV."""
        try:
            self.close()
        except OSError:
            self.fd = None
        self.path = find_usblp() or self.path
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.log(f"lp reconnected: {self.path}")

    def query(self, command: bytes, expect: int, timeout_s: float = 3.0) -> bytes:
        """Status query; if the device has gone, reconnect once and ask again."""
        try:
            return self._query(command, expect, timeout_s)
        except OSError as e:
            if e.errno not in _GONE:
                raise
            self.log(f"lp gone ({e.strerror}); reconnecting")
            self.reopen()
            return self._query(command, expect, timeout_s)

    def _query(self, command: bytes, expect: int, timeout_s: float = 3.0) -> bytes:
        import select
        from .everest.job import STATUS_COMMANDS
        allowed = {v[0] for v in STATUS_COMMANDS.values()}
        if command not in allowed:
            raise HardwareError(f"printer command not allowed: {command!r}")
        with self.lock:
            while True:                      # discard stale data
                r, _, _ = select.select([self.fd], [], [], 0)
                if not r or not os.read(self.fd, 512):
                    break
            self.log(f"lp out    {HidLoader._show(command)}")
            os.write(self.fd, command)
            data = b""
            deadline = time.monotonic() + timeout_s
            while len(data) < expect:
                left = deadline - time.monotonic()
                r, _, _ = select.select([self.fd], [], [], max(0.0, left))
                if not r:
                    break
                try:
                    chunk = os.read(self.fd, 512)
                except BlockingIOError:
                    time.sleep(0.02)
                    continue
                if not chunk:
                    time.sleep(0.02)
                    if time.monotonic() > deadline:
                        break
                    continue
                data += chunk
            self.log(f"lp in     {HidLoader._show(data) if data else '(nothing)'}")
            if not data:
                raise HardwareError("no response from the printer")
            return data

    def send_job(self, job: bytes, progress: Callable[[int, float], None] | None = None,
                 packet_timeout_s: float = 2.0) -> float:
        """Send a print job; returns the duration in seconds.

        In non-blocking mode usblp accepts only one URB (8 KB) per write. Wait with poll until the
        driver has room again, not with a fixed pause: with 10 ms per 8 KB the download is too slow
        (about 0.8 MB/s) and the printer restarts halfway through printing on the film. The whole
        job should arrive within 10 s. progress(sent, seconds) is called once per MB.
        """
        import select
        if not self.allow_jobs:
            raise HardwareError("print jobs to the real printer are disabled (allow_jobs)")
        with self.lock:
            self.log(f"lp out    print job of {len(job)} bytes")
            poller = select.poll()
            poller.register(self.fd, select.POLLOUT)
            view = memoryview(job)
            sent, next_report, t0 = 0, 1 << 20, time.monotonic()
            while view:
                if not poller.poll(packet_timeout_s * 1000):
                    raise HardwareError(f"printer stopped accepting data after {sent} bytes")
                try:
                    n = os.write(self.fd, view[:65536])
                except BlockingIOError:
                    continue
                except OSError as e:
                    if e.errno not in _GONE:
                        raise
                    # printer restarted during the download: do not resend, but do reconnect
                    self.log(f"lp gone after {sent} bytes ({e.strerror})")
                    try:
                        self.reopen()
                    except OSError:
                        pass
                    raise HardwareError(f"printer disconnected after {sent} of {len(job)} bytes") from e
                view = view[n:]
                sent += n
                if progress and (sent >= next_report or not view):
                    progress(sent, time.monotonic() - t0)
                    next_report += 1 << 20
            took = time.monotonic() - t0
            self.log(f"lp out    print job sent in {took:.1f} s")
            return took


# ---------------------------------------------------------------------------
# Simulated machine
# ---------------------------------------------------------------------------

@dataclass
class SimState:
    bins: dict[int, int] = field(default_factory=lambda: {200: 25, 201: 0, 202: 0})
    side_bin: int = 0           # side bin (element 300); the real loader reports no count
    side_capacity: int = 5
    gripper: bool = False
    drives: dict[int, dict] = field(default_factory=lambda: {
        241: {"tray_open": False, "disc": False},
        242: {"tray_open": False, "disc": False},
    })
    drawer_open: bool = False
    printer_disc: bool = False
    printer_sense: int = 0      # Everest status
    changer_sense: int = 0
    awake: bool = True
    label_on_film: bool = False
    roller_temp: int = 180      # heat roller in degrees; cold is about 25
    roller_step: int = 25       # this much warmer per !f<0C poll while the printer is awake
    door_open: bool = False
    door_polls: int = 0         # door closes by itself after this many Request Sense polls
    # fault simulation for tests: name -> number of times the fault still occurs
    #   clamp: placing in the printer gives a clamp error; pick_bin: picking from a bin fails
    #   burn: burning fails; label_lost: printer falls back to 0 after the download
    #   print_zero: 0 instead of 4 after the retransfer; printer_error: see printer_error_code
    #   tray_stuck: recorder tray stays open after closing
    faults: dict[str, int] = field(default_factory=dict)
    printer_error_code: int = 0x29
    step_delay_s: float = 0.0   # delay per movement, for a more realistic feel in the UI
    ribbon_code: str = "0"      # ESC!r3C d2: '0' CMY, '1' CMY+W, '2' black, 'E' no ribbon
    ribbon_remaining: int = 437
    film_percent: int = 82
    printed_total: int = 1234
    lock: threading.Lock = field(default_factory=threading.Lock)


def _resp(cmd: bytes, payload: str) -> bytes:
    return cmd[:4] + payload.encode("ascii")


class DryRunLoader:
    """Answers autoloader commands as if firmware 6.033 were running a 2410."""

    def __init__(self, state: SimState, log: Callable[[str], None] | None = None):
        self.state = state
        self.log = log or (lambda _m: None)

    def transact(self, command: bytes, expect: int, timeout_s: float = 10.0) -> bytes:
        check_allowed(command)
        body = command[1:].decode("ascii")
        self.log(f"loader <- {body}")
        with self.state.lock:
            resp = self._handle(command, body)
        if self.state.step_delay_s and body.startswith(("!f1", "!f2")):
            time.sleep(self.state.step_delay_s)
        self.log(f"loader -> {resp[1:].decode('ascii', 'replace')}")
        return resp

    # -- internal handling --
    def _fail(self, cmd: bytes, sense: int) -> bytes:
        self.state.changer_sense = sense
        return _resp(cmd, "0C")

    def _ok(self, cmd: bytes) -> bytes:
        self.state.changer_sense = 0
        return _resp(cmd, "1C")

    def _handle(self, cmd: bytes, body: str) -> bytes:
        s = self.state
        if body == "!e0C":
            text = "RIMAGE 2410     VERSION 6.033A  EVEREST         "
            return _resp(cmd, f"{len(text)}C{text}")
        if body == "!e1C":
            if s.door_open:
                s.changer_sense = 6
                if s.door_polls > 0:
                    s.door_polls -= 1
                    if s.door_polls == 0:
                        s.door_open = False
            resp = _resp(cmd, f"{s.changer_sense:03d}{s.printer_sense:03d}C")
            if not s.door_open and s.changer_sense == 6:
                s.changer_sense = 0
            return resp
        if body == "!e7C":
            return _resp(cmd, "1CFD;00;")
        if m := re.fullmatch(r"!e2([012])C", body):
            s.awake = m.group(1) != "0"
            if not s.awake:
                s.roller_temp = 25          # standby: the roller cools down
            if s.awake and s.printer_sense == 0x0C:
                s.printer_sense = 0
            return self._ok(cmd)
        if m := re.fullmatch(r"!f0(\d{3})C", body):
            el = int(m.group(1))
            if el in s.bins:
                return _resp(cmd, f"1{1 if s.bins[el] else 0}{min(s.bins[el], 99999):05d}C")
            if el == 220:
                return _resp(cmd, f"1{1 if s.gripper else 0}00000C")
            if el == 300:
                return _resp(cmd, "1000000C")           # like the real loader: always 0
            if el in s.drives:
                return _resp(cmd, f"1{1 if s.drives[el]['disc'] else 0}00000C")
            if el == 240:
                open_ = [e for e, d in s.drives.items() if d['tray_open']]
                if len(open_) != 1:
                    return _resp(cmd, "0065534C")
                return _resp(cmd, f"13{open_[0]:05d}C")
            if el == 260:
                return _resp(cmd, f"1{1 if s.printer_disc else 0}00000C")
            if el == 270:
                return _resp(cmd, "1000500C")
            return _resp(cmd, "0065534C")
        if m := re.fullmatch(r"!f:(\d{3})C", body):
            return _resp(cmd, "1C" + {0: "SIM00001", 260: "EV400SIM", 270: "CMY 500   ", 99: "11000000"}.get(int(m.group(1)), ""))
        if body == "!f<0C":
            # '3' + 'C' + three numbers "%6.5+d;"; byte 5 is '3'
            if s.awake and s.roller_temp < 172:
                s.roller_temp = min(172, s.roller_temp + s.roller_step)
            return cmd[:5] + f"3C{40:+06d};{s.roller_temp:+06d};{30:+06d};".encode("ascii")
        if m := re.fullmatch(r"!f2260([01])C", body):
            if s.door_open:
                return self._fail(cmd, 6)
            s.drawer_open = m.group(1) == "1"
            return self._ok(cmd)
        if m := re.fullmatch(r"!f1(\d{3})(\d)C", body):
            if s.door_open:
                return self._fail(cmd, 6)
            return self._position(cmd, int(m.group(1)), int(m.group(2)))
        return self._fail(cmd, 5)   # unknown command: Illegal Request

    def _fault(self, name: str) -> bool:
        n = self.state.faults.get(name, 0)
        if n > 0:
            self.state.faults[name] = n - 1
            return True
        return False

    def _position(self, cmd: bytes, el: int, act: int) -> bytes:
        s = self.state
        if act == 0:
            return self._ok(cmd)
        if el == 240 and act == 2:                    # locating a recorder
            open_ = [e for e, d in s.drives.items() if d["tray_open"]]
            if not open_:
                return self._fail(cmd, 7)
            d = s.drives[open_[0]]
            if not d["disc"]:
                return self._fail(cmd, 3)
            d["disc"], s.gripper = False, True
            return self._ok(cmd)
        if el == 300:                                   # side bin
            if act == 0:
                return self._ok(cmd)
            if act in (1, 9):
                if not s.gripper:
                    return self._fail(cmd, 3)
                if s.side_bin >= s.side_capacity:
                    return self._fail(cmd, 22)
                s.side_bin += 1
                s.gripper = False
                return self._ok(cmd)
            return self._fail(cmd, 5)
        if el in s.bins:
            if act == 2:
                if s.gripper:
                    return self._fail(cmd, 5)
                if s.bins[el] == 0 or self._fault("pick_bin"):
                    return self._fail(cmd, 3)
                s.bins[el] -= 1
                s.gripper = True
                return self._ok(cmd)
            if act in (1, 9):
                if not s.gripper:
                    return self._fail(cmd, 3)
                s.bins[el] += 1
                s.gripper = False
                return self._ok(cmd)
        if el in s.drives:
            d = s.drives[el]
            if not d["tray_open"]:
                return self._fail(cmd, 7)
            if act == 2:
                if s.gripper or not d["disc"]:
                    return self._fail(cmd, 3)
                d["disc"], s.gripper = False, True
                return self._ok(cmd)
            if act in (1, 9):
                if not s.gripper or d["disc"]:
                    return self._fail(cmd, 3)
                d["disc"], s.gripper = True, False
                return self._ok(cmd)
        if el == 260:
            # placing only works in the open tray; for picking the firmware handles the tray itself (no !f2)
            if act in (1, 9) and not s.drawer_open:
                return self._fail(cmd, 4)
            if act == 2:
                if s.gripper or not s.printer_disc:
                    return self._fail(cmd, 3)
                s.printer_disc, s.gripper = False, True
                if s.printer_sense == 0x04:
                    s.printer_sense = 0
                elif s.printer_sense == 34:          # clamp error cleared by picking the disc back up
                    s.printer_sense = 0x09 if s.label_on_film else 0
                return self._ok(cmd)
            if act in (1, 9):
                if not s.gripper or s.printer_disc:
                    return self._fail(cmd, 14)
                s.printer_disc, s.gripper = True, False
                if self._fault("clamp"):
                    s.printer_sense = 34
                    return self._fail(cmd, 27)
                return self._ok(cmd)
            if act in (4, 7):
                if not (s.printer_disc and s.label_on_film and not s.drawer_open):
                    return self._fail(cmd, 4)
                s.label_on_film = False
                s.printer_sense = 0 if self._fault("print_zero") else 0x04   # 4: done, disc is in the printer
                s.ribbon_remaining = max(0, s.ribbon_remaining - 1)
                s.film_percent = max(0, s.film_percent - (1 if s.printed_total % 5 == 0 else 0))
                s.printed_total += 1
                return self._ok(cmd)
            if act in (6, 8):
                s.printer_sense = 0
                s.label_on_film = False
                return self._ok(cmd)
        return self._fail(cmd, 5)


class DryRunPrinter:
    """Saves print jobs as files and sets the simulated printer to 'label on film'."""

    def __init__(self, state: SimState, workdir: str, log: Callable[[str], None] | None = None):
        self.state = state
        self.dir = os.path.join(workdir, "printjobs")
        os.makedirs(self.dir, exist_ok=True)
        self.log = log or (lambda _m: None)
        self.count = 0

    def send_job(self, job: bytes) -> None:
        if not job.startswith(ESC + b"&e"):
            raise HardwareError("print job does not start with ESC&e")
        self.count += 1
        path = os.path.join(self.dir, f"job-{int(time.time())}-{self.count}.prn")
        with open(path, "wb") as f:
            f.write(job)
        self.log(f"printer: job of {len(job)} bytes saved as {path}")
        with self.state.lock:
            if not self.state.awake:
                raise HardwareError("printer is asleep (simulation)")
            st = self.state
            if st.faults.get("label_lost", 0) > 0:
                st.faults["label_lost"] -= 1
                st.printer_sense = 0
                return
            if st.faults.get("printer_error", 0) > 0:
                st.faults["printer_error"] -= 1
                st.printer_sense = st.printer_error_code
                return
            st.label_on_film = True
            st.printer_sense = 0x09

    def query(self, command: bytes, expect: int) -> bytes:
        if command.startswith(ESC + b"!m2") and len(command) == 9 and command.endswith(b"C"):
            self.log(f"printer: ESC!m2 {command[4:8].hex(' ')}")
            return command[:4] + b"1C"
        body = command[1:].decode("ascii")
        if body not in ("!r3C", "!r4C", "!r6C", "!i6C"):
            raise HardwareError(f"printer command not allowed in dry run: {body}")
        with self.state.lock:
            s = self.state
            d1 = {0x09: "9", 0x01: "1", 0x05: "5", 0x04: "4"}.get(s.printer_sense, "0")
            film = "1" if s.film_percent > 0 else "E"
            if body == "!r3C":
                # d0 d1 d2 d3 d4d5 d6: -, state, ribbon, film, error code, switch position
                return command[:4] + b"7C" + f"0{d1}{s.ribbon_code}{film}001".encode("ascii")
            if body == "!r6C":
                return command[:4] + b"13C" + struct.pack("<IIBHH", s.printed_total, s.printed_total,
                                                          s.film_percent, s.ribbon_remaining, 0xFFFF)
        return command[:4] + b"0C"


class DryRunBurner:
    """Pretends to burn; does nothing."""

    def __init__(self, state: SimState, element: int, device: str, burn_s: float = 0.0):
        self.state = state
        self.element = element
        self.device = device
        self.burn_s = burn_s

    def burn(self, plan, log: Callable[[str], None]) -> None:
        with self.state.lock:
            if not self.state.drives[self.element]["disc"]:
                raise HardwareError(f"no disc in {self.device}")
            fail = self.state.faults.get("burn", 0) > 0
            if fail:
                self.state.faults["burn"] -= 1
        for line in plan.describe(self.device):
            log(f"[dry run] {line}")
        if self.burn_s:
            time.sleep(self.burn_s)
        if fail:
            raise HardwareError("simulated burn error (verify failed)")

    def eject(self) -> None:
        with self.state.lock:
            self.state.drives[self.element]["tray_open"] = True

    def close_tray(self) -> None:
        with self.state.lock:
            st = self.state
            if st.faults.get("tray_stuck", 0) > 0:
                st.faults["tray_stuck"] -= 1
                return
            st.drives[self.element]["tray_open"] = False

    def tray_open(self) -> bool | None:
        with self.state.lock:
            return self.state.drives[self.element]["tray_open"]
