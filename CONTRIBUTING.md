# Contributing

Bug reports, hardware reports and pull requests are welcome. Reports from owners of a 2410, a 5410N or a Medical/MDD variant are especially useful, because only a 3410 has been tested so far.

## Development setup

```bash
git clone https://github.com/lloydstellar/rimage-2410-linux.git rimage-2410-linux
cd rimage-2410-linux
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

## Tests

```bash
.venv/bin/pytest
```

The tests do not touch any hardware. They cover the colour conversion and print job format, labels and templates, audio and burn planning, the robot sequences against the simulated machine, the web app with the Flask test client, and complete jobs in dry run.

## Dry run

Most work can be done without a machine:

```bash
.venv/bin/python -m rimage2410.web --port 8080 --data-dir /tmp/rimage-test
```

The simulated machine keeps its state between jobs (bins run empty, discs end up in the output bin). On the Machine page you can set up faults, such as an empty input bin, a clamp error in the printer or a failed burn, and see which question the job asks. Print jobs created in dry run are saved in `<data dir>/dryrun/printjobs/`.

## Rules for code that talks to the machine

- New hardware code goes behind the existing interfaces in `rimage2410/hardware.py`.
- Do not remove or work around the checks in `rimage2410/robot/commands.py` (`check_allowed`) and `rimage2410/everest/job.py` (`status_command`). Pull requests that send blocked commands, write persistent settings or firmware, or put a device into boot mode will not be accepted.
- A command that has not been used on a real machine before goes through the approval question in the web app. Do not add it to the list of known commands (`TESTED` in `rimage2410/motion_policy.py`) until someone has run it on a machine and reported the result.

See [docs/safety.md](docs/safety.md).

## Reporting hardware results

Please include:

- **Model** from the type plate on the machine (for example Professional 3410), and whether it is a Medical/MDD variant.
- **Firmware** of the autoloader. At the start of every job the web app asks the autoloader to identify itself. The job log then has a line starting with `autoloader:` that contains the model name and version, for example `RIMAGE 4410     VERSION 6.033A`. Please do not update the firmware to test this software.
- **Printer**: Everest 400 or another model, and which ribbon you used.
- **USB IDs** from `lsusb` and, if relevant, the output of `lsusb -t`.
- **Recorders**: model, and which `/dev/sr*` turned out to be the top and the bottom recorder.
- **What you did and what you saw**: the job settings, what the machine did, any question the web app asked and what you answered.
- **Logs** from `<data dir>/logs/` (`hid-<date>.log`) and the job log from the job page. The packet log contains the serial numbers of your machine; remove them if you prefer.
- **Photos** of printed discs, for print problems. Sharp, in daylight, and the original files.

For print quality, include your `<data dir>/printer.json`.

## Style

- Python 3.11 or newer.
- Keep user-facing text short and plain, in English. The web app uses these terms: New job, Releases, Queue, Machine, Job, Dry run, Real machine, Input bin, Output, Reject bin, Side bin, Gripper, Printer tray, Ribbon, Retransfer film, Heat roller, Retry, Reject disc, Stop job, Allow.
- Add or update tests with every change.

By contributing you agree that your contribution is licensed under GPL-3.0-or-later.
