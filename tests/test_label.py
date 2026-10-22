from PIL import Image

from rimage2410 import label
from rimage2410.geometry import CANVAS_PX, mm_to_px


def red(size=(1000, 1000)):
    return Image.new("RGB", size, (200, 20, 20))


def px_at(img, mm_from_center):
    c = (CANVAS_PX - 1) / 2
    return img.getpixel((round(c + mm_to_px(mm_from_center)), round(c)))


def test_prepare_size_and_mask():
    out = label.prepare_label(red())
    assert out.size == (CANVAS_PX, CANVAS_PX) and out.mode == "RGB"
    assert px_at(out, 0) == (255, 255, 255)          # centre hole: no ink
    assert px_at(out, 10) == (255, 255, 255)         # inside 24 mm: no ink
    assert px_at(out, 30) == (200, 20, 20)           # in the ring: red
    assert px_at(out, 59.5) == (255, 255, 255)       # outside 118 mm: no ink
    assert out.getpixel((0, 0)) == (255, 255, 255)


def test_print_hub():
    out = label.prepare_label(red(), print_hub=True)
    assert px_at(out, 10) == (200, 20, 20)           # 20 mm diameter is inside the hub print area
    assert px_at(out, 7) == (255, 255, 255)          # 14 mm: inside 17 mm, not printed


def test_contain_leaves_background():
    out = label.prepare_label(red((1000, 200)), fit="contain", background=(0, 0, 255))
    assert px_at(out, 0.0) == (255, 255, 255)
    c = (CANVAS_PX - 1) // 2
    assert out.getpixel((c, c - round(mm_to_px(40)))) == (0, 0, 255)


def test_offset_moves_image():
    img = Image.new("RGB", (1000, 1000), (255, 255, 255))
    img.paste((0, 0, 0), (500, 0, 1000, 1000))     # right half black
    out = label.prepare_label(img, offset_x=-20)
    assert px_at(out, -15) == (0, 0, 0)               # black half moved 20 mm to the left


def test_preview_and_template():
    prev = label.render_preview(label.prepare_label(red()), size=400)
    assert prev.size == (400, 400) and prev.mode == "RGBA"
    assert prev.getpixel((0, 0))[3] == 0               # transparent outside the disc
    assert prev.getpixel((200, 200))[3] == 0           # centre hole transparent
    assert prev.getpixel((200, 80))[3] == 255          # disc itself visible
    assert label.make_template().size == (CANVAS_PX, CANVAS_PX)


def test_analyze_image():
    assert label.analyze_image(red((2000, 2000))) == []
    assert len(label.analyze_image(red((800, 600)))) == 2


def test_other_profile_converted_to_srgb():
    """An embedded profile that is not sRGB is converted and reported."""
    from PIL import Image, ImageCms
    from rimage2410 import artwork
    prof = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
    img = Image.new("RGB", (64, 64), (200, 40, 40))
    img.info["icc_profile"] = prof.tobytes()
    out, notes = artwork.to_srgb(img)
    assert out.mode == "RGB" and notes and notes[0].code == "profile"


def test_preview_canvas_matches_full_size():
    """The screen-size preview must match the full print image, only smaller."""
    import numpy as np
    from PIL import Image
    from rimage2410 import label as lbl
    yy, xx = np.mgrid[0:1200, 0:1600]
    src = Image.fromarray(np.stack([xx % 256, yy % 256, (xx // 7) % 256], -1).astype(np.uint8))
    opts = dict(fit="cover", scale=1.3, offset_x=10, offset_y=-5, rotate=15)
    full = lbl.prepare_label(src, **opts).resize((300, 300), Image.LANCZOS)
    small = lbl.prepare_label(src, **opts, canvas=300)
    diff = np.abs(np.asarray(full, float) - np.asarray(small, float)).mean()
    assert diff < 12, diff
