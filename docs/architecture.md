# Architecture

`rimage2410` is a Python package (Python 3.11 or newer) with Flask, Pillow and NumPy as its only
Python dependencies. Burning uses the command-line tools `xorriso`, `cdrskin`, `cdrdao` and
`ffmpeg` (see [install.md](install.md)).

Run the tests from the repository root:

```bash
python -m pytest
```

The tests use the simulated machine only. They never open a USB device or a recorder.

## Layers

```
web/            Flask app, job queue (SQLite), background worker
  |
runner.py       one job: burn, print and sort N discs, with progress and operator questions
  |
robot/          autoloader commands, sense codes, motion sequences (Robot)
everest/        colour conversion, halftoning, print jobs and status commands for the Everest 400
burn.py         burn plans and the commands for xorriso, cdrskin and cdrdao
  |
hardware.py     the connections (HID, USB printer, optical drives) and their dry-run versions
```

Only `hardware.py` (and `burn.LocalBurner`, which runs the burn tools on a recorder) talks to
devices. Everything above it builds and parses bytes, or calls the interfaces below.

## Modules

| Module | Contents |
| --- | --- |
| `geometry.py` | Dimensions of the print image: 400 dpi, 1889 px = 120 mm, 1920 bytes per line, disc types and their print area (`MEDIA`). |
| `label.py` | Image to print image (`prepare_label`) and a preview of the printed disc (`render_preview`). |
| `artwork.py` | Checks on supplied artwork (`Finding` with level `error`, `warning` or `info`), conversion to sRGB. |
| `template.py`, `psd.py` | Label templates as PNG and as a layered Photoshop file. |
| `calibration.py`, `alignment.py` | Calibration disc and per-printer corrections (offset, plane shift, plane gain) in `<data dir>/printer.json`. |
| `audio.py` | Measuring audio files (ffprobe, ebur128), conversion to CD audio with dither, gapless alignment, CD-TEXT, ISRC and UPC checks. |
| `burn.py` | `BurnPlan` (validation, TOC and CD-TEXT sheets, burn commands) and `LocalBurner`. |
| `everest/color.py`, `everest/halftone.py` | RGB label to colour planes with 8-bit heat energy per pixel. Tables in `everest/data/`. |
| `everest/job.py` | Print job bytes (`build_job`, `parse_job`) and the allowed status commands (`status_command`, `parse_r3`, `parse_r6`, `parse_g2`). |
| `everest/inspect.py` | Command-line tool to list the blocks of a print job and preview its planes. |
| `robot/commands.py` | Autoloader commands as `Command` objects, response parsers and the block list (`check_allowed`). |
| `robot/sense.py` | Meaning of changer and printer sense codes and how to recover from them. |
| `robot/sequence.py` | `Robot`: pick, place, load and unload recorders and the printer, wait loops, tray guard, `Problem` questions for the operator. |
| `robot/__main__.py` | Command-line tool for test sessions (show, check and send single commands; motion only with `--confirm`). |
| `motion_policy.py` | Decides which motion commands the real machine may run during a job. |
| `progress.py` | `Tracker`: thread-safe snapshot of what each recorder, the printer, the arm and each disc is doing. |
| `hardware.py` | `Loader`, `Printer` and `Burner` interfaces, the real connections and the simulated machine. |
| `runner.py` | `DiscJob` and `run_job`: the full order of a batch. |
| `web/` | Flask app (`app.py`), queue (`store.py`), worker thread (`worker.py`), releases (`releases.py`), templates and static files. |

## Interfaces in `hardware.py`

```python
class Loader(Protocol):          # autoloader
    def transact(self, command: bytes, expect: int, timeout_s: float = 10.0) -> bytes: ...
    # command: the full ASCII bytes including ESC, for example b'\x1b!e1C'
    # expect: the expected response length

class Printer(Protocol):         # Everest 400
    def send_job(self, job: bytes) -> None: ...
    def query(self, command: bytes, expect: int) -> bytes: ...

class Burner(Protocol):          # one recorder
    device: str                  # for example '/dev/sr0'
    def burn(self, plan, log: Callable[[str], None]) -> None: ...
    def eject(self) -> None: ...
    def close_tray(self) -> None: ...
```

Implementations:

| Interface | Real machine | Dry run |
| --- | --- | --- |
| `Loader` | `HidLoader` (`/dev/hidrawN`, USB `0cc3:000d`) | `DryRunLoader` |
| `Printer` | `UsbPrinter` (`/dev/usb/lpN`, USB `044e:1311`) | `DryRunPrinter` (writes `.prn` files) |
| `Burner` | `burn.LocalBurner` (recorders found by `find_recorders`) | `DryRunBurner` |

The dry-run classes share one `SimState` (bins, gripper, recorder trays, printer, ribbon, door).
Tests can inject faults through `SimState.faults`, for example `{"burn": 1}` or `{"clamp": 2}`.

`HidLoader.transact` lets read commands through (`READ_ONLY_PREFIXES`). Every other command must
pass `robot.commands.check_allowed` and the `allow_motion` callback, which is a
`motion_policy.MotionPolicy` in the web app. Every packet is written to the HID log.

Recorders are matched to their position in the machine by serial number. Set
`RIMAGE_RECORDERS="SERIAL=241,SERIAL=242"` when the automatic order is wrong.

## `runner.py`

```python
@dataclass
class DiscJob:
    job_id: str
    copies: int = 1
    source_kind: str = "none"          # 'none', 'iso', 'audio', 'cue'
    source_paths: list[str] = ...
    label_path: str | None = None      # print image (PNG, 1889 x 1889) or None
    ribbon: str = "auto"               # 'auto', 'cmy', 'cmyw', 'mono'
    input_bin: int = 1                 # 1..3
    output_bin: int | str = 2          # 1..3 or 'side' (the side bin, element 300)
    reject_bin: int | None = 3
    titles, album, artist, audio ...   # audio CD options
    color_method: object = 0           # 0, 2 or 'simple'
    parallel: bool = True              # use both recorders at once
    print_offset, plane_shift, plane_gain   # corrections from alignment.py

def run_job(job, *, loader, printer, burners, log, should_stop=lambda: False, timing=None,
            workdir=None, sleep=None, progress=None, ask=None, ram_dir=RAM_ROOT) -> str:
    """Returns 'done', 'failed' or 'stopped'."""

def make_dry_run_backends(workdir, *, step_delay_s=0.0, burn_s=0.0, log=None)
def make_hardware_backends(*, allow_motion, log=None)
def resolve_output(value, copies) -> int | str   # 'auto': side bin for 5 or fewer, otherwise bin 2
```

Order per disc: the label is sent to the printer early so it is on the retransfer film when the
disc arrives; the arm takes a disc from the input bin to a free recorder; the burn runs in its own
thread; a disc that failed to burn goes straight to the reject bin without a label; a burned disc
goes to the printer, is printed and goes to the output bin. While one disc is printed, the next
disc is already loaded into a recorder.

`ask(problem) -> str` is called when the operator has to decide something. A `Problem` has a
`code` (for example `bin_empty`, `tray_close`, `clamp_error`, `new_command`), a `title`, a
`message` and `options` (`retry`, `reject`, `stop`, `yes`, `no`, `allow`). Without `ask`, a problem
fails the job.

## `progress.Tracker`

The snapshot is a dict that the web app shows as is:

- `phase`: `preparing`, `running`, `done`, `stopped` or `error`;
- `discs`: one entry per copy with `stage` (`waiting`, `loading`, `burning`, `burned`,
  `burn_failed`, `unloading`, `to_printer`, `printing`, `printed`, `storing`, `done`,
  `rejected`) and `where` (for example `input bin`, `bin 2`, `the side bin`);
- `drives` and `printer`: `state` (`idle` or a short description) and the current disc;
- `arm`: what the arm is doing;
- `problem`: the open operator question, or `None`;
- `total`, `done`, `rejected` and `error`.

`web/static/progress.js` and the `.st-<stage>` classes in `style.css` use the same values.

## Label and print job

```python
label.prepare_label(img, *, fit="cover", scale=1.0, offset_x=0.0, offset_y=0.0, rotate=0.0,
                    background=(255, 255, 255), outer_mm=118.0, inner_mm=24.0,
                    print_hub=False, canvas=1889) -> Image   # RGB, white outside the print area
label.render_preview(label, *, size=800, outer_mm=118.0, inner_mm=24.0, print_hub=False) -> Image  # RGBA

everest.color.convert(label_rgb, *, ribbon="cmy", method=0, white_mask=None, ...) -> dict[int, ndarray]
# mono -> {0: K}; cmy -> {1: C, 2: M, 3: Y}; cmyw -> {1: C, 2: M, 3: Y, 4: W}
# each plane: uint8, shape (lines, 1920), mirrored

everest.job.build_job(planes, *, ribbon="cmy", quality=1, copies=1, media="0",
                      media_in_header=False, include_m2=True, include_end=True) -> bytes
everest.job.status_command(name) -> bytes   # read-only commands only, for example 'r3' -> b'\x1b!r3C'
```

`runner.build_print_job` combines these with the corrections from `alignment.py`. The job the
runner sends has the media byte in the header, no `ESC!m2` and no end block.

## Web app

`web.app.create_app(config)` builds the Flask app. Main settings:

| Key | Default | Meaning |
| --- | --- | --- |
| `DATA_DIR` | `~/rimage-data` | jobs, uploads, releases, logs, settings |
| `ACCESS_CODE` | empty | login code; empty means no login |
| `HARDWARE` | `dry-run` | `dry-run` or `real` (`--hardware`) |
| `RAM_DIR` | `/dev/shm` | converted audio in memory; empty means always on disk |
| `START_WORKER` | `True` | start the background worker thread |

`web.worker.Worker` takes jobs from the SQLite queue one at a time and calls `run_job`. In dry run
the simulated machine keeps its state between jobs. With the real machine, starting a job counts
as approval for motion commands that are known to work (`motion_policy.TESTED`), that are regular
moves of a job (`motion_policy.standard`), or that the operator allowed before
(`<data dir>/approved_commands.json`). A command outside that set is shown to the operator with
the options Allow and Stop job. Outside a job, only read commands are sent.

`GET /api/machine` reports `mode` (`real` or `dry-run`), the connection state, bins, gripper,
ribbon and film.

## Safety rules in the code

- `robot.commands.check_allowed` refuses commands that write permanently to the autoloader, change
  its calibration or start its bootloader. `everest.job.status_command` refuses printer commands
  that write to the printer's memory. Do not work around these checks.
- The software never updates firmware.
- Before every arm movement, `Robot.guard_trays` checks that no recorder tray is open in the arm's
  path, and closes it if needed.
