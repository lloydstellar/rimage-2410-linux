"""Conversion to CD audio and gapless alignment (rimage2410.audio). Needs ffmpeg; skipped otherwise."""

import shutil
import struct
import wave

import pytest

from rimage2410 import audio as A

needs_ffmpeg = pytest.mark.skipif(not A.tools_available(), reason="ffmpeg/ffprobe not installed")


def make_wav(path, seconds, rate=44100, width=3, value=0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(width)
        w.setframerate(rate)
        n = int(rate * seconds)
        if width == 3:
            sample = struct.pack("<i", value)[:3]
        else:
            sample = struct.pack("<h", value)
        w.writeframes(sample * 2 * n)


@needs_ffmpeg
def test_probe_24bit(tmp_path):
    p = tmp_path / "a.wav"
    make_wav(p, 5)
    info = A.probe(str(p))
    assert info.sample_rate == 44100 and info.bits == 24 and info.channels == 2
    assert abs(info.duration_s - 5) < 0.01
    assert "24 bit" in info.describe() and "dither" in info.conversion()


@needs_ffmpeg
def test_convert_24_to_16_with_dither(tmp_path):
    src, dst = tmp_path / "a.wav", tmp_path / "out.wav"
    make_wav(src, 5, value=1000)
    import subprocess
    subprocess.run(A.convert_command(str(src), str(dst)), check=True)
    with wave.open(str(dst)) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (44100, 2, 2)
        assert w.getnframes() == 5 * 44100


@needs_ffmpeg
def test_convert_96k_to_44k(tmp_path):
    src, dst = tmp_path / "a.wav", tmp_path / "out.wav"
    make_wav(src, 5, rate=96000)
    import subprocess
    subprocess.run(A.convert_command(str(src), str(dst), dither="lipshitz", gain_db=-1), check=True)
    with wave.open(str(dst)) as w:
        assert w.getframerate() == 44100 and abs(w.getnframes() - 5 * 44100) < 50


@needs_ffmpeg
def test_measure_peaks(tmp_path):
    p = tmp_path / "a.wav"
    make_wav(p, 5, value=8_000_000)          # close to full scale (2^23 = 8388608)
    info = A.measure(A.probe(str(p)))
    assert info.sample_peak_db is not None and -1 < info.sample_peak_db < 0
    # around -0.4 dBTP: no warning, only a note (the musician chooses their own ceiling)
    assert not any("dBTP" in w for w in A.track_warnings(info))
    assert any("mp3" in n for n in A.track_notes(info))


def test_true_peak_thresholds():
    info = A.AudioInfo("x.wav", "pcm_s24le", 44100, 2, 24, "s32", 60.0)
    info.sample_peak_db, info.true_peak_db = -0.6, -0.1
    assert A.track_warnings(info) == [] and len(A.track_notes(info)) == 1
    info.true_peak_db = 0.4
    assert any("above full scale" in w and "0.5 dB" in w for w in A.track_warnings(info))
    info.true_peak_db = -1.5
    assert A.track_warnings(info) == [] and A.track_notes(info) == []


def test_gapless_align(tmp_path):
    paths = []
    for i, frames in enumerate([44100 * 5 + 100, 44100 * 5 + 400, 44100 * 5 + 7]):
        p = tmp_path / f"{i}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(b"".join(struct.pack("<hh", (i * 1000 + k) % 30000, 0) for k in range(frames)))
        paths.append(str(p))
    before = b"".join(wave.open(p).readframes(10**9) for p in paths)
    lengths = A.gapless_align(paths)
    after = b"".join(wave.open(p).readframes(10**9) for p in paths)
    assert before == after                                   # no sample lost or added
    assert all(n % A.SAMPLES_PER_SECTOR == 0 for n in lengths[:-1])
    assert sum(lengths) == 3 * 44100 * 5 + 507


def test_msf():
    assert A.msf(150) == "0:02:00" and A.msf(262) == "0:03:37"


@needs_ffmpeg
def test_silence_and_pause_suggestion(tmp_path):
    import subprocess
    paths = []
    for i in range(2):
        p = tmp_path / f"{i}.wav"
        # 5 s tone, then 3 s of silence
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                        "-af", "apad=pad_dur=3", "-ac", "2", "-c:a", "pcm_s24le", str(p)], check=True)
        paths.append(str(p))
    infos = [A.measure(A.probe(p)) for p in paths]
    assert infos[0].lead_silence_s < 0.1 and 2.8 < infos[0].tail_silence_s < 3.1
    assert any("silence" in n for n in A.track_notes(infos[0]))
    assert A.suggested_pause(infos) == 0.0
