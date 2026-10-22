"""High-level motion sequences and wait loops for the autoloader.

Everything goes through a Loader (hardware.py); this module never sends anything to a device itself.

Errors: anything that needs a decision from the operator goes through `ask` (a Problem with
options). Without an operator (ask is None) every question becomes a LoaderError and the job
stops. Waiting without a choice (door open, cover open) is reported through `notice`.

Collisions with an open recorder tray: the autoloader has no sensor that sees an open tray in
its path (only motion detectors that notice a stalled lift). Collisions are avoided by the order
of steps: a tray only opens once the arm is above it, and after closing we check that it is
closed. On top of that:
- before every arm movement (every Position Medium) we check all recorder trays; a tray that is
  open without us having opened it is closed first (if that fails: ask the operator);
- the eject buttons are behind the door, so pressing one means opening the door first. The
  autoloader then stops the arm itself (door sensor, changer sense 6), and as soon as the door is
  closed we check the trays. (Locking the trays with PREVENT ALLOW MEDIUM REMOVAL does not work
  through the USB bridge of the 2410.)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import commands as c
from . import sense as S

E = c.Element
A = c.Action


class LoaderError(RuntimeError):
    def __init__(self, message: str, sense: c.Sense | None = None):
        super().__init__(message)
        self.sense = sense


class Stopped(RuntimeError):
    """The user stopped the job."""


class RejectDisc(RuntimeError):
    """The operator chose 'reject disc': the disc goes to the reject bin and the job continues."""


class LabelLost(RuntimeError):
    """The label is not (or no longer) on the film; it has to be sent to the printer again."""


class PrintFailed(RuntimeError):
    """Printing failed after the retransfer."""


OPTION_LABELS = {
    "retry": "Retry",
    "reject": "Reject disc",
    "stop": "Stop job",
    "yes": "Yes",
    "no": "No",
    "allow": "Allow",
}


@dataclass
class Problem:
    """Something that needs the operator (a transporter, printer or recorder error)."""

    code: str
    title: str
    message: str
    options: tuple[str, ...] = ("retry", "stop")
    device: str = "autoloader"
    sense: c.Sense | None = None

    def as_dict(self) -> dict:
        d = {"code": self.code, "title": self.title, "message": self.message, "device": self.device,
             "options": [{"id": o, "label": OPTION_LABELS.get(o, o)} for o in self.options]}
        if self.sense is not None:
            d["sense"] = {"changer": self.sense.changer, "printer": self.sense.printer,
                          "changer_text": S.changer_text(self.sense.changer),
                          "printer_text": S.printer_text(self.sense.printer)}
        return d


@dataclass
class Timing:
    poll_s: float = 1.0               # Request Sense interval while waiting
    printer_ready_s: float = 600.0    # warming up or initializing (Everest 400: 600 s)
    preprint_s: float = 600.0         # label on film (4 min download plus waiting for 9, up to 10 min)
    roller_warm_s: float = 480.0      # heat roller warm
    roller_min_temp: int = 155        # wait until the temperature is above 154
    print_done_s: float = 300.0       # 600 x 0.5 s
    retransfer_start_s: float = 5.0   # after !f12604C, check for 5 s that the printer leaves state 9
    tray_settle_s: float = 2.0        # recorder tray closed: wait 2 s, then check
    tray_recheck_s: float = 3.0       # still open: wait 3 s and check again
    clamp_retries: int = 2            # "Clamp Retries" (default 2)
    pick_retries: int = 4             # picking from a bin: up to 4 attempts while the gripper stays empty
    door_poll_s: float = 0.5          # door open: poll every 500 ms


def drive_from_position(value: int) -> int | None:
    """Value of Read Element Status 240 after searching for an open tray -> recorder element.

    Position ranges for loaders without a mailslot. If the firmware returns 241 or 242 directly,
    that is the element.
    """
    if value in (E.DRIVE_1, E.DRIVE_2):
        return int(value)
    if value < 12000:
        return int(E.DRIVE_1)
    if value < 16200:
        return int(E.DRIVE_2)
    if value < 20400:
        return int(E.DRIVE_1)
    if value < 24600:
        return int(E.DRIVE_2)
    return None


_RETRANSFER = (c.start_retransfer(False).data, c.start_retransfer(True).data)


class Robot:
    def __init__(self, loader, log: Callable[[str], None],
                 should_stop: Callable[[], bool] = lambda: False,
                 timing: Timing | None = None, sleep: Callable[[float], None] = time.sleep,
                 ask: Callable[[Problem], str] | None = None,
                 notice: Callable[[Problem | None], None] | None = None):
        self.loader = loader
        self.log = log
        self.should_stop = should_stop
        self.t = timing or Timing()
        self.sleep = sleep
        self.ask = ask
        self.notice = notice or (lambda _p: None)
        self._auto_reset_code: int | None = None
        self.burners: list = []                 # all recorders: their trays are checked before every movement
        self._intended_open: set[str] = set()   # trays we opened ourselves (the arm may go there)

    # -- basics --------------------------------------------------------------
    def send(self, cmd: c.Command) -> bytes:
        if self.should_stop():
            raise Stopped("stopped")
        return self.loader.transact(cmd.data, cmd.expect, cmd.timeout_s)

    def sense(self) -> c.Sense:
        cmd = c.request_sense()
        return c.parse_sense(cmd, self.send(cmd))

    def inquiry(self) -> c.Inquiry:
        cmd = c.inquiry()
        return c.parse_inquiry(cmd, self.send(cmd))

    def element(self, element: int) -> c.ElementStatus:
        cmd = c.read_element_status(element)
        return c.parse_element_status(cmd, self.send(cmd))

    def gripper_full(self) -> bool:
        return self.element(E.GRIPPER).occupied

    def printer_full(self) -> bool:
        return self.element(E.PRINTER).occupied

    # -- the operator------------------------------------------------------
    def _ask(self, p: Problem) -> str:
        """Ask the operator what to do. 'stop' and 'reject' become exceptions."""
        self.log(f"operator needed: {p.title}. {p.message}")
        if self.ask is None:
            raise LoaderError(f"{p.title}: {p.message}", p.sense)
        choice = self.ask(p)
        self.log(f"operator chose: {OPTION_LABELS.get(choice, choice)}")
        if choice == "stop":
            raise Stopped("stopped by the operator")
        if choice == "reject":
            raise RejectDisc(p.title)
        return choice

    @staticmethod
    def _problem_from(code: str, title: str, s: c.Sense | None, hint: str,
                      options=("retry", "stop"), device: str = "autoloader") -> Problem:
        detail = ""
        if s is not None:
            detail = f" (autoloader: {S.changer_text(s.changer)}"
            if s.printer:
                detail += f", printer: {S.printer_text(s.printer)}"
            detail += ")"
        return Problem(code, title, hint + detail, tuple(options), device=device, sense=s)

    # -- commands with the standard error handling --------------------------
    def _try(self, cmd: c.Command, *, sense_first: bool = False) -> c.Sense | None:
        """Send a command with a 6-byte response. None = success, otherwise the sense after the last attempt.

        Changer sense 2: retry once. Door open: wait and retry. Printer sense 5 (initializing,
        not for the retransfer): wait until it is over and retry. Printer sense 10: retry every
        second, at most 5 times.
        """
        if sense_first:
            self.sense()
        not_ready = sw10 = 0
        while True:
            if c.parse_simple(cmd, self.send(cmd)):
                return None
            s = self.sense()
            if S.changer_needs_door(s.changer):
                self.wait_door_closed()
                continue
            if s.changer == S.CHANGER_NOT_READY and not_ready < 1:
                not_ready += 1
                self.log(f"{cmd.text}: not ready, retrying")
                continue
            if s.printer == S.PRINTER_INITIALIZING and cmd.data not in _RETRANSFER:
                self._wait_while(lambda x: x.printer == S.PRINTER_INITIALIZING)
                continue
            if s.printer == S.PRINTER_SWITCH and sw10 < 5:
                sw10 += 1
                self.sleep(1.0)
                continue
            return s

    def _simple(self, cmd: c.Command) -> None:
        s = self._try(cmd)
        if s is not None:
            raise LoaderError(f"{cmd.name} {cmd.text} failed: {S.changer_text(s.changer)}", s)

    def pm(self, element: int, action: int, timeout_s: float = 60.0) -> c.Sense | None:
        """Position Medium, always preceded by a Request Sense. First we check that no recorder
        tray is unexpectedly open (collision protection)."""
        self.guard_trays()
        return self._try(c.position_medium(element, action, timeout_s), sense_first=True)

    def guard_trays(self) -> None:
        """Every recorder tray we did not open ourselves must be closed before the arm moves."""
        for b in self.burners:
            if b.device in self._intended_open:
                continue
            if self._tray_state(b):
                self.log(f"safety: tray of {b.device} is open while the arm has to move; "
                         "closing it first")
                self.close_tray(b)

    def _wait_while(self, cond: Callable[[c.Sense], bool]) -> c.Sense:
        """Poll while cond is true, without a time limit (stopping is still possible)."""
        while True:
            s = self.sense()
            if not cond(s):
                return s
            self.sleep(self.t.poll_s)

    def wait_door_closed(self) -> None:
        """Door open: pause without buttons and poll every 500 ms, without a time limit."""
        self.notice(Problem("door", "Door open", "Close the door of the 2410. The job then continues by itself.",
                            options=()))
        self.log("door open: waiting until the door is closed")
        while True:
            s = self.sense()
            if not S.changer_needs_door(s.changer):
                break
            self.sleep(self.t.door_poll_s)
        self.notice(None)
        self.log("door closed; checking recorder trays before the arm moves again")
        self.guard_trays()

    # -- startup -------------------------------------------------------------
    def startup(self) -> c.Inquiry:
        info = self.inquiry()
        self.log(f"autoloader: {info.text.strip()}")
        s = self.sense()
        self.log(f"status: loader '{S.changer_text(s.changer)}', printer '{S.printer_text(s.printer)}'")
        self.printer_awake()
        self.log("checking recorder trays")
        self.guard_trays()
        self.log("arm to home position")
        s = self.pm(E.PRINTER, A.MOVE)
        if s is not None:
            raise LoaderError(f"moving the arm home failed: {S.changer_text(s.changer)}", s)
        self.require_gripper(False, "at startup")
        return info

    # -- gripper checks (Read Element Status 220) ------------------------------
    def require_gripper(self, full: bool, where: str) -> None:
        while self.gripper_full() != full:
            if full:
                self._ask(Problem("disc_lost", "Disc lost",
                                  f"The arm no longer holds a disc ({where}). Put the disc back in the gripper "
                                  "or find it, then choose Retry."))
            else:
                self._ask(Problem("disc_in_gripper", "Unexpected disc in the gripper",
                                  f"There is a disc in the gripper ({where}). Remove it and choose Retry."))

    # -- movements ----------------------------------------------------------
    def move_above(self, element: int, what: str) -> None:
        """Arm above a recorder or the printer (PM action 0); one silent retry, then ask the operator."""
        attempt = 0
        while True:
            s = self.pm(element, A.MOVE)
            if s is None:
                return
            attempt += 1
            if attempt < 2:
                continue
            self._ask(self._problem_from("arm_above", "Arm does not reach its position", s,
                                         f"The arm does not move {what}. Check that nothing blocks the arm "
                                         "and choose Retry."))
            attempt = 0

    def pick_from_bin(self, element: int) -> None:
        """Pick a blank disc from a bin."""
        n = int(element) - int(E.BIN_1) + 1
        self.require_gripper(False, f"before picking from bin {n}")
        empty_grip = fails = 0
        while True:
            s = self.pm(element, A.PICK)
            if s is None:
                if self.gripper_full():
                    return
                empty_grip += 1
                if empty_grip < self.t.pick_retries:
                    self.log(f"picking from bin {n}: gripper empty, retrying")
                    continue
                self._ask(Problem("pick_bin", "Picking from the bin failed",
                                  f"The arm did not pick a disc from bin {n}. Check that the discs lie flat "
                                  "and do not stick together, then choose Retry."))
                empty_grip = 0
                continue
            if s.changer == S.CHANGER_NO_DISC and s.printer == 0 and self.gripper_full():
                self.log(f"picking from bin {n}: error reported, but the disc is in the gripper")
                return
            if s.changer == 4:
                self._ask(self._problem_from("disc_dropped", "Disc may have dropped", s,
                                             f"The disc may have dropped while picking from bin {n}. "
                                             "Find it and choose Retry."))
                continue
            if not self.element(element).occupied:
                self._ask(Problem("bin_empty", f"Input bin {n} is empty",
                                  f"There are no blank discs left in bin {n}. Refill the bin and choose Retry."))
                continue
            fails += 1
            if fails < 3:
                self.log(f"picking from bin {n} failed ({S.changer_text(s.changer)}), retrying")
                continue
            self._ask(self._problem_from("pick_bin", "Picking from the bin failed", s,
                                         f"The arm cannot pick a disc from bin {n}. Check the bin and choose Retry."))
            fails = 0

    def put_in_bin(self, element: int) -> None:
        """Put the disc from the gripper into a bin or into the side bin (element 300)."""
        side = int(element) == int(E.MAILSLOT)
        name = "the side bin" if side else f"bin {int(element) - int(E.BIN_1) + 1}"
        full = (S.CHANGER_BIN_FULL, 20) if side else (S.CHANGER_BIN_FULL,)   # 20 = mailslot full
        self.require_gripper(True, f"before storing in {name}")
        while True:
            s = self.pm(element, A.PLACE)
            if s is None:
                self.sense()
                return
            if s.changer in full:
                self._ask(Problem("bin_full", f"{name[0].upper()}{name[1:]} is full",
                                  f"Remove the discs from {name} and choose Retry.", sense=s))
                continue
            self._ask(self._problem_from("store", f"Storing in {name} failed", s,
                                         "Check the bin and the gripper, then choose Retry."))

    # -- recorder trays (driven by the PC, not the autoloader) ----------------
    @staticmethod
    def _tray_state(burner) -> bool | None:
        fn = getattr(burner, "tray_open", None)
        return fn() if fn else None

    def open_tray(self, burner) -> None:
        """Open the tray (the PC does this through eject). Only call this with the arm above it or at the top."""
        self._intended_open.add(burner.device)
        while True:
            try:
                burner.eject()
                state = self._tray_state(burner)
            except Exception as e:  # noqa: BLE001 - eject can fail in many ways
                self.log(f"{burner.device}: opening the tray failed: {e}")
                state = False
            if state is not False:
                return
            self._ask(Problem("tray_open", f"Tray of {burner.device} does not open",
                              "Check the recorder and choose Retry.", device="recorder"))

    def close_tray(self, burner) -> None:
        """Close the tray, wait 2 s and check; still open: wait 3 s and check again."""
        while True:
            try:
                burner.close_tray()
            except Exception as e:  # noqa: BLE001
                self.log(f"{burner.device}: closing the tray failed: {e}")
            self.sleep(self.t.tray_settle_s)
            state = self._tray_state(burner)
            if state:
                self.sleep(self.t.tray_recheck_s)
                state = self._tray_state(burner)
            if not state:
                self._intended_open.discard(burner.device)
                return
            self._ask(Problem("tray_close", f"Tray of {burner.device} does not close",
                              "Check that the disc sits properly in the tray and choose Retry.", device="recorder"))

    def load_drive(self, burner, bin_element: int) -> None:
        """Move a blank disc from the input bin to a recorder."""
        el = int(getattr(burner, "element", E.DRIVE_1))
        self.pick_from_bin(bin_element)
        while True:
            self.move_above(el, f"above {burner.device}")
            self.require_gripper(True, "after picking from the bin")
            self.open_tray(burner)
            s = self.pm(el, A.PLACE)
            if s is None:
                break
            self.pm(E.DRIVE_ANY, A.MOVE)
            self._ask(self._problem_from("place_tray", f"Disc not placed in {burner.device}", s,
                                         "Check the recorder tray and choose Retry."))
        self.close_tray(burner)

    def unload_drive(self, burner, to_printer: bool) -> None:
        """Take the disc from a recorder into the gripper."""
        el = int(getattr(burner, "element", E.DRIVE_1))
        self.require_gripper(False, f"before picking from {burner.device}")
        if to_printer:
            self.move_above(el, f"above {burner.device}")
        else:
            self.pm(E.DRIVE_ANY, A.MOVE)          # gripper to the top, result not checked
        self.open_tray(burner)
        attempt = 0
        while True:
            s = self.pm(el, A.PICK)
            if s is None:
                break
            if S.changer_needs_door(s.changer) and self.gripper_full():
                self.pm(E.DRIVE_ANY, A.MOVE)
                break
            if s.changer == S.CHANGER_NO_DISC and attempt < 4:
                attempt += 1
                if attempt <= 2:
                    self.log(f"picking from {burner.device} failed, retrying in 1 s")
                else:
                    self.log(f"picking from {burner.device} failed, closing and reopening the tray")
                    self.close_tray(burner)
                    self.open_tray(burner)
                self.sleep(1.0)
                continue
            self._ask(self._problem_from("pick_tray", f"Picking from {burner.device} failed", s,
                                         "Put the disc in the centre of the recorder tray and choose Retry."))
            attempt = 0
        self.close_tray(burner)

    # -- printer tray and printer ------------------------------------------------
    def drawer(self, open_: bool) -> c.Sense | None:
        return self._try(c.open_close_element(E.PRINTER, open_))

    def open_drawer(self) -> None:
        """Open the printer tray: one silent retry, then ask the operator."""
        attempt = 0
        while True:
            s = self.drawer(True)
            if s is None:
                return
            attempt += 1
            if attempt < 2:
                continue
            self._ask(self._problem_from("printer_drawer_open", "Printer tray does not open", s,
                                         "Check the printer tray and choose Retry.", device="printer"))
            attempt = 0

    def close_drawer_checked(self) -> None:
        """Close the printer tray and check: after closing, the printer must report a disc."""
        busy9 = clamp = other = 0
        while True:
            s = self.drawer(False)
            if s is None:
                if self.printer_full():
                    return
                s = c.Sense(changer=S.CHANGER_CLAMP, printer=S.PRINTER_CLAMP)
            if s.printer == S.PRINTER_WAITING_FOR_DISC and busy9 < 3:
                busy9 += 1
                continue
            if (s.printer == S.PRINTER_CLAMP or s.changer == S.CHANGER_CLAMP) and clamp < 2:
                clamp += 1
                self.log("printer tray: disc not clamped, closing again")
                continue
            if other < 1 and s.printer != S.PRINTER_CLAMP:
                other += 1
                self.open_drawer()
                continue
            self.open_drawer()
            self._ask(self._problem_from("printer_drawer_close", "Printer tray does not close properly", s,
                                         "Place the disc properly in the open printer tray and choose Retry.",
                                         ("retry", "reject", "stop"), device="printer"))
            busy9 = clamp = other = 0

    def load_printer(self) -> None:
        """Put the disc from the gripper into the printer."""
        self.require_gripper(True, "on the way to the printer")
        while self.printer_full():
            self._ask(Problem("disc_in_printer", "There is already a disc in the printer",
                              "Remove the disc from the printer tray and choose Retry.", device="printer"))
        self.move_above(E.PRINTER, "above the printer")
        self.require_gripper(True, "above the printer")
        self.open_drawer()
        self.require_gripper(True, "after opening the printer tray")
        clamp = 0
        while True:
            s = self.pm(E.PRINTER, A.PLACE)
            if s is None:
                break
            self.pm(E.PRINTER, A.MOVE)
            grip, full = self.gripper_full(), self.printer_full()
            if s.changer == S.CHANGER_PRINTER_FULL and not grip and full:
                self.log("printer full and gripper empty: the disc is in the printer")
                break
            if s.changer in (S.CHANGER_NO_DISC, S.CHANGER_CLAMP) or s.printer == S.PRINTER_CLAMP:
                if not grip and full:
                    self.pm(E.PRINTER, A.PICK)        # pick the disc back up
                if clamp < self.t.clamp_retries:
                    clamp += 1
                    self.log(f"clamp error in the printer, attempt {clamp} of {self.t.clamp_retries}")
                    continue
                self._ask(self._problem_from("clamp_error", "Clamp error in the printer", s,
                                             "The disc is not clamped properly. Check the disc and the "
                                             "printer tray, then choose Retry.", ("retry", "reject", "stop"),
                                             device="printer"))
                clamp = 0
                continue
            if s.changer == 9 and grip and not full:
                continue
            self._ask(self._problem_from("place_printer", "Disc not placed in the printer", s,
                                         "Check the printer tray and choose Retry.", ("retry", "reject", "stop"),
                                         device="printer"))
        self.close_drawer_checked()

    def unload_printer(self) -> None:
        """Pick the disc from the printer.

        No tray commands are sent here, only !f12602C. The tray only opens if picking fails
        and the printer seems empty.
        """
        self.require_gripper(False, "before picking from the printer")
        opened = False
        attempt = 0
        while True:
            s = self.pm(E.PRINTER, A.PICK)
            if s is None:
                break
            if not self.printer_full():
                if not opened:
                    self.open_drawer()
                    opened = True
                if not self.printer_full():
                    self._ask(self._problem_from("no_disc_printer", "No disc in the printer", s,
                                                 "The printer reports no disc. Check the printer tray and choose "
                                                 "Retry.", device="printer"))
                continue
            if s.changer == S.CHANGER_NO_DISC and attempt < 4:
                attempt += 1
                if attempt > 2:
                    self.log("picking from the printer failed, closing and reopening the printer tray")
                    self.drawer(False)
                    self.open_drawer()
                    opened = True
                self.sleep(3.0)
                continue
            self._ask(self._problem_from("pick_printer", "Picking from the printer failed", s,
                                         "Put the disc in the centre of the open printer tray and choose Retry.",
                                         device="printer"))
            attempt = 0
        if opened:
            self.drawer(False)

    # -- printer: wake, wait, recover -------------------------------------------
    def printer_awake(self) -> None:
        """Fan Control on (!e22C), then wait until the printer is no longer initializing or warming up."""
        self._simple(c.fan_control(True))
        self.wait_printer_ready("waking the printer")

    def printer_standby(self) -> None:
        self._simple(c.fan_control(False))

    def wait_printer_ready(self, what: str) -> c.Sense:
        """Wait while the printer initializes (5) or warms up (1); recover from errors."""
        start = time.monotonic()
        shown = None
        try:
            while True:
                s = self.sense()
                if S.changer_needs_door(s.changer):
                    self.wait_door_closed()
                    continue
                if s.printer in (S.PRINTER_INITIALIZING, S.PRINTER_WARMING):
                    if shown != s.printer:
                        shown = s.printer
                        self.notice(Problem("printer_waiting", "Printer is not ready yet",
                                            "The printer is " + ("initializing" if s.printer == 5 else "warming up")
                                            + ". After standby this can take a few minutes.", options=(),
                                            device="printer"))
                elif S.printer_is_error(s.printer):
                    self.recover_printer(s.printer, what)
                    start = time.monotonic()
                    continue
                else:
                    return s
                if time.monotonic() - start > self.t.printer_ready_s:
                    raise LoaderError(f"printer not ready after {self.t.printer_ready_s:.0f} s ({what}): "
                                      f"{S.printer_text(s.printer)}", s)
                self.sleep(self.t.poll_s)
        finally:
            if shown is not None:
                self.notice(None)

    def recover_printer(self, code: int, what: str) -> None:
        """Recover from a printer error.

        Afterwards the printer is ready again; a label that was on the film may be gone.
        """
        kind = S.printer_recovery(code)
        text = S.printer_text(code)
        self.log(f"printer error while {what}: {text} ({code})")
        if kind == "firmware":
            raise LoaderError("the printer is in firmware update mode; the job stops. "
                              "Switch the printer off and on and check it.")
        if kind == "reset" and self._auto_reset_code != code:
            self._auto_reset_code = code
            self.log(f"resetting the printer automatically (error {code})")
            self.reset_printer()
        elif kind == "button":
            self.notice(Problem("printer_button", "Printer reports an error", "Press the button on the printer.",
                                options=(), device="printer"))
            self._wait_while(lambda s: s.printer in S.PRINTER_BUTTON)
            self.notice(None)
        elif kind == "reinit":
            self.notice(Problem("printer_cover", text,
                                "Close the printer cover and lock everything. The job then continues "
                                "by itself.", options=(), device="printer"))
            self._wait_while(lambda s: s.printer in S.PRINTER_WAIT_REINIT)
            self.notice(None)
        elif kind == "supply":
            thing = "the ribbon" if code in (21, 22) else "the retransfer film"
            self._ask(Problem("printer_supply", text, f"Replace {thing} and choose Retry.", device="printer",
                              sense=c.Sense(0, code)))
        else:
            self._ask(Problem("printer_error", f"Printer error: {text}",
                              "Open and close the printer door (or switch the printer off and on), wait until it "
                              "is ready and choose Retry.", device="printer", sense=c.Sense(0, code)))
            self.reset_printer()
        # then it has to reinitialize; a new error is handled again
        self.wait_printer_ready(what)

    def reset_printer(self) -> None:
        """Soft reset (!f12606C); if that fails, a hard reset (!f12608C)."""
        if self._try(c.printer_reset(hard=False)) is not None:
            self._simple(c.printer_reset(hard=True))

    def skip_label(self) -> None:
        """Remove the label from the film (soft reset)."""
        self.log("removing the label from the film (soft reset of the printer)")
        self.reset_printer()
        self.wait_printer_ready("discarding the label")

    def wait_roller_warm(self, on_temp: Callable[[int | None], None] | None = None) -> None:
        """Everest 400: wait until the heat roller is warm enough (!f<0C).

        After that the autoloader itself waits until the roller reaches 165 degrees before it
        lets the printer do the retransfer.
        """
        start = time.monotonic()
        while True:
            cmd = c.everest_temperature()
            vals = c.parse_temperature(cmd, self.send(cmd))
            temp = vals[1] if len(vals) > 1 else None
            if on_temp:
                on_temp(temp)
            if temp is not None and temp >= self.t.roller_min_temp:
                return
            if time.monotonic() - start > self.t.roller_warm_s:
                raise LoaderError(f"heat roller does not warm up (last value {temp})")
            self.sleep(self.t.poll_s)

    def wait_label_on_film(self) -> None:
        """Wait until the printer reports 9 (label on the film, waiting for the disc).

        Sense 0 while the label is on its way gets 2 s and then 5 more polls; if it stays 0, the
        label is lost (252). A printer error is recovered and then the label has to be sent again.
        """
        start = time.monotonic()
        zero = 0
        while True:
            s = self.sense()
            if S.changer_needs_door(s.changer):
                self.wait_door_closed()
                continue
            if s.printer == S.PRINTER_WAITING_FOR_DISC:
                return
            if s.printer == 0:
                zero += 1
                if zero > 6:
                    raise LabelLost("printer is ready but there is no label on the film")
                self.sleep(2.0 if zero == 1 else 1.0)
                continue
            zero = 0
            if S.printer_is_error(s.printer):
                self.recover_printer(s.printer, "putting the label on the film")
                raise LabelLost("resend the label after a printer error")
            if time.monotonic() - start > self.t.preprint_s:
                raise LoaderError(f"label still not on the film after {self.t.preprint_s:.0f} s "
                                  f"(printer: {S.printer_text(s.printer)})", s)
            self.sleep(self.t.poll_s)

    def start_retransfer(self) -> None:
        """Wake the printer, then !f12604C. The printer must then leave state 9 within 5 s."""
        self.printer_awake()
        s = self._try(c.start_retransfer())
        if s is not None:
            if s.printer == 3:                    # retransfer already running: treat as success
                self.log("retransfer reported as failed, but the printer is busy (3): continuing")
            elif S.printer_is_error(s.printer):
                self.recover_printer(s.printer, "transferring")
                raise PrintFailed(f"retransfer failed: {S.printer_text(s.printer)}")
            else:
                raise PrintFailed(f"retransfer failed: {S.changer_text(s.changer)}")
        start = time.monotonic()
        while True:
            s = self.sense()
            if s.printer != S.PRINTER_WAITING_FOR_DISC:
                return
            if time.monotonic() - start > self.t.retransfer_start_s:
                raise PrintFailed("the printer did not start the retransfer (stays at 'label on film')")
            self.sleep(0.5)

    def wait_print_done(self) -> str:
        """Wait until the printer reports 4 (disc printed). Returns 'ok' or 'supply' (ribbon or film now out).

        Only 4 counts as done; 0 twice in a row is error 250; 21 or 23 right after the retransfer
        means the disc was printed.
        """
        start = time.monotonic()
        zero = 0
        while True:
            s = self.sense()
            if S.changer_needs_door(s.changer):
                self.wait_door_closed()
                continue
            if s.printer == S.PRINTER_DISC_PRESENT:
                self._auto_reset_code = None
                return "ok"
            if s.printer in (S.PRINTER_RIBBON_OUT, S.PRINTER_FILM_OUT):
                return "supply"
            if s.printer == 0:
                zero += 1
                if zero >= 2:
                    raise PrintFailed(S.printer_text(250))
                self.sleep(2.0)
                continue
            zero = 0
            if S.printer_is_error(s.printer):
                self.recover_printer(s.printer, "printing")
                raise PrintFailed(f"printing failed: {S.printer_text(s.printer)}")
            if time.monotonic() - start > self.t.print_done_s:
                raise PrintFailed(S.printer_text(251))
            self.sleep(self.t.poll_s)

    # -- identify recorders -------------------------------------------------------
    def locate_drive(self, burner) -> int | None:
        """Which element (241 or 242) belongs to this recorder?

        Arm to the top (240, 0), open only the (empty) tray of this recorder, then "pick" at 240
        (action 2). Error 3 (no disc) is expected; error 7 means no tray. Then Read Element Status
        240 gives a position that drive_from_position converts. The other trays must be closed;
        guard_trays takes care of that.
        """
        self.pm(E.DRIVE_ANY, A.MOVE)
        self.open_tray(burner)
        try:
            s = self.pm(E.DRIVE_ANY, A.PICK)
            if s is not None:
                if s.changer == 7:
                    self.log(f"{burner.device}: no open recorder tray found")
                    return None
                if s.changer not in (S.CHANGER_NO_DISC, 12):
                    raise LoaderError(f"locating the recorder failed: {S.changer_text(s.changer)}", s)
            if self.gripper_full():
                raise LoaderError("there was a disc in the open tray; remove it from the gripper and empty the tray")
            st = self.element(E.DRIVE_ANY)
            self.log(f"{burner.device}: position of the open tray {st.value}")
            if not st.ok:
                raise LoaderError("autoloader returned no position for the open tray")
            return drive_from_position(st.value)
        finally:
            self.pm(E.DRIVE_ANY, A.MOVE)          # arm back to the top before the tray closes
            self.close_tray(burner)

    # -- simple movements (for the CLI and tests) -------------------------------------
    def pick(self, element: int) -> None:
        s = self.pm(element, A.PICK)
        if s is not None:
            raise LoaderError(f"picking at {int(element)} failed: {S.changer_text(s.changer)}", s)

    def place(self, element: int) -> None:
        s = self.pm(element, A.PLACE)
        if s is not None:
            raise LoaderError(f"placing at {int(element)} failed: {S.changer_text(s.changer)}", s)

    def clear_after_reject(self, reject_bin: int) -> None:
        """After 'reject disc': move the disc from the gripper or printer to the reject bin, close the printer tray."""
        if not self.gripper_full() and self.printer_full():
            self.unload_printer()
        if self.gripper_full():
            self.put_in_bin(reject_bin)
        self.drawer(False)
