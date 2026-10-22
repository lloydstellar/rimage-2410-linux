# Troubleshooting

Two places to look first: the log on the job page, and `<data dir>/logs/hid-<date>.log`, which has every packet sent to the machine and every answer. For the service, `journalctl -u rimage-web` has the output of the web app. For USB problems, `sudo dmesg -w` shows what the kernel sees.

## The machine is not found

1. **Power.** Is the Rimage switched on, and has it finished starting up?
2. **USB.** Run `lsusb`. You should see `0cc3:000d` (the autoloader), `044e:1311` (the Everest 400) and two USB to SATA bridges (the recorders). If some or all are missing, try another cable or port, or switch the machine off and on.
3. **Permissions.** Run `ls -l /dev/hidraw* /dev/usb/lp*`. Your user (or the `rimage` service user) needs read and write access. If not, check the udev rule in `/etc/udev/rules.d/70-rimage.rules` (see [install.md](install.md#3-udev-rules)), reload the rules and replug the USB cable. For the service, check that the user `rimage` is in the groups `plugdev` and `cdrom` (`id rimage`).
4. **Another program.** Only one program can talk to the autoloader at a time. Stop any other tool or a second copy of the web app.
5. **Mode.** Did you start the web app with `--hardware`? Without it, the web app uses the simulated machine. The bar at the top of the page says which mode is active.

If the autoloader shows a different product ID than `0cc3:000d`, the software does not find it yet. Please open an issue with the output of `lsusb -v -d 0cc3:`.

## The printer resets while it receives a job

Symptoms: a few seconds into a job the printer disconnects and reconnects. The kernel log shows `usblp0: nonzero write bulk status received: -71` and a USB disconnect, the display shows "Printer initializing" and the status light turns red for a moment.

The printer starts printing onto the film while the job is still coming in. If the data arrives too slowly, it gives up and resets. A job is about 11 MB and normally arrives in 2 to 3 seconds.

- Connect the Rimage directly to a USB 2.0 (or faster) port on the computer. Avoid extra hubs, long extension cables and slow adapters.
- Check with `lsusb -t` that the Rimage runs at 480M.
- On a Raspberry Pi, use the official power supply. A weak supply can cause USB problems.

The web app reconnects to the printer by itself after a reset. Check the job log to see how the job continued.

## White edges on the printed disc

- **Alignment.** If you see a thin white edge on one side (for example at the top of the inner edge and the bottom of the outer edge), the print is shifted. Print the alignment label and set `offset_x_mm` and `offset_y_mm` (see [calibration.md](calibration.md#alignment)).
- **Disc type and bleed.** Choose the disc type that matches your discs. The print area must run a little past the white coating. For other discs, measure where the white starts and ends and use Custom size with a small margin on both sides.
- **Artwork.** Let the artwork run to the edge of the square (120 mm) and use Fill. With Fit, a non-square image leaves a border in the background colour.

## Blue looks purple, grey looks pink

Cyan prints too weakly: small cyan dots do not come out well. Raise the cyan gain, for example `"plane_gain": {"1": 1.2}`, and print the gain test or the alignment label to compare. Stay at or below about 1.3; see [calibration.md](calibration.md#colour-strength).

## Coloured edges on black text

A blue edge on one side and a red edge on the other side of black letters means the cyan plane does not line up with magenta and yellow. Correct it with `plane_shift_lines` (see [calibration.md](calibration.md#colour-plane-registration)).

The registration varies by about 0.1 mm from one print to the next, so a correction can only remove the average. A thin edge on some prints is normal. If it gets worse over time, clean the print head and rollers following the printer's maintenance instructions.

## The side bin is full

The side bin holds 5 discs. The autoloader does not count the discs in it, so the software cannot know how many are already there. Empty the side bin before every job. If the machine reports that it is full, the job asks you to empty it (Retry, Stop job). For more than 5 copies, choose a bin as output, or use "Automatic", which picks bin 2 for runs of more than 5.

## The wrong recorder opens, or a recorder is not found

The autoloader knows the two recorders as element 241 (top) and element 242 (bottom). Linux calls them `/dev/sr0` and `/dev/sr1`, but that numbering can change after a restart. The software therefore finds them by their port on the internal USB hub of the machine: port 2 is the top recorder (241), port 1 is the bottom recorder (242).

If that does not match your machine, the wrong tray could open while the arm is above the other recorder. Stop using `--hardware` until this is fixed.

Find out which drive is which, with no job running and the web app stopped:

1. List the drives and their serial numbers:

   ```bash
   ls -l /dev/disk/by-id/ | grep sr
   ```

   Each line ends with `-> ../../sr0` or `-> ../../sr1`. The name before it contains the serial number of the USB to SATA bridge of that recorder.

2. See which hub port each drive is on:

   ```bash
   readlink -f /sys/block/sr0/device /sys/block/sr1/device
   ```

   In a path like `.../3-8.2:1.0/...`, the number after the last dot before `:1.0` is the hub port.

3. Open the front door (the web app is stopped, so nothing moves) and check which tray opens:

   ```bash
   eject /dev/sr0        # watch: top or bottom tray?
   eject -t /dev/sr0     # close it again
   ```

   Do the same for `/dev/sr1`. Make sure both trays are closed before you close the door.

4. If the hub ports do not match (port 2 top, port 1 bottom), set the serial numbers explicitly. Use the serial of the top recorder for 241 and of the bottom recorder for 242:

   ```bash
   RIMAGE_RECORDERS="TOPSERIAL=241,BOTTOMSERIAL=242" .venv/bin/python -m rimage2410.web --hardware
   ```

   For the service, add the line `RIMAGE_RECORDERS=TOPSERIAL=241,BOTTOMSERIAL=242` to `/etc/default/rimage-web` and restart it.

Please also report your machine model and what you found, so the default can be improved.

## Audio files are not accepted

- "ffprobe missing" or unknown playing time: install `ffmpeg`, which includes `ffprobe`.
- Tracks shorter than 4 seconds cannot be burned.
- A CD holds at most 80 minutes, including the pauses between tracks.
