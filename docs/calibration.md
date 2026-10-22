# Calibration

Every Everest 400 prints a little differently. This software never changes the settings stored in the printer. Instead it corrects the image just before it builds the print job, using the file `<data dir>/printer.json`. Without that file, no correction is applied.

```json
{
  "offset_x_mm": 0.0,
  "offset_y_mm": 0.0,
  "plane_shift_lines": {},
  "plane_gain": {}
}
```

| Key | Meaning | Range |
| --- | --- | --- |
| `offset_x_mm` | moves the whole image to the right (positive) or left (negative), in mm | -2 to 2 |
| `offset_y_mm` | moves the whole image up (positive) or down (negative), in mm, as seen on the artwork | -2 to 2 |
| `plane_shift_lines` | moves one colour plane down (positive) or up (negative), in lines of 0.0635 mm. Keys: `"1"` = cyan, `"2"` = magenta, `"3"` = yellow | -16 to 16 |
| `plane_gain` | multiplies the print energy of one colour plane. Same keys as above | 0.5 to 1.5 |

The image is moved in whole pixels (0.0635 mm at 400 dpi). The file is read again for every job, so you do not need to restart the web app after editing it.

As an example, one printer needed this:

```json
{
  "offset_x_mm": 0.0,
  "offset_y_mm": -0.3,
  "plane_shift_lines": {"1": 1},
  "plane_gain": {"1": 1.2}
}
```

That is: the image 0.3 mm down, cyan 1 line down and cyan energy times 1.2. These are not defaults. Measure your own printer.

The plane keys apply to the colour ribbon. With the black ribbon only the image offset is used.

## Calibration discs

The module `rimage2410.calibration` generates three test discs:

| Disc | Function | What it shows |
| --- | --- | --- |
| Alignment label | `alignment_label()` | where the print sits on the disc, plus colour patches |
| Colour ramp | `ramp_planes()` | at which energy each colour starts to print |
| Gain test | `gain_test()` | the same colour patches with four different gains for one colour |

Use blank printable discs of the type you normally print on. Take sharp photos in daylight, straight from above, and keep the original files. Messaging apps shrink photos so much that you cannot read 0.1 mm any more.

## Alignment

### Print the alignment label

Create the image (run this in the repository directory, with the virtual environment):

```bash
.venv/bin/python -c "from rimage2410.calibration import alignment_label; alignment_label().save('alignment.png')"
```

The label has a ruler at 8 positions around the disc, every 45 degrees, labelled T, TR, R, BR, B, BL, L and TL (top, top right, and so on, clockwise; "TOP" marks the top of the artwork). Each position has a ruler across the inner edge and one across the outer edge of the white printable coating. The ticks are 0.25 mm apart: long ticks are whole millimetres, medium ticks half millimetres. The red tick (0) lies exactly on the nominal edge of the white coating, by default 23 mm (inner) and 118 mm (outer). For discs with other sizes, pass them: `alignment_label(inner_mm=..., outer_mm=...)`. Behind the rulers a light colour covers the normal print area, so you also see any white edges as you would on a real label.

Print it like a normal label:

1. Make sure `printer.json` has no offset, or write down the values you have now.
2. New job: upload `alignment.png`, keep Fill, scale 100% and no movement or rotation.
3. Disc type: choose Custom size with the widest values (inner 16 mm, outer 119.5 mm). Otherwise the print area cuts off the outer part of the rulers.
4. No burn source, 1 copy, the colour ribbon. Add to queue.

### Read it

At each ruler, read the tick where the white of the disc starts (inner ruler) or ends (outer ruler). Count from the red tick: plus is away from the centre, minus is towards the centre.

If the print is shifted by d mm in one direction, you read -d in that direction and +d on the opposite side. So the shift towards a side is (reading on the opposite side - reading on this side) / 2. Inner and outer rulers should agree.

Example: T reads -0.45, B reads +0.2, R reads 0 and L reads +0.1. The print sits (0.2 - (-0.45)) / 2 = about 0.3 mm too high, and horizontally about 0.05 mm, which is within what you can read from a photo. The correction is the opposite of the shift: `"offset_y_mm": -0.3`.

The module can do the sum for you from any set of readings (least squares over all directions you give):

```bash
.venv/bin/python -c "from rimage2410.calibration import offset_from_readings as f; print(f({'T': -0.45, 'B': 0.2, 'L': 0.1, 'R': 0.0}))"
```

It prints the shift of the print as (x to the right, y up) in mm. Put the opposite values in `printer.json`. If you already had a correction, add the new one to it. Then print the label again to check.

A white edge at one side of a real label usually means the alignment is off by more than the bleed of the selected disc type.

## Colour plane registration

The colour ribbon prints cyan, magenta and yellow one after the other. If one plane lands a little higher or lower than the others, black text gets coloured edges. Look at black letters, for example on the alignment label:

- a blue edge at the top and a red edge at the bottom of black letters: cyan sits too high. Move cyan down with a positive value, `"plane_shift_lines": {"1": 1}`;
- a red edge at the top and a blue edge at the bottom: cyan sits too low. Use a smaller or negative value.

One line is 0.0635 mm. On the tested printer the position of cyan varied by about 0.1 mm from one print to the next, so a fixed value can only remove the average. Judge it over several prints and do not chase a single one.

## Colour strength

### What the printer does with energy

The Everest prints each colour plane with a value (energy) per pixel. The printer behaves almost like a switch: energies below about 96 print nothing, 96 prints grainy, and 112 and above print solid. Light and middle tones are therefore made of small dots. Small dots of one colour can come out weak, which makes colours drift. On the tested printer this happened with cyan: blue looked purple, navy purplish, grey slightly pink, and pure cyan almost white.

### Colour ramp

`ramp_planes()` prints rows of patches with a fixed energy, bypassing the colour conversion: one row each for cyan, magenta and yellow, and one row with all three together (which should look grey). The energies are 16, 32, 48, 64, 80, 96, 112, 128 and 144. Nothing above 144 is sent; that is the highest value the colour conversion uses for full black.

Read it: at which number does each colour start to show, and where is it solid? If one colour starts later than the others, that colour needs more gain.

### Gain test

`gain_test()` prints four bands with the same colour patches as the alignment label (black, three greys, blue, navy, cyan, magenta, yellow, red, green and sky blue). In each band one colour plane, cyan by default, gets a different gain: x1.00, x1.15, x1.30 and x1.50. Pick the band where grey looks neutral and blue looks blue.

On the tested printer x1.15 to x1.30 gave the best grey and blue, and x1.50 damaged the dark patches (white spots, brownish black). The gain test applies its gains without a limit, so that last band shows what too much looks like.

Then set the gain, for example `"plane_gain": {"1": 1.2}`. The `plane_gain` setting has a safety cap: a pixel is never raised above an energy of 200 by the gain, and pixels that are already higher stay as they are.

You can also tune gain without the gain test: set a value, print the alignment label (it has the same colour patches) and compare.

### Printing the ramp and the gain test

These two discs are built directly as colour planes, so you cannot upload them as an image in New job. The web app has no button for them yet. The functions return the planes and a preview image. You can look at the preview and build the print job file like this:

```bash
.venv/bin/python -c "
from rimage2410.calibration import ramp_planes
from rimage2410.everest.job import build_job
planes, preview = ramp_planes()
preview.save('ramp-preview.png')
open('ramp.prn', 'wb').write(build_job(planes, ribbon='cmy', media_in_header=True, include_m2=False, include_end=False))
"
```

`gain_test()` works the same way. Both accept `offset_mm=(x, y)` and `plane_shift={...}`, so they can use your alignment corrections. Getting such a job onto a disc needs the same steps as a normal job (send the job, load a disc into the printer, transfer, unload). Adding these discs to the web app would be a welcome contribution.
