# Using the web app

The web app has four pages: **New job**, **Releases**, **Queue** and **Machine**. A bar at the top shows whether you are in **Dry run** (nothing is sent to the machine) or on the **Real machine**.

## The pages

**New job** is where you put a disc together, in three cards:

1. **Label.** Upload an image (PNG, JPEG, TIFF, BMP, WebP, GIF or a Photoshop PSD; for a PSD the flattened image is used). The preview shows the printed disc: silver where nothing is printed, with the centre hole. You can choose Fill or Fit, scale, move, rotate, set a background colour and print the hub. Choose the disc type to set the print area.
2. **Burn.** One ISO file for a data disc, audio files for an audio CD, or a BIN/CUE pair. Leave it empty to print only.
3. **Job.** Name, number of copies (1 to 50), ribbon, input bin, output and reject bin. Here you also save the disc as a release.

**Releases** lists saved releases. See [Releases](#releases).

**Queue** lists all jobs with a preview, type and status. Open a job to see its progress, the live log, and the image after colour conversion (as the printer receives it). You can stop or delete a job there.

**Machine** shows what the machine is doing during a job. Outside a job it shows the carousel, gripper, recorder trays, printer, heat roller temperature, and the ribbon and retransfer film (type and prints left). On the real machine this is read at most every 5 seconds, and only while no job is running. In dry run you can set up a fault here to see how a job reacts.

## Your first job

1. **Load blank discs.** Open the front door. Use the carousel button on the control panel behind the front door to turn the bin you want to the front. Put the blank discs in with the recording side down, so the printable side faces up. Close the door.
2. **Choose artwork.** On New job, upload your image. A square image of 1889 x 1889 pixels is exactly 120 mm at 400 dpi, the resolution of the Everest 400. You can download a Photoshop or PNG template with the print area for the selected disc type.
3. **Check the label.** Look at the preview and the checks below it. They tell you about low resolution (below 300 ppi a note, below 200 ppi a warning), colour profile conversions, details that fall in the centre or outside the print area, transparency and large white areas. White is not ink: on a white printable disc you see the disc surface there, on a silver disc you see silver.
4. **Choose the disc type.** The print runs slightly past the white coating (bleed), so no thin white edge stays visible. For a disc type that is not in the list, measure where the white starts and ends and choose Custom size.
5. **Add audio tracks** (for an audio CD). See [Audio CDs](#audio-cds).
6. **Set the copies, ribbon and bins.** Ribbon "Automatic" uses whatever ribbon is in the printer and stops the job if there is none or it is not recognised. Output "Automatic: side bin for 5 or fewer, otherwise bin 2" puts small runs in the side bin, where you can take them out without opening the door. Rejected discs always go to the reject bin.
7. **Add to queue.** The job starts when the machine is free. Open it from the Queue to follow it.
8. **Answer questions.** If something needs you, a question appears at the top of the job page and the Machine page. While a question is open, the arm stands still. A recorder that is already burning finishes its disc.

Typical questions:

| Situation | What to do | Buttons |
| --- | --- | --- |
| Door open | Close the door; the job continues by itself | none |
| Input bin empty | Fill the bin | Retry, Stop job |
| Picking from the bin failed | Check that the discs lie flat | Retry, Stop job |
| Recorder tray does not open or close | Check that the disc lies correctly | Retry, Stop job |
| Clamp error in the printer (after two automatic retries) | Check the disc and the printer tray | Retry, Reject disc, Stop job |
| Output bin or side bin full | Empty it | Retry, Stop job |
| Printer cover open | Close the cover; the job continues by itself | none |
| Ribbon or film used up | Replace the ribbon or the film | Retry, Stop job |
| Printing failed | Look at the disc: is the label fine anyway? | Yes, No (No sends it to the reject bin) |
| New command for the machine | See [safety.md](safety.md) | Allow, Stop job |

A burn failure needs no answer: that disc goes to the reject bin without a label and the job continues.

You can also print only (no source) or burn only (no image).

## Audio CDs

Choose one or more audio files: WAV, FLAC, MP3, OGG, M4A or AIFF. 24-bit and high sample rate masters are fine. When you upload, each track is measured: format, true peak and loudness. You get a warning for tracks shorter than 4 seconds and for peaks above 0 dBFS or a true peak above 0 dBTP.

Per track you set the order, title, a different artist if needed (CD-TEXT), the pause before the track (0 to 10 seconds, default 2; the first track always starts after 2 seconds) and the ISRC. Titles are filled in from file names such as `01 Artist - Title`.

For the whole disc: album and artist (CD-TEXT), default pause, gapless (for live albums and mixes; track boundaries are placed on CD frame boundaries), dither type, an optional volume reduction, write speed, copy bit and UPC/EAN. If every track already ends with 2 seconds of silence or more, the page suggests a pause of 0 seconds. The total playing time includes the pauses; you get a warning above 74 minutes and an error above 80.

When burning, audio is converted once to 44.1 kHz, 16 bit with ffmpeg and the soxr resampler, with TPDF dither by default. Burning uses cdrskin in disc-at-once mode with CD-TEXT. 16x is the default and is recommended for discs that must play in old CD players. You can choose 8x up to the recorder maximum.

Only fill in UPC and ISRC with real codes, for example from your distributor.

## Releases

A release keeps everything for one CD in a single directory, `<data dir>/releases/<id>/`: the original artwork, the original audio files, the track list with titles, pauses and ISRC codes, the label settings including the disc type, the audio options, and the default copies, ribbon and bins.

Save a release from New job with **Save as release**. On the Releases page you can then:

- choose a number of discs and click **Make**; the job goes straight into the queue;
- **Edit**: loads everything back into New job, where you can update the release or save it as a new one;
- **Download (zip)** the release directory;
- **Delete** it.

This is meant for print on demand: when you need five more copies of a CD, open Releases and make them.

## How a batch is scheduled

The arm can do one thing at a time. Burning takes much longer than moving a disc, so the software keeps both recorders busy:

```
arm:        load 1 -> R1   load 2 -> R2   ...   1 out of R1, 1 into printer, load 3 -> R1, transfer, 1 away   2 out of R2 ...
recorder 1: [------ burns disc 1 ------]                       [------ burns disc 3 ------]
recorder 2:                [------ burns disc 2 ------]                                    ...
printer:    label 1 onto film                  prints disc 1, label 2 onto film            prints disc 2 ...
```

- Both recorders burn at the same time.
- A finished disc waits in its recorder until the arm and the printer are free.
- As soon as a disc leaves a recorder, that recorder gets the next blank disc and starts burning again.
- The printer first prints the label onto the retransfer film, then transfers it to the disc. The next label goes onto the film while discs are still burning, so the printer is ready when the next disc arrives.
- A disc that fails to burn goes straight to the reject bin without a label. The label stays on the film for the next disc.
- Data discs are verified by reading them back after burning.

Before every arm move the software reads the machine status and checks the recorder trays. Before and after every pick or place it checks whether there is a disc in the gripper.

The parallel schedule has been tested in dry run. On the real machine, one-disc jobs have been tested; batches with both recorders at once are still waiting for a test report.

## Typical timings

Measured on a Rimage Professional 3410 with FalconMedia CD-R discs:

| Step | Time |
| --- | --- |
| Burning a 31 minute audio CD at 16x | about 2.5 to 3 minutes |
| Sending the print job to the printer (about 11 MB) | about 2.5 seconds |
| Printing the label onto the film | about 42 seconds |
| Transferring the label to the disc | about 21 seconds |
| One disc through the printer (load, transfer, unload) | about 1.5 minutes |
| A one-disc job: burn the 31 minute audio CD, print, side bin | about 4.5 minutes |

Once, printing the label onto the film took more than 3 minutes instead of 42 seconds. The cause is not known; the next prints were normal again.

Before a transfer the printer waits until the heat roller is hot enough. After the machine has been off or the printer cover has been open, the first disc can take longer.
