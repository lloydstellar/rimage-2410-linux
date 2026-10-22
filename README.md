# rimage-2410-linux

Burn, move and print discs on a Rimage Professional disc publisher from Linux, through a web app in your browser.

Also known as: Rimage 2410, Rimage 3410, Rimage 5410N, Rimage Professional, Everest 400, disc publisher, CD/DVD autoloader, Linux driver.

## Supported hardware

The Rimage Professional 2410, 3410 and 5410N (including the Medical/MDD variants) are one family: a robot arm, a carousel with three bins, two CD/DVD recorders and an integrated Everest 400 thermal retransfer printer, all behind one USB cable. The autoloader in these machines reports itself as "RIMAGE 4410".

So far only a **Rimage Professional 3410** has been tested, with autoloader firmware 6.033A and an Everest 400. The 2410 and 5410N should behave the same, but nobody has tried them yet. If you have one, please report what works and what does not (see [CONTRIBUTING.md](CONTRIBUTING.md)).

What the computer sees over USB:

| Device | USB ID | Linux device |
| --- | --- | --- |
| Autoloader (robot arm, carousel) | `0cc3:000d` | `/dev/hidrawN` |
| Everest 400 printer | `044e:1311` | `/dev/usb/lpN` |
| Two recorders | USB to SATA bridges | `/dev/sr0`, `/dev/sr1` |

Do not update the autoloader firmware. Firmware 6.041 and later only accepts RFID (Encore) ribbons. This software was verified with 6.033A.

## Features

- Web app for a laptop, desktop or Raspberry Pi, usable from a phone or another computer on the same network.
- Label printing on the Everest 400: live preview of the printed disc, artwork checks (resolution, colour profile, details that fall outside the print area), print area per disc type with a small bleed, and Photoshop and PNG templates.
- Colour conversion for the Everest, with per-printer corrections for image position, colour plane registration and the strength of each colour.
- Audio CDs from WAV or FLAC (24-bit masters too), MP3, OGG, M4A or AIFF: high quality conversion to 16-bit with TPDF dither (ffmpeg with soxr), pause per track, gapless mode, CD-TEXT, ISRC, UPC/EAN, copy bit and a choice of write speed (16x recommended for old players). Burned with cdrskin.
- Data discs from an ISO image (xorriso) and BIN/CUE images (cdrdao).
- Batches with both recorders at work: while one disc burns, the arm and the printer handle the other one.
- Output to any bin or to the small side bin, which you can empty without opening the door.
- Releases: save everything for a CD (artwork, tracks, settings) and make more copies later with a few clicks. Handy for print on demand.
- Live progress per disc, operator questions with buttons (Retry, Reject disc, Stop job) and a log of every packet sent to the machine.
- Dry run mode with a simulated machine, so you can try everything without hardware.

Tested on the machine so far: arm moves, the side bin, burning an audio CD with CD-TEXT at 16x, printing with the CMY colour ribbon, and a complete one-disc job from the web app. Not yet tested on the machine: data discs, BIN/CUE, the black and CMY + white ribbons, and a batch that uses both recorders at the same time. These work in dry run.

## Quick start

On Debian, Ubuntu or Raspberry Pi OS:

```bash
sudo apt install -y python3-venv python3-dev git ffmpeg cdrskin cdrdao xorriso eject

git clone https://github.com/lloydstellar/rimage-2410-linux.git rimage-2410-linux
cd rimage-2410-linux
python3 -m venv .venv
.venv/bin/pip install -e .
```

Allow your user to talk to the autoloader and the printer (for a desktop or laptop session):

```bash
sudo cp deploy/70-rimage-desktop.rules /etc/udev/rules.d/70-rimage.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Start in dry run first. Nothing is sent to the machine:

```bash
.venv/bin/python -m rimage2410.web --port 8080
```

Open <http://localhost:8080>. Try a job and watch the simulated machine on the Machine page.

When you are ready, switch on the Rimage, connect it with USB and start with `--hardware`:

```bash
.venv/bin/python -m rimage2410.web --port 8080 --hardware
```

Now starting a job moves the arm, burns and prints. Read [docs/safety.md](docs/safety.md) first.

To reach the web app from other devices, add `--host 0.0.0.0` and set an access code. See [docs/install.md](docs/install.md), which also covers running it as a service on a Raspberry Pi.

## Safety in short

- The web app runs in dry run unless you start it with `--hardware`.
- Starting a job counts as approval for the commands that are known to work on this machine family.
- A command that was never used before is shown in the web app first, with **Allow** and **Stop job**. An allowed command is remembered in `<data dir>/approved_commands.json`.
- Outside a job, only read commands are sent. Nothing moves.
- Commands that write persistent settings, calibration or firmware are blocked in the code and are never sent.
- Opening the front door stops the arm. The software checks the recorder trays before every arm move.

## Documentation

- [Installation](docs/install.md): laptop, desktop and Raspberry Pi, udev rules, systemd service, access code
- [Usage](docs/usage.md): the web app pages, your first job, how batches are scheduled, typical timings
- [Calibration](docs/calibration.md): alignment, colour ramp, gain test and `printer.json`
- [Safety](docs/safety.md): the approval model, blocked commands, the door and the recorder trays
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md): tests, dry run and hardware reports

## Disclaimer

This project is not affiliated with, endorsed by or supported by Rimage Corporation. Rimage, Everest and the product names are trademarks of their owners. Use at your own risk.

## License

Copyright (C) 2026 Lloyd Stellar.

GPL-3.0-or-later. See [LICENSE](LICENSE). The colour tables in `rimage2410/everest/data/` are not covered by the GPL; see [NOTICE](NOTICE).
