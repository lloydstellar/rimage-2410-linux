"""When the web app may let the real machine move.

- Starting a job in the web app counts as consent for the motion commands of that job, but only
  for commands that are known to work (TESTED) or that the operator allowed earlier in the web app.
- A command that has never been used on the machine is first shown to the operator (buttons
  Allow and Stop job). Allowed commands are stored in <data dir>/approved_commands.json.
- Outside a running job nothing moves: the policy then lets only read commands through.
- The block list (robot/commands.check_allowed) always applies on top of this and wins.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Callable

from .robot.sequence import Problem

FILE = "approved_commands.json"

# Commands verified on a Professional 3410 with autoloader firmware 6.033A
# (response !f11C, !f21C or !e21C).
TESTED = frozenset(b"\x1b" + c.encode() for c in (
    "!e22C",                                                        # wake the printer
    "!f12000C", "!f12001C", "!f12002C", "!f12010C", "!f12011C", "!f12012C",     # bins
    "!f12020C", "!f12021C", "!f12022C",
    "!f12410C", "!f12411C", "!f12412C", "!f12420C",                 # recorders
    "!f12600C", "!f12601C", "!f12602C", "!f12604C",                 # printer: arm, place, pick, retransfer
    "!f22600C", "!f22601C",                                         # printer tray close and open
    "!f13000C", "!f13001C",                                         # side bin: move there, place
))

# What a command does, for the question to the operator.
_ACTIONS = {"0": "move the arm to", "1": "place a disc in", "2": "pick a disc from", "4": "transfer the label in",
            "6": "soft reset", "7": "transfer the label (asynchronous) in", "8": "hard reset"}
_ELEMENTS = {"200": "bin 1", "201": "bin 2", "202": "bin 3", "240": "the recorders", "241": "the upper recorder",
             "242": "the lower recorder", "260": "the printer", "300": "the side bin"}


def describe(cmd: bytes) -> str:
    body = cmd.lstrip(b"\x1b").decode("ascii", "replace")
    if body.startswith("!f1") and len(body) == 8:
        return f"{_ACTIONS.get(body[6], 'run action ' + body[6] + ' at')} {_ELEMENTS.get(body[3:6], 'element ' + body[3:6])}"
    if body.startswith("!f2") and len(body) == 8:
        target = "the printer tray" if body[3:6] == "260" else _ELEMENTS.get(body[3:6], "element " + body[3:6])
        return f"{'open' if body[6] == '1' else 'close'} {target}"
    if body == "!e20C":
        return "put the printer in standby"
    return "perform an unknown action"


class MotionPolicy:
    """allow_motion for HidLoader. active: a job is running; ask: question to the operator."""

    def __init__(self, data_dir: str, log: Callable[[str], None] | None = None):
        self.path = os.path.join(data_dir, FILE)
        self.log = log or (lambda _m: None)
        self.active = False
        self.ask: Callable[[Problem], str] | None = None
        self._lock = threading.Lock()
        self.approved = self._load()

    def _load(self) -> set[bytes]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return {b"\x1b" + c.encode() for c in json.load(f).get("commands", [])}
        except FileNotFoundError:
            return set()

    def _save(self) -> None:
        cmds = sorted(c.lstrip(b"\x1b").decode() for c in self.approved)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"commands": cmds}, f, indent=2)
        os.replace(tmp, self.path)

    def known(self, cmd: bytes) -> bool:
        return cmd in TESTED or cmd in self.approved

    def __call__(self, cmd: bytes) -> bool:
        if not self.active:
            self.log(f"refused (no job running): {cmd!r}")
            return False
        if self.known(cmd):
            return True
        ask = self.ask
        if ask is None:
            self.log(f"refused (never used before, nobody to ask): {cmd!r}")
            return False
        text = cmd.lstrip(b"\x1b").decode("ascii", "replace")
        choice = ask(Problem(
            "new_command", "New action for the machine",
            f"The job wants to {describe(cmd)} ({text}). This command has never been used on this machine. "
            "Check that the door is closed and nothing is in the way. Allow also applies to later runs.",
            options=("allow", "stop")))
        if choice != "allow":
            self.log(f"not allowed by the operator: {text}")
            return False
        with self._lock:
            self.approved.add(cmd)
            self._save()
        self.log(f"allowed by the operator and saved: {text}")
        return True
