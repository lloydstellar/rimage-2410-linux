# Installation

The software runs on any Linux computer with Python 3.11 or newer. Two common setups:

- **Laptop or desktop.** You start the web app yourself and use it in your browser. Good for trying things out.
- **Raspberry Pi.** The web app runs as a service next to the Rimage, and you use it from a laptop or phone on the same network. Use 64-bit Raspberry Pi OS (Bookworm or newer). A Pi 5 with 8 GB of memory and an SSD for the data directory is a good choice; see [Storage and speed](#storage-and-speed).

Connect the Rimage directly to a USB 2.0 (or faster) port on the computer. The whole machine runs at USB 2.0 speed (480M) and shares that bandwidth between both recorders, the printer and the autoloader.

## 1. System packages

On Debian, Ubuntu and Raspberry Pi OS:

```bash
sudo apt update
sudo apt install -y python3-venv python3-dev git ffmpeg cdrdao cdrskin xorriso eject
```

What they are for:

| Package | Used for |
| --- | --- |
| `ffmpeg` | reading audio files, measuring them (`ffprobe`) and converting them to CD format |
| `cdrskin` | burning audio CDs, with CD-TEXT |
| `cdrdao` | burning BIN/CUE images |
| `xorriso` | burning data discs from an ISO image |
| `eject` | opening and closing recorder trays |
| `python3-venv`, `python3-dev`, `git` | installing the software |

On other distributions, install the same tools with your package manager.

## 2. The software

On a laptop or desktop, anywhere you like:

```bash
git clone https://github.com/lloydstellar/rimage-2410-linux.git rimage-2410-linux
cd rimage-2410-linux
python3 -m venv .venv
.venv/bin/pip install -e .
```

The Python dependencies (Flask, Pillow, NumPy) are installed by pip.

On a Raspberry Pi that will run the service, use `/opt/rimage2410` and a separate system user:

```bash
sudo useradd --system --create-home --home-dir /var/lib/rimage --shell /usr/sbin/nologin rimage
sudo usermod -aG cdrom,plugdev rimage
sudo mkdir -p /opt/rimage2410
sudo chown "$USER" /opt/rimage2410
git clone https://github.com/lloydstellar/rimage-2410-linux.git /opt/rimage2410
python3 -m venv /opt/rimage2410/venv
/opt/rimage2410/venv/bin/pip install -e /opt/rimage2410
```

## 3. udev rules

By default only root may open the autoloader (`/dev/hidrawN`) and the printer (`/dev/usb/lpN`). The `deploy/` directory has two rule files. Install **one** of them as `/etc/udev/rules.d/70-rimage.rules`:

| File | Use it when | What it does |
| --- | --- | --- |
| `deploy/70-rimage-desktop.rules` | you run the web app yourself on a laptop or desktop | gives the logged-in user access (`uaccess`), the same way Linux handles CD drives |
| `deploy/70-rimage.rules` | the web app runs as a service without a logged-in user (Raspberry Pi) | gives the group `plugdev` access; the `rimage` user is in that group |

Both files also turn off USB autosuspend for the autoloader and the printer, so they do not fall asleep during a long job.

```bash
sudo cp deploy/70-rimage-desktop.rules /etc/udev/rules.d/70-rimage.rules   # or deploy/70-rimage.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Then switch the Rimage off and on, or unplug and replug the USB cable. Check with `lsusb` that you see `0cc3:000d` (the autoloader) and `044e:1311` (the Everest 400). If your autoloader shows another product ID, the rules and the software will not find it; please open an issue with the `lsusb` output.

The recorders are normal optical drives. On a desktop the logged-in user can use them already. The service user needs the group `cdrom`, which the commands above add.

## 4. Run it

Dry run, only on this computer:

```bash
.venv/bin/python -m rimage2410.web --port 8080
```

The real machine, reachable from the network:

```bash
.venv/bin/python -m rimage2410.web --host 0.0.0.0 --port 8080 --hardware --access-code 'choose-a-code'
```

Options:

| Option | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--host` | | `127.0.0.1` | `0.0.0.0` makes the web app reachable from the network |
| `--port` | | `8080` | |
| `--data-dir` | `RIMAGE_DATA_DIR` | `~/rimage-data` | where jobs, uploads, releases, logs and settings are kept |
| `--access-code` | `RIMAGE_ACCESS_CODE` | empty | code to log in; empty means no login |
| `--max-upload-mb` | `RIMAGE_MAX_UPLOAD_MB` | `2048` | largest upload per request, in MB |
| `--hardware` | | off | drive the real machine instead of the simulation |

If the Rimage is switched off when the web app starts, it starts anyway and connects as soon as the Machine page or a job needs the machine. It also reconnects when the USB connection drops, for example after you switch the machine off and on.

If the recorders are not detected correctly, see [Recorder mapping](troubleshooting.md#the-wrong-recorder-opens-or-a-recorder-is-not-found).

## 5. Run it as a service

The `deploy/` directory has a systemd unit and a settings file:

```bash
sudo cp /opt/rimage2410/deploy/rimage-web.env /etc/default/rimage-web
sudo nano /etc/default/rimage-web             # set the access code
sudo cp /opt/rimage2410/deploy/rimage-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rimage-web
journalctl -u rimage-web -f                   # follow the log
```

`/etc/default/rimage-web` sets `RIMAGE_HOST`, `RIMAGE_PORT`, `RIMAGE_DATA_DIR` (`/var/lib/rimage`), `RIMAGE_ACCESS_CODE` and `RIMAGE_MAX_UPLOAD_MB`.

The unit runs the web app as the user `rimage` with the groups `cdrom` and `plugdev`. It may only write to `/var/lib/rimage`. Before you enable it, check `WorkingDirectory` and `ExecStart` in the unit: they must point to the cloned code (`/opt/rimage2410` in the steps above) and to the virtual environment (`/opt/rimage2410/venv`).

The unit as shipped starts the web app in dry run. To drive the real machine, add `--hardware` to the end of the `ExecStart` line, then run `sudo systemctl daemon-reload` and `sudo systemctl restart rimage-web`.

Make sure only one program talks to the autoloader at a time. Stop the service before you use other tools on the machine.

## 6. Access code and network

- Set an access code (`RIMAGE_ACCESS_CODE` or `--access-code`). Without one, anyone on your network can start jobs.
- The web app has no HTTPS and is meant for a home or office network. Do not forward its port to the internet. For access from outside, use a VPN such as WireGuard or Tailscale.
- You can limit access with a firewall, for example `sudo ufw allow from 192.168.1.0/24 to any port 8080`.
- Uploads stay in the data directory. Delete old jobs from the Queue now and then, or ISO files will fill the disk.

## Data directory

```
<data dir>/
  jobs.sqlite              the queue
  secret.key               key for login sessions
  uploads/img-<id>.*       uploaded images
  uploads/src-<id>/        uploaded audio files, ISO or BIN/CUE
  jobs/<id>/label.png      label image (1889 x 1889 pixels)
  jobs/<id>/preview.png    preview of the printed disc
  work/<id>/               working files of a job
  releases/<id>/           saved releases (release.json, artwork, tracks/, preview.png)
  dryrun/printjobs/        print jobs created in dry run (.prn)
  logs/hid-<date>.log      real machine: every packet and every approval
  approved_commands.json   real machine: commands you allowed in the web app
  printer.json             print corrections for your printer (see calibration.md)
```

## Storage and speed

Burning two discs at once needs a steady data stream. The software helps in three ways:

- Audio is converted to CD format once, before burning. The converted files go to `/dev/shm` (memory) when they fit with at least 1 GB to spare. A full CD is about 800 MB.
- Each recorder gets a 64 MB buffer (cdrskin, xorriso and cdrdao).
- ISO images and uploads are burned straight from the data directory. On a Raspberry Pi, put the data directory on an SSD, not on the SD card.

Data rate per recorder:

| | 1x | 16x | 52x |
| --- | --- | --- | --- |
| Audio CD | 0.18 MB/s | 2.8 MB/s | 9.2 MB/s |
| DVD | 1.4 MB/s | 22 MB/s | |

Two DVD recorders at 16x do not fit through one USB 2.0 connection at the same time.
