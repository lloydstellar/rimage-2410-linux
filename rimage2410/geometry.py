"""Dimensions of the Everest 400 print image.

- The image is 1889 pixels wide and high; each line in a colour plane is 1920 bytes.
- The printer prints at 400 dpi: 1889 px / 400 dpi * 25.4 = 120.0 mm.
- Standard CD/DVD print area: outer diameter 118 mm, inner diameter 24 mm. With hub printing
  the inner diameter goes down to 17 mm.
"""

DPI = 400
CANVAS_PX = 1889          # width and height of the image for a CD
LINE_PX = 1920            # width of one line in a colour plane
DISC_MM = 120.0           # diameter of a CD
HOLE_MM = 15.0            # centre hole
PRINT_OUTER_MM = 118.0    # default outer diameter of the printable area
PRINT_INNER_MM = 24.0     # default inner diameter of the printable area

# Print area per disc type (inner and outer diameter in mm). The print runs slightly past the
# edge of the printable layer (bleed), so no thin white edge stays visible.
MEDIA = {
    # 0.5 mm bleed all round: the alignment is accurate to about 0.1 mm
    "falcon": {"name": "FalconMedia Diamond CD-R (white from 23 to 118 mm)", "inner": 22.0, "outer": 119.0},
    "rimage": {"name": "Rimage standard (24 to 118 mm)", "inner": 24.0, "outer": 118.0},
}
DEFAULT_MEDIA = "falcon"
MEDIA_LIMITS = {"inner": (16.0, 46.0), "outer": (100.0, 119.5)}
HUB_INNER_MM = 17.0       # inner diameter when the hub is printed too ("Print On Hub")


def mm_to_px(mm: float) -> float:
    """Millimetres to pixels at 400 dpi."""
    return mm / 25.4 * DPI


def px_to_mm(px: float) -> float:
    """Pixels at 400 dpi to millimetres."""
    return px * 25.4 / DPI
