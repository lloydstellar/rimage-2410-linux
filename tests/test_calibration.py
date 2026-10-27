import math

import pytest

from rimage2410 import calibration as cal
from rimage2410.everest import job as ej
from rimage2410.geometry import CANVAS_PX


def test_label_size_and_red_zero_ticks():
    img = cal.alignment_label()
    assert img.size == (CANVAS_PX, CANVAS_PX) and img.mode == "RGB"
    c = (CANVAS_PX - 1) / 2
    for r_mm in (11.5, 59.0):                       # red mark on the nominal edge, at the top
        y = round(c - r_mm / 25.4 * 400)
        px = [img.getpixel((round(c) + dx, y + dy)) for dx in (-3, 0, 3) for dy in (-1, 0, 1)]
        assert any(p[0] > 180 and p[1] < 60 and p[2] < 60 for p in px)


def test_offset_from_readings():
    # print 0.5 mm too high: top -0.5, bottom +0.5, left and right 0
    dx, dy = cal.offset_from_readings({"T": -0.5, "B": 0.5, "L": 0.0, "R": 0.0})
    assert dx == pytest.approx(0.0) and dy == pytest.approx(0.5)
    # 0.3 mm to the right, measured in all 8 directions
    r = {n: -0.3 * math.sin(math.radians(45 * i)) for i, n in enumerate(cal.DIRECTIONS)}
    assert cal.offset_from_readings(r) == pytest.approx((0.3, 0.0))
    with pytest.raises(ValueError):
        cal.offset_from_readings({"T": -0.5, "B": 0.5})


def test_g2_allowed_and_parsed():
    assert ej.status_command("g2") == b"\x1b!g2\x02\x00\x00\x00C"
    bank = bytearray(200)
    bank[0:2] = (15).to_bytes(2, "little")
    bank[6:8] = (-3).to_bytes(2, "little", signed=True)
    bank[0x0E:0x10] = (8).to_bytes(2, "little", signed=True)
    resp = b"\x1b!g21\x02\x00\xc8\x00C" + bytes(bank)
    a = ej.parse_g2(resp)
    assert (a.horizontal, a.vertical_color, a.vertical_mono) == (12, 4.0, 0.0)
    with pytest.raises(ej.JobError):
        ej.parse_g2(b"\x1b!g21\x03" + bytes(205))


def test_g1_still_refused():
    with pytest.raises(ej.JobError):
        ej.status_command("g1")


def test_alignment_shift_and_storage(tmp_path):
    from PIL import Image
    from rimage2410 import alignment
    img = Image.new("RGB", (40, 40), "white")
    img.putpixel((20, 20), (0, 0, 0))
    out = alignment.shift(img, 0.0, -0.3)          # 0.3 mm down = 5 pixels
    assert out.getpixel((20, 25)) == (0, 0, 0) and out.getpixel((20, 20)) == (255, 255, 255)
    assert alignment.shift(img, 0.0, 0.0) is img
    assert alignment.load(str(tmp_path)) == (0.0, 0.0)
    alignment.save(str(tmp_path), 0.0, -0.3)
    assert alignment.load(str(tmp_path)) == (0.0, -0.3)
    with pytest.raises(ValueError):
        alignment.save(str(tmp_path), 0.0, 5.0)


def test_build_print_job_applies_offset(tmp_path):
    from PIL import Image
    from rimage2410 import runner
    from rimage2410.everest import job as ej
    p = tmp_path / "l.png"
    Image.new("RGB", (CANVAS_PX, CANVAS_PX), "white").save(p)
    a = runner.build_print_job(str(p), "cmy", "simple")
    b = runner.build_print_job(str(p), "cmy", "simple", (0.0, -0.3))
    assert len(a) == len(b) and ej.parse_job(b)[-1]["name"] == "S"


def test_plane_shift(tmp_path):
    import numpy as np
    from rimage2410 import alignment
    a = np.zeros((10, 4), np.uint8); a[0] = 7; a[3] = 200
    out = alignment.shift_planes({1: a, 2: a.copy()}, {1: 2})
    assert out[1][5].max() == 200 and out[1][3].max() == 0 and (out[1][0] == 7).all()
    assert (out[2] == a).all()
    alignment.save_plane_shift(str(tmp_path), {1: 2})
    alignment.save(str(tmp_path), 0.0, -0.3)
    assert alignment.load_plane_shift(str(tmp_path)) == {1: 2} and alignment.load(str(tmp_path)) == (0.0, -0.3)


def test_ramp_planes():
    from rimage2410.everest import job as ej
    planes, prev = cal.ramp_planes(plane_shift={1: 2})
    assert set(planes) == {1, 2, 3} and all(p.shape == (CANVAS_PX, 1920) for p in planes.values())
    assert max(int(p.max()) for p in planes.values()) <= cal.RAMP_MAX
    job = ej.build_job(planes, ribbon="cmy", media_in_header=True, include_m2=False, include_end=False)
    assert ej.parse_job(job)[-1]["name"] == "S"
    with pytest.raises(ValueError):
        cal.ramp_planes(energies=(200,))


def test_gain_test_boosts_only_cyan_bands():
    import numpy as np
    from rimage2410.everest import color
    planes, img = cal.gain_test(gains=(1.0, 1.0, 1.0, 1.5), method="simple")
    ref = color.convert(img, ribbon="cmy", method="simple")
    diff = planes[1].astype(int) - ref[1].astype(int)
    assert diff.min() >= 0 and diff.max() > 0
    assert (planes[2] == ref[2]).all() and (planes[3] == ref[3]).all()


def test_plane_gain_with_cap(tmp_path):
    import numpy as np
    from rimage2410 import alignment
    a = np.array([[7, 7, 7, 7], [0, 100, 180, 247]], np.uint8)
    out = alignment.gain_planes({1: a}, {1: 1.2})[1]
    assert out[1].tolist() == [0, 120, 200, 247] and (out[0] == 7).all()
    alignment.save_plane_gain(str(tmp_path), {1: 1.2})
    assert alignment.load_plane_gain(str(tmp_path)) == {1: 1.2}


def test_ramp_planes_mono():
    from rimage2410.everest import job as ej
    planes, prev = cal.ramp_planes_mono()
    assert set(planes) == {0} and planes[0].shape == (CANVAS_PX, 1920)
    assert int(planes[0][1:].max()) <= cal.RAMP_MAX
    job = ej.build_job(planes, ribbon="mono", media_in_header=True, include_m2=False, include_end=False)
    assert ej.parse_job(job)[-1]["name"] == "S"
    with pytest.raises(ValueError):
        cal.ramp_planes_mono(fine=(200,))


def test_grey_label_and_mono_alignment_label_print_on_mono(tmp_path):
    from rimage2410 import runner
    for name, img in (("grey", cal.grey_label()), ("align", cal.alignment_label(mono=True))):
        assert img.size == (CANVAS_PX, CANVAS_PX)
        path = tmp_path / f"{name}.png"
        img.save(path)
        assert len(runner.build_print_job(str(path), "mono")) > 0


def test_gain_test_mono_boosts_only_black():
    from rimage2410.everest import color
    planes, img = cal.gain_test(gains=(1.0, 1.0, 1.0, 1.3), plane=0, method="simple")
    assert set(planes) == {0}
    ref = color.convert(img, ribbon="mono", method="simple")
    diff = planes[0].astype(int) - ref[0].astype(int)
    assert diff.min() >= 0 and diff.max() > 0


def test_gain_test_respects_cap():
    from rimage2410 import alignment
    from rimage2410.everest import color
    planes, img = cal.gain_test(gains=(1.0, 1.1, 1.2, 1.3), plane=0, method=0)
    ref = color.convert(img, ribbon="mono", method=0)[0]
    raised = planes[0] > ref
    assert raised.any() and planes[0][raised].max() <= alignment.GAIN_CAP


def test_calib_print_dry_run_ramp_mono(tmp_path, capsys):
    from rimage2410 import calib_print
    assert calib_print.main(["ramp-mono", "--out", "side", "--dry-run", "--data-dir", str(tmp_path)]) == 0
    work = next((tmp_path / "work").iterdir())
    log = (work / "hid.log").read_text()
    for cmd in ("!f12002C", "!f12601C", "!f12604C", "!f12602C", "!f13001C"):
        assert cmd in log
    assert "job done: 1 good" in capsys.readouterr().out


def test_print_bytes_checks_ribbon(tmp_path):
    from rimage2410 import runner
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path / "d"))   # colour ribbon
    job = runner.DiscJob(job_id="x", print_bytes=b"\x1b%-12345X", ribbon="mono", output_bin=3, reject_bin=None)
    assert runner.run_job(job, loader=loader, printer=printer, burners=burners, log=lambda m: None,
                          workdir=str(tmp_path / "w")) == "failed"


def test_calib_print_image_uses_plane_gain(tmp_path):
    from rimage2410 import alignment, calib_print
    path = tmp_path / "label.png"
    cal.grey_label().save(path)
    work = tmp_path / "w"
    work.mkdir()
    plain, _ = calib_print.build("image", str(tmp_path), str(work), str(path), "mono")
    alignment.save_plane_gain(str(tmp_path), {0: 1.1})
    boosted, ribbon = calib_print.build("image", str(tmp_path), str(work), str(path), "mono")
    assert ribbon == "mono" and plain != boosted and len(plain) == len(boosted)
