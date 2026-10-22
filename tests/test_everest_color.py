import numpy as np
import pytest

from rimage2410.everest import color
from rimage2410.geometry import CANVAS_PX, LINE_PX


def img(value, h=8):
    return np.full((h, CANVAS_PX, 3), value, dtype=np.uint8)


@pytest.mark.parametrize("ribbon,planes", [("cmy", {1, 2, 3}), ("cmyw", {1, 2, 3, 4}), ("mono", {0})])
def test_simple_shapes(ribbon, planes):
    out = color.convert(img(128), ribbon=ribbon, method="simple")
    assert set(out) == planes
    for a in out.values():
        assert a.dtype == np.uint8 and a.shape == (8, LINE_PX)
        assert not a[:, CANVAS_PX:].any()


def test_simple_white_no_ink():
    out = color.convert(img(255), ribbon="cmyw", method="simple")
    for p, a in out.items():
        body = a.copy()
        body[0, CANVAS_PX - 7:CANVAS_PX] = 0      # stamp
        assert not body.any(), p


def test_simple_black_full_ink():
    out = color.convert(img(0), ribbon="cmy", method="simple")
    assert (out[2][:, :CANVAS_PX] == 255).all()


def test_simple_mirror():
    a = img(255)
    a[:, 0] = (0, 255, 255)                     # left column cyan
    out = color.convert(a, ribbon="cmy", method="simple")
    assert out[1][3, CANVAS_PX - 1] == 255 and out[1][3, 0] == 0


def test_stamp():
    out = color.convert(img(255), ribbon="cmy", method="simple")
    assert list(out[1][0, CANVAS_PX - 7:CANVAS_PX]) == [1, 6, 0, 0, 0, 1, 1]
    out = color.convert(img(255), ribbon="mono", method="simple")
    assert list(out[0][0, CANVAS_PX - 7:CANVAS_PX])[:5] == [1, 6, 0, 0, 0]


def test_tables_load():
    assert color.table("lut_weight_u16x4096", "<u2").shape == (4096,)
    assert color.table("lut3d_mode1_method1").shape == (14739,)


def test_wrong_width():
    with pytest.raises(ValueError):
        color.convert(np.zeros((4, 100, 3), np.uint8), method="simple")


# ---- faithful conversion (method 0 and 2) ------------------------------------

from rimage2410.everest import halftone


@pytest.mark.parametrize("ribbon,method", [("cmy", 0), ("cmy", 2), ("cmyw", 0), ("cmyw", 2), ("mono", 0), ("mono", 2)])
def test_faithful_shapes(ribbon, method):
    out = color.convert(img(100, h=12), ribbon=ribbon, method=method)
    assert set(out) == set(color.PLANES[ribbon])
    for a in out.values():
        assert a.dtype == np.uint8 and a.shape == (12, LINE_PX)
        assert not a[:, CANVAS_PX:].any()


def test_faithful_white_little_black_much():
    w = color.convert(img(255, h=16), ribbon="cmy", method=0)
    b = color.convert(img(0, h=16), ribbon="cmy", method=0)
    for p in (1, 2, 3):
        mid_w = w[p][2:, 900:1000].mean()
        mid_b = b[p][2:, 900:1000].mean()
        assert mid_w < 20            # only the preheat (floor[0] = 20)
        assert mid_b > 5 * mid_w


def test_faithful_deterministic():
    rng = np.random.default_rng(0)
    a = rng.integers(0, 256, (10, CANVAS_PX, 3), dtype=np.uint8)
    o1 = color.convert(a, ribbon="cmyw", method=2)
    o2 = color.convert(a, ribbon="cmyw", method=2)
    for p in o1:
        assert np.array_equal(o1[p], o2[p])


def test_faithful_stamp():
    out = color.convert(img(255, h=4), ribbon="cmy", method=0, colormatch=1)
    assert list(out[1][0, CANVAS_PX - 7:CANVAS_PX]) == [1, 6, 0, 0, 0, 1, 1]
    out = color.convert(img(255, h=4), ribbon="cmy", method=2, colormatch=2, alt=True)
    assert list(out[1][0, CANVAS_PX - 7:CANVAS_PX]) == [1, 6, 0, 0, 0, 2, 2]
    out = color.convert(img(255, h=4), ribbon="mono", method=0)
    assert list(out[0][0, CANVAS_PX - 7:CANVAS_PX]) == [1, 6, 0, 0, 0, 0, 5]


def test_method0_alt_rejected():
    with pytest.raises(ValueError):
        color.convert(img(100, h=4), ribbon="cmy", method=0, alt=True)


def test_median_only_mid_range():
    rows = np.full((5, 5, 3), 100, np.uint8)
    rows[2, 2] = 200                              # outlier in the middle
    rows[2, 0] = 250                              # outside 15..240: left unchanged
    f = halftone.median_filter(rows)
    assert f.shape == (3, 5, 3)
    assert f[1, 2, 0] == 100 and f[1, 0, 0] == 250


def test_colormatch_on_grid_is_lut_copy():
    axis = color.table("lut_axis_u32x256", "<u4")
    on = [v for v in range(256) if (axis[v] & 0xFF) == ((axis[v] >> 8) & 0xFF)]
    v = on[len(on) // 2]
    px = np.array([[[v, v, v]]], dtype=np.uint8)
    out = halftone.colormatch(px, 1, 0)
    lut = color.table("lut3d_mode1_method0")
    lo = int(axis[v] & 0xFF)
    base = ((lo * 17 + lo) * 17 + lo) * 3
    assert list(out[0, 0]) == list(lut[base:base + 3])


def test_no_heat_flag_gives_screen_values():
    out = color.convert(img(0, h=6), ribbon="cmy", method=0, heat=False)
    assert out[1][1:4, 900:1000].max() > 0
