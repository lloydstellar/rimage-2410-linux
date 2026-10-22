import numpy as np
import pytest

from rimage2410.everest import job


def planes(ribbon, rows=4):
    nums = job.RIBBONS[ribbon][2]
    return {p: np.full((rows, 1920), p * 10, np.uint8) for p in nums}


def test_block_layout():
    b = job.block(0x41, b"\x01\x02")
    assert len(b) == 16 and b[:3] == b"\x1b&A" and b[3:5] == b"\x01\x02" and b[5:15] == bytes(10) and b[15:] == b"P"


def test_header_exact_bytes():
    hdr = job.header_blocks(job.JobParams(width=1889, height=1889, quality=1, ribbon="cmy", copies=2))
    blocks = [hdr[i:i + 16] for i in range(0, len(hdr), 16)]
    assert [b[2] for b in blocks] == [0x65, 0x41, 0x4D, 0x45, 0x4C, 0x55, 0x52, 0x46]
    a = blocks[1]
    assert a[3:5] == (1889).to_bytes(2, "little") and a[5:7] == (1889).to_bytes(2, "little") and a[7] == 0
    assert blocks[2][3] == 1
    assert blocks[5][3] == 2                      # ESC&U: CMY
    assert blocks[6][3:5] == (1).to_bytes(2, "little")   # ESC&R: copies - 1


def test_media_in_header_variant():
    hdr = job.header_blocks(job.JobParams(media_in_header=True))
    assert hdr[16 + 7] == ord("0")


def test_m2():
    assert job.m2_command(job.JobParams(quality=0x11, media="0")) == b"\x1b!m2\x11\x000\x00C"


@pytest.mark.parametrize("ribbon,u", [("cmy", 2), ("cmyw", 3), ("mono", 0)])
def test_build_and_parse_roundtrip(ribbon, u):
    data = job.build_job(planes(ribbon), ribbon=ribbon)
    parsed = job.parse_job(data)
    assert parsed[0]["kind"] == "m2"
    blocks = [p for p in parsed if p["kind"] == "block"]
    assert blocks[5]["code"] == 0x55 and blocks[5]["args"][0] == u
    s = [p for p in blocks if p["code"] == 0x53]
    assert [p["plane"] for p in s] == list(job.RIBBONS[ribbon][2])
    assert [p["last"] for p in s] == [False] * (len(s) - 1) + [True]
    assert all(p["length"] == 4 * 1920 for p in s)
    assert blocks[-1]["code"] == 0x0C


def test_plane_validation():
    with pytest.raises(job.JobError):
        job.build_job(planes("cmy"), ribbon="cmyw")
    bad = planes("cmy")
    bad[2] = bad[2].astype(np.int16)
    with pytest.raises(job.JobError):
        job.build_job(bad, ribbon="cmy")


def test_status_commands_whitelist():
    assert job.status_command("r3") == b"\x1b!r3C"
    for forbidden in ("g1", "m5", "i3", "r0", "d2"):
        with pytest.raises(job.JobError):
            job.status_command(forbidden)


def test_parse_r3():
    st = job.parse_r3(b"\x1b!r37C0001000")
    assert st.state == "0" and st.ribbon == "0" and st.film == "1" and st.error == "00"
    assert "Ready" in st.text and "CMY" in st.text


def test_ribbon_and_consumables():
    st = job.parse_r3(b"\x1b!r37C0021000")
    assert job.ribbon_from_status(st) == "mono"
    assert job.ribbon_from_status(job.parse_r3(b"\x1b!r37C00E1000")) is None
    import struct
    resp = b"\x1b!r613C" + struct.pack("<IIBHH", 1234, 99, 82, 437, 0xFFFF)
    c = job.parse_r6(resp)
    assert c.counter_a == 1234 and c.film_percent == 82 and c.ribbon_remaining == 437 and c.film_value is None
    assert c.ribbon_percent("cmy") == 87
