# Safety

A disc publisher has a robot arm, a heated printer and two recorder trays that open into the path of the arm. This page explains what the software does to keep the machine safe, and what you should do.

## Dry run by default

Without `--hardware` the web app drives a simulated machine. Nothing is sent over USB. Use dry run to learn the web app, to test artwork and audio, and to see how a job reacts to faults (Machine page, "Simulate a fault").

With `--hardware` the web app drives the real machine. A bar at the top of every page says which mode you are in.

## The approval model

On the real machine:

- **Starting a job is your approval** for every regular move of that job: picking, placing and moving the arm at the bins, the recorders, the printer and the side bin, opening and closing the trays, the label transfer, printer resets and waking up the printer. The job never waits for a question about these, so it keeps running when nobody is at the machine.
- **Any other command** is not sent straight away. The software does not normally send one. The job pauses and a question appears at the top of the job page and the Machine page, with the buttons **Allow** and **Stop job**. Before you click Allow, check that the door is closed and nothing is in the way.
- **Questions about errors stay** (Retry, Reject disc, Stop): then the machine waits until someone chooses.
- **An allowed command is remembered** in `<data dir>/approved_commands.json` and counts as known from then on. To withdraw an approval, stop the web app, remove the command from that file and start the web app again.
- **Outside a job, only read commands are sent.** The Machine page reads the status, but nothing moves. Opening the Machine page never moves the arm.

Every packet sent to the autoloader and the printer, and every approval, is written to `<data dir>/logs/hid-<date>.log`.

## Firmware

The software was verified with autoloader firmware 6.033A. **Do not update the autoloader firmware.** Firmware 6.041 and later only accepts RFID (Encore) ribbons, so ordinary ribbons stop working. Never send firmware update or calibration commands to the autoloader or the printer. The software has no function for either.

## The front door

The eject buttons of the recorders are behind the front door. When the door opens, the autoloader stops the arm by itself. The job then shows "Close the door" and waits without a time limit. When the door is closed again, the software first checks the recorder trays and then continues.

Keep the door closed while a job runs. Take finished discs from the side bin, which you can reach without opening the door.

## Recorder trays

The autoloader cannot see an open recorder tray in the way of the arm. The software prevents collisions in three ways:

- a recorder tray only opens when the arm is already above that recorder;
- after closing a tray, the software waits and checks that it is really closed;
- before every arm move, it checks all recorder trays. If a tray is open that the software did not open itself, it closes it first. If that fails, the arm does not move and you get a question.

Do not open recorder trays by hand, with the eject button or with `eject`, while a job runs.

## Before you start a job

- The door is closed.
- The bins hold what you expect: blank discs in the input bin, room in the output bin or side bin, and in the reject bin.
- The recorder trays are closed and empty, and no disc is in the gripper or the printer.
- Ribbon and retransfer film are in the printer, and the printer cover is closed.
- Only one program talks to the autoloader. Do not run other tools against the machine while the web app runs with `--hardware`.

## If something unexpected happens

1. Click **Stop job** on the job page. If the arm keeps moving in a way you do not expect, open the front door: the autoloader stops the arm.
2. Do not reach into the machine while the arm can still move.
3. Look at what the machine shows: the display, the status light, where the discs are.
4. Read the end of the job log and of `<data dir>/logs/hid-<date>.log`.
5. Remove any disc from the gripper, the printer or a recorder by hand only when the arm has stopped.
6. Start a new job only when you understand what happened. If it looks like a bug, please report it with the log (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

When you click Stop job, a disc that is still burning stays in its recorder. The log says which one.
