# Ribbon patch for autoloader firmware 6.033A

The autoloader in the Rimage Professional 2410, 3410 and 5410N checks the ribbon in the Everest 400. If it does not like the ribbon, it refuses to pick a disc and shows error 33, "Installed ribbon is not authorized for this system". The hardware of these models is the same, but the firmware accepts a different set of ribbons depending on the model. On our 2410 the black ribbon 2002160, which is sold for the 3410, was refused this way, while the colour ribbon worked.

`ribbon_patch.py` makes a patched copy of the official firmware file `INTEGRA-XX10-6033.bin` that accepts these ribbons. The firmware itself is not in this repository. Download it from Rimage and run the script on your own copy.

What the patch changes: 5 bytes in the ribbon check, which widen the range of accepted ribbon data, and the SHA-1 checksum that the firmware file carries at `0xF7EC`. Everything else stays byte for byte the same, including the version string. The machine still reports 6.033A after the update.

## Read this first

Flashing firmware can leave your autoloader unusable if it goes wrong. You do this at your own risk. This project has no connection with Rimage.

- Only firmware 6.033A is supported. Never install 6.041 or later on these machines: from 6.041 the autoloader only accepts Encore RFID ribbons.
- Keep the original `INTEGRA-XX10-6033.bin`. It is your way back.
- Tested on one machine so far. After the patch the black ribbon 2002160 is picked and prints, and the CMY colour ribbon is picked as before. Please report your results (see [CONTRIBUTING.md](../../CONTRIBUTING.md)).

## What you need

- `INTEGRA-XX10-6033.bin` from the Rimage support site (search for the file name). It should be 64000 bytes with SHA-256 `e5d558a6375c462c523204d5506d5dc7dcbbb630268b0949269df94bb4024d67`. As far as we know the same file is used for the 2410, 3410 and 5410N.
- Python 3.8 or later. No other packages.
- To flash: GemUtil (`Gemutil.exe`, "Rimage Autoloader Utilities"), Rimage's own program for autoloader firmware. It is a Windows program, but it runs under Wine on Linux (see below). You find it on the Rimage support site and in the Rimage Software Suite as `GemUtil/GemUtil_en-US.msi`. We used the one from suite 9.4.1.46 (SHA-256 `31626af5c4bf784cc4732203fb34c1d9ecee6355b0d4553d662579d150774afa`).

## Patch the file

```bash
python3 tools/ribbon-patch/ribbon_patch.py INTEGRA-XX10-6033.bin
```

This writes `INTEGRA-XX10-6033b.bin` next to the original. Use `-o` for another name and `--force` to replace an existing output file. The script never changes the input file.

To find out what a file is, without writing anything:

```bash
python3 tools/ribbon-patch/ribbon_patch.py --check SOME-FILE.bin
```

It tells you whether the file is the original, the patched version or something else, and why.

## The checks

Before patching:

- The file must be exactly the original: 64000 bytes with the SHA-256 above.
- If it is not, the script tells you what is wrong: too short (incomplete download), not firmware for these models, another firmware version, a damaged file (the built-in SHA-1 does not match) or a file someone else has modified.
- On top of the hash, the script checks the version string, the built-in SHA-1 and the original bytes and instructions at each patch location.

After patching, in memory and again after reading the written file back from disk:

- only the 5 patch bytes and the 20 checksum bytes differ from the original;
- the first 16 KB (the part the flashing tool inspects) and the version string are unchanged;
- the new built-in SHA-1 is correct;
- the SHA-256 of the result is `4bf97b837654c0b1545052f29f88c7a810ee0da5024465a1d5bbd24adb14a2dd`, the file that was tested on the machine.

The output is written to a temporary file first and only renamed to its final name when every check passes. If anything fails, nothing is written.

## Flash the patched file

1. Stop the web app. Close the door, make sure the arm is empty and that there are no discs in the recorders or the printer tray. GemUtil sends the arm home when it starts and after the update.
2. Start GemUtil, select the autoloader (HID) and check that it reads version 6.033A.
3. Choose Update Firmware and select `INTEGRA-XX10-6033b.bin`. Leave the firmware compatibility check enabled. If the check fails, stop and find out why.
4. Do not switch off the machine or unplug the USB cable while it is downloading. The autoloader switches to boot mode, receives the file and restarts by itself after about 15 seconds. GemUtil connects again about 15 seconds later.
5. Close GemUtil, switch the Rimage off and on, and try picking a disc with the ribbon that was refused before.

In GemUtil, leave "Apply Settings" and "Update SN" alone. They write settings into the autoloader.

## Run GemUtil on Linux with Wine

This is how the patched firmware was flashed on our machine, from a Linux laptop, without Windows. Tested on Arch Linux with Wine 11.17; a recent Wine on another distribution should work the same way.

GemUtil talks to the autoloader as an ordinary USB HID device. Wine passes that through via `/dev/hidraw`, so the udev rule from [deploy/](../../deploy/) is all you need for access. Unlike the Windows virtual machine we tried first, Wine also picks up the autoloader again when it reconnects in boot mode during the update.

Install Wine and GemUtil once, in a separate Wine prefix so it does not mix with other Windows programs. On other distributions, install a recent Wine with 32-bit support (GemUtil is a 32-bit program); we have not tried that.

```bash
sudo pacman -S wine            # Arch Linux
export WINEPREFIX=~/.wine-rimage
wineboot -i                    # Mono and Gecko are not needed; cancel if asked
sha256sum GemUtil_en-US.msi    # compare with the checksum above
wine msiexec /i GemUtil_en-US.msi
```

Start GemUtil:

```bash
export WINEPREFIX=~/.wine-rimage
cd ~/.wine-rimage/drive_c/"Program Files (x86)"/Rimage/Utilities
wine Gemutil.exe
```

Then:

1. Set Port to HID. GemUtil shows the autoloader and version 6.033A.
2. Test the reconnect before you flash: switch the Rimage off and on, then in GemUtil set Port to another value and back to HID. It should find the autoloader again. If it does not, do not flash.
3. Update Firmware. In the file dialog, drive `Z:` is the root of your Linux file system, so a file in your home directory is at `Z:\home\<you>\INTEGRA-XX10-6033b.bin`.
4. Follow the steps under "Flash the patched file" above. Close the door and empty the arm before you start GemUtil: it sends the arm home right away.

Under Wine, GemUtil shows "Erasing..." and "Downloading" but no final message. You can tell it worked when the autoloader restarts and GemUtil connects again, about 30 seconds after you started the update. On our machine the whole transfer took 12 seconds.

## In a Windows virtual machine

Test this before you flash: with GemUtil open, switch the Rimage off and on. If the VM does not get the autoloader back, do not flash from that VM. The autoloader disconnects and reconnects when it switches to boot mode, and a VM that loses it at that moment leaves the autoloader waiting in its boot loader. A Windows VM in Docker (QEMU) had exactly this problem; Wine did not.

## Going back

Flash the original `INTEGRA-XX10-6033.bin` the same way.
