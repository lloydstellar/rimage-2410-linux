"""Progress of a batch: what each recorder, the printer and the arm are doing, and where each disc is.

The runner reports every step to a Tracker. The Tracker keeps a snapshot and passes it to a
callback (the web app). It is thread-safe, because the burn threads report too.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Callable

# stages of a disc, in order
STAGES = {
    "waiting": "Still in the input bin",
    "loading": "Arm is taking it to the recorder",
    "burning": "Being burned",
    "burned": "Burned, waiting for the arm",
    "burn_failed": "Burn failed",
    "unloading": "Arm is taking it out of the recorder",
    "to_printer": "Arm is placing it in the printer",
    "printing": "Being printed",
    "printed": "Printed",
    "storing": "Arm is placing it in the output bin",
    "done": "Done",
    "rejected": "Rejected",
}


class Tracker:
    def __init__(self, total: int, devices: list[str] | None = None,
                 on_change: Callable[[dict], None] | None = None,
                 burning: bool = True, printing: bool = False):
        self._lock = threading.Lock()
        self._on_change = on_change
        now = time.time()
        self._s = {
            "phase": "preparing",
            "total": total,
            "done": 0,
            "rejected": 0,
            "started": now,
            "updated": now,
            "burning": burning,
            "printing": printing,
            "arm": "",
            "problem": None,       # notice or question for the operator (Problem.as_dict + waiting, since)
            "error": "",           # why the job stopped
            "printer": {"state": "idle", "disc": None, "since": now, "info": ""},
            "drives": {d: {"state": "idle", "disc": None, "since": now} for d in (devices or [])},
            "discs": [{"n": i + 1, "stage": "waiting", "where": "input bin", "since": now, "drive": None}
                      for i in range(total)],
        }

    # -- internal -------------------------------------------------------------
    def _emit(self) -> None:
        self._s["updated"] = time.time()
        snap = copy.deepcopy(self._s)
        if self._on_change:
            try:
                self._on_change(snap)
            except Exception:  # noqa: BLE001 - progress reporting must never fail a job
                pass

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._s)

    # -- reports ------------------------------------------------------------
    def phase(self, phase: str) -> None:
        with self._lock:
            self._s["phase"] = phase
            if phase in ("done", "stopped", "error"):
                self._s["arm"] = ""
                if not (self._s["problem"] or {}).get("waiting"):
                    self._s["problem"] = None
            self._emit()

    def arm(self, text: str) -> None:
        with self._lock:
            self._s["arm"] = text
            self._emit()

    def disc(self, n: int, stage: str, where: str | None = None, drive: str | None = None) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}")
        with self._lock:
            d = self._s["discs"][n - 1]
            d["stage"], d["since"] = stage, time.time()
            if where is not None:
                d["where"] = where
            if drive is not None:
                d["drive"] = drive
            if stage == "done":
                self._s["done"] += 1
            elif stage == "rejected":
                self._s["rejected"] += 1
            self._emit()

    def drive(self, device: str, state: str, disc: int | None = None) -> None:
        with self._lock:
            dr = self._s["drives"].setdefault(device, {})
            dr.update(state=state, disc=disc, since=time.time())
            self._emit()

    def printer(self, state: str, disc: int | None = None, info: str = "") -> None:
        with self._lock:
            self._s["printer"].update(state=state, disc=disc, since=time.time(), info=info)
            self._emit()

    def printer_info(self, info: str) -> None:
        with self._lock:
            self._s["printer"]["info"] = info
            self._emit()

    def problem(self, p: dict | None, waiting: bool = False) -> None:
        """Notice (waiting=False, no choice) or question for the operator (waiting=True); None clears it."""
        with self._lock:
            self._s["problem"] = None if p is None else {**p, "waiting": waiting, "since": time.time()}
            self._emit()

    def error(self, text: str) -> None:
        with self._lock:
            self._s["error"] = text
            self._emit()
