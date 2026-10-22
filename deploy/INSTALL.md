# Raspberry Pi service

Short version of the service setup. The full guide, with system packages, udev rules, options and
storage advice, is in [docs/install.md](../docs/install.md).

The files in this directory:

| File | Install as | Purpose |
| --- | --- | --- |
| `rimage-web.service` | `/etc/systemd/system/rimage-web.service` | systemd unit that runs the web app as the user `rimage` |
| `rimage-web.env` | `/etc/default/rimage-web` | host, port, data directory, access code and upload limit |
| `70-rimage.rules` | `/etc/udev/rules.d/70-rimage.rules` | USB access for the group `plugdev` (service without a logged-in user) |
| `70-rimage-desktop.rules` | `/etc/udev/rules.d/70-rimage.rules` | USB access for the logged-in user (laptop or desktop); use this one or `70-rimage.rules`, not both |

## Steps

Raspberry Pi OS Bookworm or newer, 64-bit.

```bash
sudo apt install -y python3-venv python3-dev git ffmpeg cdrdao cdrskin xorriso eject
sudo useradd --system --create-home --home-dir /var/lib/rimage --shell /usr/sbin/nologin rimage
sudo usermod -aG cdrom,plugdev rimage
sudo mkdir -p /opt/rimage2410
sudo chown "$USER" /opt/rimage2410
git clone https://github.com/lloydstellar/rimage-2410-linux /opt/rimage2410
python3 -m venv /opt/rimage2410/venv
/opt/rimage2410/venv/bin/pip install -e /opt/rimage2410

sudo cp /opt/rimage2410/deploy/70-rimage.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger

sudo cp /opt/rimage2410/deploy/rimage-web.env /etc/default/rimage-web
sudo nano /etc/default/rimage-web             # set RIMAGE_ACCESS_CODE
sudo cp /opt/rimage2410/deploy/rimage-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rimage-web
journalctl -u rimage-web -f                   # follow the log
```

Then open `http://<address-of-the-pi>:8080`.

The unit starts the web app in dry run. To drive the real machine, add `--hardware` to the end of
the `ExecStart` line, then run `sudo systemctl daemon-reload` and `sudo systemctl restart rimage-web`.
If you cloned the code somewhere else, change `WorkingDirectory` and `ExecStart` to match.

Check with `lsusb` that the autoloader shows up as `0cc3:000d` and the printer as `044e:1311`.

Keep the Pi on your local network. The web app has no HTTPS: do not forward its port to the
internet, and use a VPN if you need access from outside.
