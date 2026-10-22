import numpy as np
from PIL import Image

from rimage2410 import label, template


def _composite(skip_hub=False, inner=24.0, outer=118.0):
    layers = template.build_layers(inner, outer)
    comp = Image.new("RGBA", layers[0].image.size, (255, 255, 255, 255))
    for lay in layers:
        if lay.visible and not (skip_hub and lay.name.startswith("GUIDE: hub zone")):
            comp = Image.alpha_composite(comp, lay.image)
    return comp.convert("RGB")


def test_guides_never_print():
    from rimage2410.geometry import MEDIA
    for m in MEDIA.values():
        for hub in (False, True):
            comp = _composite(skip_hub=hub, inner=m["inner"], outer=m["outer"])
            out = np.asarray(label.prepare_label(comp, print_hub=hub, inner_mm=m["inner"],
                                                 outer_mm=m["outer"])).astype(int)
            assert (255 - out.min(axis=2)).max() == 0, (m, hub)


def test_psd_is_readable(tmp_path):
    p = tmp_path / "s.psd"
    template.make_psd_template(str(p))
    im = Image.open(p)
    assert im.format == "PSD" and im.size == (1889, 1889) and im.mode == "RGB"
    names = [lay[0] for lay in im.layers]
    assert names[1] == "Your design" and len(names) == 7
    assert im.info.get("icc_profile")
