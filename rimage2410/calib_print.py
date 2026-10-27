"""Calibration prints from the command line, through the same print sequence as the web app (runner.run_job).

    python -m rimage2410.calib_print ramp-mono --out side          # black ramp disc to the side bin
    python -m rimage2410.calib_print grey --out 3                  # grey ladder to bin 3
    python -m rimage2410.calib_print align-mono --out 3 --dry-run  # black alignment label, no machine
    python -m rimage2410.calib_print gain-mono --out side          # black gain x1.0 to x1.3
    python -m rimage2410.calib_print image --image label.png --ribbon mono --out side   # your own label

One disc from bin 1 (or --in), print only. The alignment (and for colour the gain and shift per
plane) comes from printer.json in the data directory (alignment.py).

Real machine: only moves that motion_policy already knows (tested, standard or approved before) are
sent; anything else is refused, as there is nobody to ask. If something goes wrong, the job stops.
Every HID and printer packet is written to <data dir>/logs/hid-<date>.log.
Close the door and make sure nothing is in the way before you start.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import alignment, calibration, runner
from .everest import job as ej
from .motion_policy import MotionPolicy

LABELS = ("ramp-mono", "grey", "align-mono", "align", "gain-mono", "image")
GAINS_MONO = (1.0, 1.1, 1.2, 1.3)


def build(label: str, data_dir: str, workdir: str, image: str | None = None,
          ribbon: str = "mono") -> tuple[bytes, str]:
    """Print job and ribbon for a calibration label (or your own label: 'image')."""
    offset = alignment.load(data_dir)
    if label == "image":
        if not image:
            raise ValueError("label 'image' needs --image")
        return runner.build_print_job(image, ribbon, 0, offset, alignment.load_plane_shift(data_dir),
                                      alignment.load_plane_gain(data_dir)), ribbon
    if label == "ramp-mono":
        planes, preview = calibration.ramp_planes_mono(offset_mm=offset)
        preview.save(os.path.join(workdir, "label.png"))
        return ej.build_job(planes, ribbon="mono", media_in_header=True, include_m2=False, include_end=False), "mono"
    if label == "gain-mono":
        planes, img = calibration.gain_test(gains=GAINS_MONO, plane=0, method=0, offset_mm=offset)
        img.save(os.path.join(workdir, "label.png"))
        return ej.build_job(planes, ribbon="mono", media_in_header=True, include_m2=False, include_end=False), "mono"
    if label == "grey":
        img, ribbon = calibration.grey_label(), "mono"
    elif label == "align-mono":
        img, ribbon = calibration.alignment_label(mono=True), "mono"
    elif label == "align":
        img, ribbon = calibration.alignment_label(), "cmy"
    else:
        raise ValueError(f"unknown label {label!r}")
    path = os.path.join(workdir, "label.png")
    img.save(path)
    return runner.build_print_job(path, ribbon, 0, offset, alignment.load_plane_shift(data_dir),
                                  alignment.load_plane_gain(data_dir)), ribbon


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("label", choices=LABELS)
    ap.add_argument("--in", dest="input_bin", type=int, default=1, choices=(1, 2, 3))
    ap.add_argument("--out", default="side", help="1, 2, 3 or side (side bin)")
    ap.add_argument("--image", help="PNG of 1889 x 1889 for label 'image'")
    ap.add_argument("--ribbon", default="mono", choices=("mono", "cmy"), help="ribbon for label 'image'")
    ap.add_argument("--data-dir", default=os.path.expanduser("~/rimage-data"))
    ap.add_argument("--dry-run", action="store_true", help="simulated machine")
    args = ap.parse_args(argv)
    out = args.out if args.out == "side" else int(args.out)

    job_id = f"calibration-{args.label}-{time.strftime('%Y%m%d-%H%M%S')}"
    workdir = os.path.join(args.data_dir, "work", job_id)
    os.makedirs(workdir, exist_ok=True)
    if args.dry_run:
        pkt_path = os.path.join(workdir, "hid.log")
    else:
        os.makedirs(os.path.join(args.data_dir, "logs"), exist_ok=True)
        pkt_path = os.path.join(args.data_dir, "logs", f"hid-{time.strftime('%Y-%m-%d')}.log")

    def log(line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp} {line}", flush=True)
        with open(pkt_path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")

    data, ribbon = build(args.label, args.data_dir, workdir, args.image, args.ribbon)
    log(f"== {job_id}: {len(data)} bytes, ribbon {ribbon}, bin {args.input_bin} to {out}, "
        f"offset {alignment.load(args.data_dir)}, gain {alignment.load_plane_gain(args.data_dir)}"
        + (f", image {args.image}" if args.image else ""))
    job = runner.DiscJob(job_id=job_id, copies=1, print_bytes=data, ribbon=ribbon,
                         input_bin=args.input_bin, output_bin=out, reject_bin=None)
    if args.dry_run:
        loader, printer, burners = runner.make_dry_run_backends(os.path.join(workdir, "dryrun"), log=log)
        if ribbon == "mono":
            loader.state.ribbon_code, loader.state.ribbon_remaining = "8", 1000
        policy = None
    else:
        policy = MotionPolicy(args.data_dir, log=lambda m: log("approval: " + m))
        policy.active = True
        loader, printer, burners = runner.make_hardware_backends(allow_motion=policy, log=log)
    t0 = time.monotonic()
    try:
        result = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=log, workdir=workdir)
    finally:
        if policy is not None:
            policy.active = False
        for dev in (loader, printer):
            close = getattr(dev, "close", None)
            if close:
                close()
    log(f"== {job_id}: {result} after {time.monotonic() - t0:.0f} s")
    return 0 if result == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
