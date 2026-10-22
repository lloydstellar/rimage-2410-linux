"""Run one job: burn, print and sort.

Order per disc:
1. label: wake the printer (!e22C) and send the print job early, so the label is already on
   the retransfer film when the disc is ready ("pre-printing"); after that, each time the
   previous disc has been printed;
2. disc from the input bin, arm above the recorder, tray open (by the PC), disc in, tray closed;
3. burn and verify in a separate thread;
4. burn failed: disc goes straight to the reject bin, without a label (the label stays on the film);
5. burn succeeded: disc into the printer, give the recorder a new blank disc right away, heat
   the roller, retransfer, wait for "printed" (printer status 4 only), disc out;
6. disc into the output bin, or into the reject bin if printing failed.
If something goes wrong, the runner asks the operator what to do (Retry, Reject disc, Stop).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .burn import RAM_ROOT, BurnError, BurnPlan, cleanup_stale_ram_dirs, pick_burn_dir
from .hardware import DryRunBurner, DryRunLoader, DryRunPrinter, HardwareError, SimState
from .robot.commands import Element
from .progress import Tracker
from .robot.sequence import LabelLost, LoaderError, PrintFailed, Problem, RejectDisc, Robot, Stopped, Timing

BINS = {1: Element.BIN_1, 2: Element.BIN_2, 3: Element.BIN_3}
# Output: a bin, or the side bin (element 300, the mailslot). You can take discs out of the side
# bin without opening the door. It holds 5 discs.
OUTPUTS = {**BINS, "side": Element.MAILSLOT}
SIDE_BIN_CAPACITY = 5


def resolve_output(value, copies: int):
    """'auto' = side bin for up to SIDE_BIN_CAPACITY copies, otherwise bin 2."""
    if value in (None, "", "auto"):
        return "side" if copies <= SIDE_BIN_CAPACITY else 2
    if value == "side":
        return "side"
    return int(value)


@dataclass
class DiscJob:
    job_id: str
    copies: int = 1
    source_kind: str = "none"            # 'none', 'iso', 'audio', 'cue'
    source_paths: list[str] = field(default_factory=list)
    label_path: str | None = None        # print image (PNG, 1889 x 1889)
    ribbon: str = "auto"                 # 'auto' (whatever is in the printer), 'cmy', 'cmyw', 'mono'
    input_bin: int = 1
    output_bin: int | str = 2            # 1, 2, 3 or "side" (side bin)
    reject_bin: int | None = 3
    titles: list[str] = field(default_factory=list)
    album: str = ""
    artist: str = ""
    color_method: object = 0             # 0, 2 or 'simple' (see everest/color.py)
    parallel: bool = True                # two recorders at once when possible
    audio: dict = field(default_factory=dict)   # audio CD: pauses, gapless, dither, gain_db, isrc, upc, ...
    print_offset: tuple = (0.0, 0.0)     # alignment correction in mm (x right, y up), see alignment.py
    plane_shift: dict = field(default_factory=dict)   # lines down per colour plane, see alignment.py
    plane_gain: dict = field(default_factory=dict)    # gain per colour plane, see alignment.py

AUDIO_OPTIONS = ("performers", "pauses", "gapless", "dither", "gain_db", "isrc", "upc", "copy_permitted", "speed")


def build_print_job(label_path: str, ribbon: str, method=0, offset_mm=(0.0, 0.0),
                    plane_shift: dict | None = None, plane_gain: dict | None = None) -> bytes:
    """Turn a print image into an Everest print job using rimage2410.everest.

    offset_mm: alignment correction; plane_shift: lines down per colour plane; plane_gain:
    gain per colour plane (alignment.py)."""
    from PIL import Image
    from . import alignment
    from .everest import color, job
    img = alignment.shift(Image.open(label_path).convert("RGB"), *offset_mm)
    planes = color.convert(img, ribbon=ribbon, method=method)
    planes = alignment.shift_planes(alignment.gain_planes(planes, plane_gain or {}), plane_shift or {})
    # media byte in the header, no ESC!m2 and no end block (see _send_label)
    return job.build_job(planes, ribbon=ribbon, media_in_header=True, include_m2=False, include_end=False)


def check_printer(printer, job: DiscJob, log, tr: Tracker | None = None) -> str:
    """Read the ribbon and supplies from the Everest (ESC!r3C, ESC!r6C) and pick the ribbon for this job.

    'auto' uses whatever is in the printer. An explicit choice that does not match the
    installed ribbon stops the job, because the print would fail.
    """
    from .everest import job as ej
    try:
        st = ej.parse_r3(printer.query(ej.status_command("r3"), 13))
        cons = ej.parse_r6(printer.query(ej.status_command("r6"), 20))
    except Exception as e:  # noqa: BLE001
        if job.ribbon == "auto":
            raise HardwareError(f"cannot read the ribbon ({e}); choose the ribbon manually") from e
        log(f"warning: cannot read ribbon and supplies ({e}); using {job.ribbon}")
        return job.ribbon
    installed = ej.ribbon_from_status(st)
    pct = cons.ribbon_percent(installed)
    if tr is not None:
        tr.printer_info(ej.RIBBON_NAMES.get(installed or "", "no ribbon")
                        + (f", {cons.ribbon_remaining} prints left" if cons.ribbon_remaining is not None else "")
                        + (f", film {cons.film_percent}%" if cons.film_percent is not None else ""))
    log("printer: ribbon " + ej.RIBBON_NAMES.get(installed or "", "not installed or unknown")
        + (f", {cons.ribbon_remaining} prints left ({pct}%)" if cons.ribbon_remaining is not None else "")
        + (f", retransfer film {cons.film_percent}%" if cons.film_percent is not None else "")
        + (f" ({cons.film_value} prints left)" if cons.film_value is not None else ""))
    if installed is None:
        raise HardwareError("no (recognised) ribbon in the printer")
    if st.film in ("E",):
        raise HardwareError("no retransfer film in the printer")
    if job.ribbon not in ("auto", installed):
        raise HardwareError(f"the printer has a {ej.RIBBON_NAMES[installed]} ribbon, but the job "
                            f"asks for {ej.RIBBON_NAMES.get(job.ribbon, job.ribbon)}. Change the ribbon or choose Automatic.")
    if cons.ribbon_remaining is not None and cons.ribbon_remaining < job.copies:
        log(f"warning: the ribbon has {cons.ribbon_remaining} prints left, the job needs {job.copies}")
    if cons.film_percent is not None and cons.film_percent <= 5:
        log(f"warning: the retransfer film is almost used up ({cons.film_percent}%)")
    if cons.film_value is not None and cons.film_value < job.copies:
        log(f"warning: the retransfer film has {cons.film_value} prints left, the job needs {job.copies}")
    return installed


def _save_printjob_preview(print_bytes: bytes, workdir: str, log) -> None:
    """Approximate image of what goes to the printer after colour conversion (for the web app)."""
    try:
        from .everest import inspect
        img = inspect.preview(print_bytes)
        img.thumbnail((700, 700))
        img.save(os.path.join(workdir, "printjob-preview.png"))
    except Exception as e:  # noqa: BLE001 - only a helper image
        log(f"could not make the print job preview: {e}")


def split_m2(data: bytes) -> tuple[bytes | None, bytes]:
    """Split a leading ESC!m2 command (9 bytes) from the actual job."""
    if data.startswith(b"\x1b!m2"):
        return data[:9], data[9:]
    return None, data


def make_dry_run_backends(workdir: str, *, step_delay_s: float = 0.0, burn_s: float = 0.0,
                          log: Callable[[str], None] | None = None):
    """Dry-run versions of the loader, the printer and the two recorders, with shared state."""
    state = SimState(step_delay_s=step_delay_s)
    loader = DryRunLoader(state, log)
    printer = DryRunPrinter(state, workdir, log)
    burners = [DryRunBurner(state, Element.DRIVE_1, "/dev/sr0", burn_s),
               DryRunBurner(state, Element.DRIVE_2, "/dev/sr1", burn_s)]
    return loader, printer, burners


def make_hardware_backends(*, allow_motion: Callable[[bytes], bool], log: Callable[[str], None] | None = None):
    """The real machine: autoloader (HID), Everest 400 (usblp) and the recorders, found by
    serial number (hardware.find_recorders). allow_motion decides per motion command (motion_policy)."""
    from .burn import LocalBurner
    from .hardware import HidLoader, UsbPrinter, find_recorders
    loader = HidLoader(log=log, allow_motion=allow_motion)
    try:
        printer = UsbPrinter(log=log, allow_jobs=True)
    except Exception:
        loader.close()
        raise
    found = find_recorders()
    if not found:
        loader.close()
        printer.close()
        raise HardwareError("no recorders found (/dev/sr*)")
    burners = [LocalBurner(dev, el) for dev, el in found]
    return loader, printer, burners


def run_job(job: DiscJob, *, loader, printer, burners: list, log: Callable[[str], None],
            should_stop: Callable[[], bool] = lambda: False, timing: Timing | None = None,
            workdir: str | None = None, sleep=None, progress: Tracker | None = None,
            ask: Callable[[Problem], str] | None = None, ram_dir: str | None = RAM_ROOT) -> str:
    """Run the job; returns 'done', 'failed' or 'stopped'.

    progress: optional Tracker that records per step what the recorders, printer and arm are doing.
    ask: asks the operator for a choice (Retry, Reject disc, Stop) when something goes wrong.
    Without ask, the job stops on such an error.
    ram_dir: tmpfs for the converted audio (None = always on disk).
    """
    tr = progress or Tracker(job.copies)

    def ask_operator(p: Problem) -> str:
        tr.problem(p.as_dict(), waiting=True)
        try:
            return ask(p)
        finally:
            tr.problem(None)

    def notice(p: Problem | None) -> None:
        tr.problem(p.as_dict() if p else None, waiting=False)

    kwargs = {} if sleep is None else {"sleep": sleep}
    robot = Robot(loader, log, should_stop, timing, ask=ask_operator if ask else None, notice=notice, **kwargs)
    robot.burners = list(burners)      # watch all recorders, including one this job does not use
    workdir = workdir or os.path.join(os.path.expanduser("~/rimage-data"), "work", job.job_id)
    os.makedirs(workdir, exist_ok=True)

    plan = None
    burn_dir, in_ram = os.path.join(workdir, "burn"), False
    if job.source_kind != "none":
        cleanup_stale_ram_dirs(ram_dir)
        burn_dir, in_ram = pick_burn_dir(job.source_kind, list(job.source_paths), job.job_id, burn_dir,
                                         ram_dir, log)
        plan = BurnPlan(job.source_kind, list(job.source_paths), burn_dir,
                        titles=job.titles, album=job.album, artist=job.artist,
                        **{k: v for k, v in (job.audio or {}).items() if k in AUDIO_OPTIONS})
        for w in plan.validate():
            log(f"warning: {w}")

    if job.input_bin == job.output_bin:
        raise ValueError("input and output bin must be different")
    if job.output_bin == "side" and job.copies > SIDE_BIN_CAPACITY:
        log(f"warning: {job.copies} discs to the side bin, which holds {SIDE_BIN_CAPACITY}; "
            "when it is full, the job asks you to empty it")
    use_burners = burners[:2] if (plan is not None and job.parallel and len(burners) >= 2
                                  and job.copies > 1) else burners[:1]
    for b in (use_burners if plan is not None else []):
        tr.drive(b.device, "idle")
    ctx = _Ctx(robot=robot, job=job, plan=plan, printer=printer, burners=use_burners, log=log,
               should_stop=should_stop, tr=tr, in_el=BINS[job.input_bin], out_el=OUTPUTS[job.output_bin],
               rej_el=BINS.get(job.reject_bin) if job.reject_bin else None)
    try:
        return _run(ctx, tr, robot, printer, job, workdir, plan, use_burners)
    finally:
        if in_ram:
            if any(t.is_alive() for t in ctx.threads):
                log(f"keeping work folder {burn_dir} until the running burns have finished")
            else:
                shutil.rmtree(burn_dir, ignore_errors=True)


def _run(ctx, tr, robot, printer, job, workdir, plan, use_burners) -> str:
    log = ctx.log
    try:
        if job.label_path:
            tr.printer("checking ribbon")
            ribbon = check_printer(printer, job, log, tr)
            tr.printer("building print job")
            log(f"building print job from the label ({ribbon})")
            ctx.print_bytes = build_print_job(job.label_path, ribbon, job.color_method, job.print_offset, job.plane_shift,
                                              job.plane_gain)
            log(f"print job ready: {len(ctx.print_bytes)} bytes")
            _save_printjob_preview(ctx.print_bytes, workdir, log)
            tr.printer("idle")
        tr.arm("starting up: status, wake printer, arm to home")
        robot.startup()
        tr.phase("running")
        if plan is not None and len(use_burners) >= 2:
            plan.parallel = True
            _run_parallel(ctx)
        else:
            for n in range(1, job.copies + 1):
                log(f"--- disc {n} of {job.copies} ---")
                _one_disc(ctx, n)
        if ctx.film_for is not None:
            # a label is still on the film (for example after a burn failure): discard it
            tr.printer("removing label from the film")
            robot.skip_label()
            ctx.film_for = None
            tr.printer("idle")
        snap = tr.snapshot()
        log(f"job done: {snap['done']} good, {snap['rejected']} rejected")
        tr.phase("done")
        return "done"
    except Stopped as e:
        log(f"job stopped: {e}")
        _report_leftovers(ctx)
        tr.phase("stopped")
        return "stopped"
    except (LoaderError, HardwareError, BurnError) as e:
        log(f"error: {e}")
        _report_leftovers(ctx)
        tr.error(str(e))
        tr.phase("error")
        return "failed"


@dataclass
class _Ctx:
    robot: Robot
    job: DiscJob
    plan: BurnPlan | None
    printer: object
    burners: list
    log: Callable[[str], None]
    should_stop: Callable[[], bool]
    tr: Tracker
    in_el: int
    out_el: int
    rej_el: int | None
    print_bytes: bytes | None = None
    film_for: int | None = None        # disc the label on the film is for (-1: not decided yet)
    finished: int = 0                  # discs that are in a bin (good or rejected)
    busy: dict = field(default_factory=dict)   # recorder -> disc still inside it (for the final report)
    threads: list = field(default_factory=list)   # burn threads (remove the work folder only after they finish)

    def bin_no(self, el: int) -> int:
        return list(BINS.values()).index(el) + 1

    def place_name(self, el: int) -> str:
        """'bin 2' or 'the side bin'."""
        return "the side bin" if el == Element.MAILSLOT else f"bin {self.bin_no(el)}"

    @property
    def reject_el(self) -> int:
        return self.rej_el if self.rej_el is not None else self.out_el


def _report_leftovers(ctx: _Ctx) -> None:
    left = {d: n for d, n in ctx.busy.items() if n}
    if left:
        ctx.log("warning: there are still discs in the recorders: "
                + ", ".join(f"disc {n} in {d}" for d, n in left.items())
                + ". Running burns will finish.")


# ---- printer ----------------------------------------------------------------------

def _send_label(ctx: _Ctx, n: int | None) -> None:
    """Wake the printer and send the job; the printer puts the label on the retransfer film.

    The job has no ESC!m2 and no final block (form feed). The printer then stays at status 9
    until the autoloader starts the retransfer.
    n: the disc the label is for, or None if that is not known yet.
    """
    ctx.tr.printer("waking up", n)
    ctx.robot.printer_awake()
    m2, data = split_m2(ctx.print_bytes)
    if m2:
        ctx.log("warning: print job contains ESC!m2; leaving it out")
    ctx.log(f"sending print job {'for disc ' + str(n) if n else 'for the next disc'} to the Everest (label on the film)")
    ctx.tr.printer("putting label on the film", n)
    ctx.printer.send_job(data)
    ctx.film_for = n or -1


def _ensure_label(ctx: _Ctx, n: int) -> None:
    """Make sure the label for disc n is on the film; if it is lost, send it again (up to 2 times)."""
    lost = 0
    while True:
        if ctx.film_for is None:
            _send_label(ctx, n)
        try:
            ctx.robot.wait_label_on_film()
        except LabelLost as e:
            ctx.film_for = None
            lost += 1
            ctx.log(f"label lost ({e}), sending again")
            if lost > 2:
                ctx.robot._ask(Problem("label_lost", "Label does not reach the film",
                                       "The label failed to reach the retransfer film three times. Check the "
                                       "printer (ribbon, film, cover) and choose Retry.", device="printer"))
                lost = 0
            continue
        ctx.film_for = n
        ctx.tr.printer("label on the film", n)
        return


def _print_disc(ctx: _Ctx, n: int, preload: Callable[[], None] | None = None) -> bool:
    """Print disc n (in the gripper) and get it back into the gripper. True = printed correctly.

    preload: after placing the disc in the printer and before the transfer, load the next blank
    disc into the recorder that just became free, so it burns while the printer works.
    """
    _ensure_label(ctx, n)
    ctx.tr.disc(n, "to_printer", "gripper")
    ctx.tr.arm(f"placing disc {n} in the printer")
    ctx.robot.load_printer()
    ctx.tr.disc(n, "printing", "printer")
    ctx.tr.arm("")
    if preload:
        preload()
    ok = True
    try:
        ctx.tr.printer("heating roller", n)
        ctx.robot.wait_roller_warm(lambda t: ctx.tr.printer_info("" if t is None else f"roller {t} °C"))
        ctx.tr.printer("printing (retransfer)", n)
        ctx.robot.start_retransfer()
        result = ctx.robot.wait_print_done()
        ctx.film_for = None
        if result == "supply":
            ctx.log(f"disc {n} is printed, but the ribbon or film is now used up")
            ctx.robot._ask(Problem("supply_out", "Ribbon or retransfer film used up",
                                   f"Disc {n} is printed. Replace the ribbon or the film and choose Retry "
                                   "to continue.", device="printer"))
            ctx.robot.wait_printer_ready("after replacing ribbon or film")
    except PrintFailed as e:
        ctx.film_for = None
        ctx.log(f"disc {n}: printing failed: {e}")
        choice = ctx.robot._ask(Problem("print_failed", "Printing failed",
                                        f"{e}. Look at disc {n}: is the label printed correctly after all?",
                                        ("yes", "no"), device="printer"))
        ok = choice == "yes"
    ctx.log(f"disc {n}: {'label printed' if ok else 'label failed, disc goes to the reject bin'}")
    # as soon as the printer reports 4, send the next label to the printer
    if ctx.finished + 1 < ctx.job.copies and ctx.film_for is None:
        _send_label(ctx, None)
    ctx.tr.arm(f"taking disc {n} out of the printer")
    ctx.robot.unload_printer()
    ctx.tr.printer("idle" if ctx.film_for is None else "label on the film")
    ctx.tr.disc(n, "printed", "gripper")
    return ok


# ---- arm ----------------------------------------------------------------------------

def _put_away(ctx: _Ctx, n: int, ok: bool, reason: str = "") -> None:
    target = ctx.out_el if ok else ctx.reject_el
    ctx.tr.disc(n, "storing", "gripper")
    ctx.tr.arm(f"placing disc {n} in {ctx.place_name(target)}")
    ctx.robot.put_in_bin(target)
    ctx.finished += 1
    ctx.tr.disc(n, "done" if ok else "rejected", ctx.place_name(target))
    ctx.tr.arm("")
    ctx.log(f"disc {n} in {ctx.place_name(target)}" + (f" ({reason})" if reason else ""))


def _reject_now(ctx: _Ctx, n: int, why: str) -> None:
    """The operator chose 'Reject disc': move the disc from the gripper or printer to the reject bin."""
    ctx.log(f"disc {n} rejected by the operator: {why}")
    target = ctx.reject_el
    ctx.tr.arm(f"placing disc {n} in {ctx.place_name(target)}")
    ctx.robot.clear_after_reject(target)
    ctx.finished += 1
    ctx.tr.disc(n, "rejected", ctx.place_name(target))
    ctx.tr.arm("")


def _load_drive(ctx: _Ctx, n: int, burner) -> None:
    ctx.tr.disc(n, "loading", "gripper", burner.device)
    ctx.tr.drive(burner.device, "loading", n)
    ctx.tr.arm(f"placing disc {n} from bin {ctx.bin_no(ctx.in_el)} in {burner.device}")
    ctx.robot.load_drive(burner, ctx.in_el)
    ctx.busy[burner.device] = n
    ctx.tr.arm("")
    ctx.tr.disc(n, "burning", burner.device)
    ctx.tr.drive(burner.device, "burning", n)


def _unload_drive(ctx: _Ctx, n: int, burner, ok: bool, to_printer: bool) -> None:
    ctx.tr.drive(burner.device, "unloading", n)
    ctx.tr.disc(n, "unloading", burner.device)
    ctx.tr.arm(f"taking disc {n} out of {burner.device}")
    ctx.robot.unload_drive(burner, to_printer)
    ctx.busy[burner.device] = None
    ctx.tr.drive(burner.device, "idle")
    ctx.tr.disc(n, "burned" if ok else "burn_failed", "gripper")
    ctx.tr.arm("")


def _finish_disc(ctx: _Ctx, n: int, ok: bool, burner, preload: Callable[[], None] | None) -> None:
    """Handle a burned disc: good and printing -> printer (with preload), good without
    printing -> output bin, failed -> straight to the reject bin, without a label."""
    printing = ctx.print_bytes is not None
    try:
        if ok and printing:
            _unload_drive(ctx, n, burner, ok, to_printer=True)
            printed = _print_disc(ctx, n, preload)
            preload = None
            _put_away(ctx, n, printed, "" if printed else "label failed")
        else:
            _unload_drive(ctx, n, burner, ok, to_printer=False)
            _put_away(ctx, n, ok, "" if ok else "burn failed")
            if not ok and printing and ctx.film_for is not None:
                ctx.log("the label on the film is kept for the next disc")
    except RejectDisc as e:
        _reject_now(ctx, n, str(e))
    if preload:
        preload()


# ---- scheduling ---------------------------------------------------------------------

def _run_parallel(ctx: _Ctx) -> None:
    """Two recorders at once.

    The arm does one thing at a time (this thread); each recorder burns in its own thread. A
    recorder that has finished is unloaded. Once the burned disc is in the printer, that same
    recorder gets a new blank disc right away, before printing starts. Log lines from the burn
    threads are collected and written by this thread.
    """
    job, log = ctx.job, ctx.log
    slots = [{"burner": b, "thread": None, "ok": None, "n": None, "lines": []} for b in ctx.burners]
    state = {"next": 1}
    if ctx.print_bytes is not None:
        _send_label(ctx, None)

    def burn_thread(slot):
        lines = slot["lines"]
        try:
            slot["burner"].burn(ctx.plan, lines.append)
            slot["ok"] = True
        except Exception as e:  # noqa: BLE001 - any burn error means reject
            lines.append(f"burn failed: {e}")
            slot["ok"] = False
        ctx.tr.drive(slot["burner"].device, "done, waiting for the arm" if slot["ok"] else "failed, waiting for the arm",
                     slot["n"])
        ctx.tr.disc(slot["n"], "burned" if slot["ok"] else "burn_failed", slot["burner"].device)

    def start(slot) -> None:
        n = state["next"]
        log(f"--- disc {n} of {job.copies} to {slot['burner'].device} ---")
        _load_drive(ctx, n, slot["burner"])
        state["next"] += 1
        slot["n"], slot["ok"] = n, None
        slot["thread"] = threading.Thread(target=burn_thread, args=(slot,), daemon=True,
                                          name=f"burn-{slot['burner'].device}")
        slot["thread"].start()
        ctx.threads.append(slot["thread"])

    while ctx.finished < job.copies:
        progressed = False
        for s in slots:                          # burn finished: unload, print, store
            t = s["thread"]
            if t is not None and not t.is_alive():
                t.join()
                for line in s["lines"]:
                    log(f"[{s['burner'].device}] {line}")
                s["lines"].clear()
                n, ok, b = s["n"], bool(s["ok"]), s["burner"]
                s["thread"] = None
                log(f"disc {n}: burn on {b.device} {'succeeded' if ok else 'failed'}")
                preload = (lambda slot=s: start(slot)) if state["next"] <= job.copies else None
                _finish_disc(ctx, n, ok, b, preload)
                progressed = True
        for s in slots:                          # empty recorder: load a new disc and burn
            if s["thread"] is None and state["next"] <= job.copies:
                start(s)
                progressed = True
        if not progressed:
            if ctx.should_stop():
                raise Stopped("stopped by the user")
            time.sleep(0.2)


def _one_disc(ctx: _Ctx, n: int) -> None:
    """One disc at a time: burn (if there is a source), print, store."""
    if ctx.plan is not None:
        burner = ctx.burners[0]
        log = ctx.log
        _load_drive(ctx, n, burner)
        if ctx.print_bytes is not None and ctx.film_for is None:
            # put the label on the film now, so it is ready when the burn finishes;
            # otherwise the arm waits with the disc in the gripper until the label is on the film
            _send_label(ctx, n)
        try:
            burner.burn(ctx.plan, log)
            log(f"burn on {burner.device} succeeded")
            ok = True
        except (BurnError, HardwareError) as e:
            log(f"burn failed: {e}")
            ok = False
        _finish_disc(ctx, n, ok, burner, None)
        return
    # print only
    try:
        ctx.tr.disc(n, "loading", "gripper")
        ctx.tr.arm(f"picking disc {n} from bin {ctx.bin_no(ctx.in_el)}")
        ctx.robot.pick_from_bin(ctx.in_el)
        ctx.tr.disc(n, "burned", "gripper")
        if ctx.print_bytes is None:
            _put_away(ctx, n, True)
            return
        printed = _print_disc(ctx, n)
        _put_away(ctx, n, printed, "" if printed else "label failed")
    except RejectDisc as e:
        _reject_now(ctx, n, str(e))
