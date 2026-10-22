import numpy as np

from rimage2410.everest import inspect, job
from rimage2410.robot.__main__ import hid_report
from rimage2410.robot import commands as c


def test_hid_report():
    assert hid_report(c.request_sense().data) == bytes.fromhex("02051b21653143")


def test_inspect_roundtrip():
    planes = {p: np.full((3, 1920), p, np.uint8) for p in (1, 2, 3)}
    data = job.build_job(planes, ribbon="cmy")
    back = inspect.planes_from_job(data)
    assert sorted(back) == [1, 2, 3] and back[2].shape == (3, 1920) and back[2][0, 0] == 2
    assert inspect.preview(data).size == (1889, 3)
    assert inspect.describe(data)[0].startswith("ESC!m2")
