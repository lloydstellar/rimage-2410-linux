"""Background worker: runs jobs one at a time through runner.run_job.

Two modes:
- dry run (default): runner.make_dry_run_backends. The simulated machine (bins, printer)
  keeps its state from one job to the next.
- real machine (hardware=True, command-line option --hardware): runner.make_hardware_backends.
  Starting a job counts as approval for moves that are known to work; new commands are shown
  to the operator first (motion_policy.py). Every HID and printer packet goes to
  <data dir>/logs/hid-<date>.log.
"""

from __future__ import annotations

import os
import threading
import time
import traceback

import numpy as np
from PIL import Image

from .. import alignment, runner
from ..motion_policy import MotionPolicy
from ..progress import Tracker
from ..everest import job as ejob
from ..geometry import LINE_PX
from ..robot.sequence import Timing



def simple_print_job(label_path: str, ribbon: str) -> bytes:
    """Fallback: build the planes by simple inversion when the real colour conversion is missing."""
    rgb = np.asarray(Image.open(label_path).convert("RGB"), dtype=np.uint8)
    h, w, _ = rgb.shape
    pad = (LINE_PX - w) // 2

    def plane(a: np.ndarray) -> np.ndarray:
        out = np.zeros((h, LINE_PX), np.uint8)
        out[:, pad:pad + w] = a
        return out

    inv = 255 - rgb
    if ribbon == "mono":
        lum = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.uint8)
        planes = {0: plane(255 - lum)}
    else:
        planes = {1: plane(inv[..., 0]), 2: plane(inv[..., 1]), 3: plane(inv[..., 2])}
        if ribbon == "cmyw":
            white = np.where(rgb.min(axis=2) < 250, 255, 0).astype(np.uint8)
            planes[4] = plane(white)
    return ejob.build_job(planes, ribbon=ribbon)


class Worker:
    def __init__(self, store, data_dir: str, *, step_delay_s: float = 0.3, burn_s: float = 3.0,
                 print_fallback: bool = True, fast: bool = False, ram_dir: str | None = "/dev/shm",
                 hardware: bool = False, hw_backends=None):
        self.store = store
        self.data_dir = data_dir
        self.hardware = hardware
        self.print_fallback = print_fallback and not hardware   # never send a fallback job to the real printer
        self.fast = fast
        self.ram_dir = ram_dir
        self._current_job: str | None = None
        self._progress: dict[str, dict] = {}     # job_id -> latest progress (running job only)
        self._stop_flags: dict[str, threading.Event] = {}
        self._questions: dict[str, dict] = {}    # job_id -> open question to the operator
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status_cache: tuple[float, dict] | None = None
        if hardware:
            os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
            self.policy = MotionPolicy(data_dir, log=self._policy_log)
            self._factory = hw_backends or runner.make_hardware_backends
            self.loader = self.printer = None
            self.burners = []
            self.hw_error: str | None = None
            self.state = None
            self.connect()          # if the 2410 is still off, retry later (on status or job)
        else:
            self.policy = None
            self.loader, self.printer, self.burners = runner.make_dry_run_backends(
                os.path.join(data_dir, "dryrun"), step_delay_s=step_delay_s, burn_s=burn_s,
                log=self._hw_log)
            self.state = self.loader.state
            self.state.roller_temp = 25      # the simulation starts with a cold printer so the warm-up is visible
        self._orig_build = runner.build_print_job
        runner.build_print_job = self._build_print_job

    # -- logging ------------------------------------------------------------
    def _log(self, line: str) -> None:
        if self._current_job:
            self.store.append_log(self._current_job, line)

    def connect(self) -> bool:
        """Real machine: connect if not connected yet. Returns True if the machine is available."""
        if not self.hardware or self.loader is not None:
            return True
        try:
            self.loader, self.printer, self.burners = self._factory(allow_motion=self.policy,
                                                                    log=self._packet_log)
            self.hw_error = None
            self._packet_log("connected to the 2410: " + ", ".join(f"{b.device}={b.element}" for b in self.burners))
            return True
        except Exception as e:  # noqa: BLE001 - machine off or not connected
            self.hw_error = f"{type(e).__name__}: {e}"
            return False

    def _hw_log(self, line: str) -> None:
        self._log("  " + line)

    def _packet_log(self, line: str) -> None:
        """Real machine: every packet goes to the daily log in the data directory (not the job log)."""
        path = os.path.join(self.data_dir, "logs", f"hid-{time.strftime('%Y-%m-%d')}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")

    def _policy_log(self, line: str) -> None:
        self._packet_log("approval: " + line)
        self._log("approval: " + line)

    # -- print job with fallback --------------------------------------------
    def _build_print_job(self, label_path: str, ribbon: str, method=0, offset_mm=(0.0, 0.0),
                         plane_shift=None, plane_gain=None) -> bytes:
        try:
            data = self._orig_build(label_path, ribbon, method, offset_mm, plane_shift, plane_gain)
        except Exception as e:  # noqa: BLE001 - the colour conversion is still in development
            if not self.print_fallback:
                raise
            self._log(f"WARNING: colour conversion not available ({type(e).__name__}: {e}); "
                      "dry run uses the simple conversion (inversion only)")
            data = simple_print_job(label_path, ribbon)
        return data

    # -- control --------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="rimage-worker", daemon=True)
            self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop_job(self, job_id: str) -> bool:
        with self._lock:
            ev = self._stop_flags.get(job_id)
        if ev:
            ev.set()
            return True
        job = self.store.get(job_id)
        if job and job["status"] == "queued":
            self.store.set_status(job_id, "stopped")
            return True
        return False

    @property
    def current_job(self) -> str | None:
        return self._current_job

    def _make_ask(self, job_id: str, stop: threading.Event):
        """Ask the operator: wait until the web interface answers or the job is stopped."""
        def ask(problem) -> str:
            slot = {"event": threading.Event(), "choice": None, "options": list(problem.options)}
            with self._lock:
                self._questions[job_id] = slot
            self._log(f"waiting for the operator: {problem.title}")
            try:
                while not slot["event"].wait(0.5):
                    if stop.is_set():
                        return "stop"
                return slot["choice"]
            finally:
                with self._lock:
                    self._questions.pop(job_id, None)
        return ask

    def answer(self, job_id: str, choice: str) -> str | None:
        """Operator answer to the open question. Returns an error message or None."""
        with self._lock:
            slot = self._questions.get(job_id)
        if slot is None:
            return "there is no open question"
        if choice not in slot["options"]:
            return "invalid choice"
        slot["choice"] = choice
        slot["event"].set()
        return None

    def progress(self, job_id: str) -> dict | None:
        """Live progress of a running job (or None)."""
        return self._progress.get(job_id)

    def reset_simulation(self) -> None:
        if self.state is None:
            raise RuntimeError("dry run only")
        with self.state.lock:
            self.state.bins = {200: 25, 201: 0, 202: 0}
            self.state.gripper = False
            for d in self.state.drives.values():
                d["tray_open"] = False
                d["disc"] = False
            self.state.drawer_open = False
            self.state.printer_disc = False
            self.state.printer_sense = 0
            self.state.changer_sense = 0
            self.state.label_on_film = False

    def _clear_transient(self) -> None:
        """After an error or stop in the dry run: act as if the operator empties the machine
        (disc out of the gripper, recorders and printer). The bins stay as they are."""
        if self.state is None:
            return
        with self.state.lock:
            self.state.gripper = False
            for d in self.state.drives.values():
                d["tray_open"] = False
                d["disc"] = False
            self.state.drawer_open = False
            self.state.printer_disc = False
            self.state.label_on_film = False
            self.state.printer_sense = 0
            self.state.changer_sense = 0

    # -- running ------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            job = self.store.next_queued()
            if job is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            self.run_one(job)

    def run_one(self, job: dict) -> str:
        job_id = job["id"]
        p = job["params"]
        stop = threading.Event()
        with self._lock:
            self._stop_flags[job_id] = stop
        self._current_job = job_id
        self.store.set_status(job_id, "running")
        if self.hardware and not self.connect():
            self._log(f"machine not found: {self.hw_error}. Is the 2410 switched on and the USB cable connected?")
            self.store.set_status(job_id, "failed")
            self._current_job = None
            with self._lock:
                self._stop_flags.pop(job_id, None)
            return "failed"
        if self.hardware:
            self._log("job started on the real machine (starting counts as approval for moves that are known to work)")
        else:
            self._log("job started (dry run: nothing is sent to the machine)")
        try:
            dj = runner.DiscJob(
                job_id=job_id, copies=int(p.get("copies", 1)),
                source_kind=p.get("source_kind", "none"), source_paths=p.get("source_paths", []),
                label_path=p.get("label_path"), ribbon=p.get("ribbon", "auto"),
                input_bin=int(p.get("input_bin", 1)),
                output_bin=runner.resolve_output(p.get("output_bin", 2), int(p.get("copies", 1))),
                reject_bin=p.get("reject_bin"), titles=p.get("titles", []),
                album=p.get("album", ""), artist=p.get("artist", ""),
                color_method=p.get("color_method", 0), audio=p.get("audio", {}),
                print_offset=alignment.load(self.data_dir),
                plane_shift=alignment.load_plane_shift(self.data_dir),
                plane_gain=alignment.load_plane_gain(self.data_dir))
            burning = dj.source_kind != "none"
            n_drives = (2 if (dj.parallel and dj.copies > 1) else 1) if burning else 0
            tracker = Tracker(dj.copies, devices=[b.device for b in self.burners[:n_drives]],
                              on_change=lambda snap: self._progress.__setitem__(job_id, snap),
                              burning=burning, printing=bool(dj.label_path))
            self._progress[job_id] = tracker.snapshot()
            timing = Timing(poll_s=0.0) if self.fast else Timing(poll_s=0.5)
            kwargs = {"sleep": (lambda _s: None)} if self.fast else {}
            ask = self._make_ask(job_id, stop)
            if self.policy is not None:
                def policy_ask(problem, _ask=ask, _tr=tracker, _stop=stop):
                    _tr.problem(problem.as_dict(), waiting=True)
                    try:
                        choice = _ask(problem)
                    finally:
                        _tr.problem(None)
                    if choice != "allow":
                        _stop.set()          # not allowed = stop the job, no second question
                    return choice
                self.policy.ask = policy_ask
                self.policy.active = True
            result = runner.run_job(dj, loader=self.loader, printer=self.printer, burners=self.burners,
                                    log=self._log, should_stop=stop.is_set, timing=timing,
                                    workdir=os.path.join(self.data_dir, "work", job_id),
                                    progress=tracker, ask=ask, ram_dir=self.ram_dir,
                                    **kwargs)
        except Exception as e:  # noqa: BLE001
            self._log(f"unexpected error: {type(e).__name__}: {e}")
            self._log(traceback.format_exc(limit=3))
            result = "failed"
        finally:
            if self.policy is not None:
                self.policy.active = False
                self.policy.ask = None
            self._status_cache = None
        if result != "done":
            self._clear_transient()
        snap = self._progress.pop(job_id, None)
        if snap is not None:
            self.store.set_progress(job_id, snap)
        self.store.set_status(job_id, result)
        self._log(f"result: {result}")
        self._current_job = None
        with self._lock:
            self._stop_flags.pop(job_id, None)
        return result


    # -- status of the real machine ---------------------------------------------
    def hw_status(self, max_age_s: float = 5.0) -> dict:
        """Read only, and only while no job is running: during a job an extra Request Sense
        could clear the error code that the job itself is waiting for."""
        if self.current_job:
            return {"busy": True}
        if not self.connect():
            return {"busy": False, "connected": False, "error": f"machine not found: {self.hw_error}"}
        now = time.monotonic()
        if self._status_cache and now - self._status_cache[0] < max_age_s:
            return self._status_cache[1]
        from ..robot import sense as S
        from ..robot.commands import Element, everest_temperature, parse_temperature
        from ..robot.sequence import Robot
        out: dict = {"busy": False, "connected": True}
        try:
            r = Robot(self.loader, lambda _m: None)
            s = r.sense()
            out.update(changer_sense=s.changer, changer_text=S.changer_text(s.changer),
                       printer_sense=s.printer, printer_text=S.printer_text(s.printer))
            bins = {}
            for n, el in ((1, Element.BIN_1), (2, Element.BIN_2), (3, Element.BIN_3)):
                st = r.element(el)
                bins[str(n)] = None if st.value == 65534 else (st.value if st.occupied else 0)
            out["bins"] = bins
            out["gripper"] = r.element(Element.GRIPPER).occupied
            out["printer_disc"] = r.element(Element.PRINTER).occupied
            cmd = everest_temperature()
            vals = parse_temperature(cmd, self.loader.transact(cmd.data, cmd.expect, 5))
            out["roller_temp"] = vals[1] if len(vals) > 1 else None
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        drives = {}
        for b in self.burners:
            try:
                drives[str(b.element)] = {"device": b.device, "tray_open": b.tray_open()}
            except Exception as e:  # noqa: BLE001
                drives[str(b.element)] = {"device": b.device, "error": str(e)}
        out["drives"] = drives
        self._status_cache = (now, out)
        return out
