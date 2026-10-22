import wave

import pytest

from rimage2410.burn import BurnError, BurnPlan


def make_wav(path, seconds):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0\0\0\0" * int(44100 * seconds))


def test_iso_plan(tmp_path):
    iso = tmp_path / "a.iso"
    iso.write_bytes(b"\0" * 2048)
    plan = BurnPlan("iso", [str(iso)], str(tmp_path / "w"))
    assert plan.validate() == []
    assert plan.burn_commands("/dev/sr0")[0][:4] == ["xorriso", "-as", "cdrecord", "-v"]


def test_audio_plan_and_toc(tmp_path):
    a, b = tmp_path / "1.wav", tmp_path / "2.wav"
    make_wav(a, 5)
    make_wav(b, 6)
    plan = BurnPlan("audio", [str(a), str(b)], str(tmp_path / "w"), titles=["One", 'Two "x"'],
                    album="Album", artist="Artist")
    assert plan.validate() == []
    toc = plan.toc_text()
    assert toc.startswith("CD_DA") and toc.count("TRACK AUDIO") == 2 and 'Two \\"x\\"' in toc
    assert plan.prepare_commands()[0][0] == "ffmpeg"


def test_audio_too_long(tmp_path, monkeypatch):
    a = tmp_path / "1.wav"
    make_wav(a, 5)
    monkeypatch.setattr("rimage2410.burn.audio_duration_s", lambda p: 81 * 60)
    with pytest.raises(BurnError):
        BurnPlan("audio", [str(a)], str(tmp_path)).validate()


def test_cue_needs_one_cue(tmp_path):
    b = tmp_path / "x.bin"
    b.write_bytes(b"\0")
    with pytest.raises(BurnError):
        BurnPlan("cue", [str(b)], str(tmp_path)).validate()


def test_cdrskin_default_and_sheet(tmp_path):
    a = tmp_path / "1.wav"
    make_wav(a, 5)
    plan = BurnPlan("audio", [str(a)], str(tmp_path / "w"), titles=["One"], album="A", artist="B")
    cmd = plan.burn_commands("/dev/sr0")[0]
    assert cmd[0] == "cdrskin" and "-sao" in cmd and any(x.startswith("input_sheet_v07t=") for x in cmd)
    sheet = plan.v07t_text()
    assert "Album Title = A" in sheet and "Track 01 Title = One" in sheet
    plan2 = BurnPlan("audio", [str(a)], str(tmp_path / "w"), audio_engine="cdrdao")
    assert plan2.burn_commands("/dev/sr0")[0][0] == "cdrdao"


def test_cd_sectors(tmp_path):
    from rimage2410.burn import cd_sectors
    a = tmp_path / "1.wav"
    make_wav(a, 1)          # 44100 frames = 75 sectors
    assert cd_sectors([str(a)]) == 150 + 75


def test_track_shorter_than_4_seconds(tmp_path):
    a = tmp_path / "1.wav"
    make_wav(a, 3)
    with pytest.raises(BurnError, match="at least 4"):
        BurnPlan("audio", [str(a)], str(tmp_path / "w")).validate()


def test_pauses_gapless_and_codes(tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"{i + 1}.wav"
        make_wav(p, 5)
        paths.append(str(p))
    plan = BurnPlan("audio", paths, str(tmp_path / "w"), titles=["A", "B", "C"], artist="Artist",
                    pauses=[0, 0, 3.5], isrc=["", "nl-a1b-26-00002", ""], upc="8712345678906")
    assert plan.validate() == []
    assert [plan.pregap_sectors(i) for i in range(3)] == [0, 0, 262]
    cmd = plan.burn_commands("/dev/sr1")[0]
    assert "--audio_not_raw" in cmd and "-copy" in cmd and "mcn=8712345678906" in cmd
    # sao_pregap and isrc come right before the track they belong to
    i3 = cmd.index(plan._wav_path(2))
    assert cmd[i3 - 1] == "sao_pregap=262"
    assert cmd[cmd.index(plan._wav_path(1)) - 1] == "isrc=NLA1B2600002"
    toc = plan.toc_text()
    assert 'CATALOG "8712345678906"' in toc and "PREGAP 0:03:37" in toc and 'ISRC "NLA1B2600002"' in toc
    sheet = plan.v07t_text()
    assert "ISRC 02 = NLA1B2600002" in sheet and "UPC / EAN = 8712345678906" in sheet
    plan.gapless = True
    assert all(plan.pregap_sectors(i) == 0 for i in range(3))
    assert "sao_pregap" not in " ".join(plan.burn_commands("/dev/sr1")[0])


def test_default_pause_is_two_seconds(tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f"{i + 1}.wav"
        make_wav(p, 5)
        paths.append(str(p))
    plan = BurnPlan("audio", paths, str(tmp_path / "w"))
    assert plan.pregap_sectors(0) == 0 and plan.pregap_sectors(1) == 150


def test_bad_codes(tmp_path):
    a = tmp_path / "1.wav"
    make_wav(a, 5)
    with pytest.raises(BurnError, match="ISRC"):
        BurnPlan("audio", [str(a)], str(tmp_path / "w"), isrc=["XX"]).validate()
    with pytest.raises(BurnError, match="UPC"):
        BurnPlan("audio", [str(a)], str(tmp_path / "w"), upc="123").validate()


def test_cdtext_cleaning(tmp_path):
    from rimage2410.audio import cdtext_clean
    assert cdtext_clean("Café über") == ("Café über", False)
    assert cdtext_clean("Dvořák \u2013 \u201cLargo\u201d") == ('Dvorák - "Largo"', True)
    a = tmp_path / "1.wav"
    make_wav(a, 5)
    w = BurnPlan("audio", [str(a)], str(tmp_path / "w"), titles=["Dvořák"]).validate()
    assert any("Western European" in x for x in w)


def test_fifo_buffers(tmp_path):
    a = tmp_path / "1.wav"
    make_wav(a, 5)
    assert "fs=64m" in BurnPlan("audio", [str(a)], str(tmp_path / "w")).burn_commands("/dev/sr0")[0]
    iso = tmp_path / "a.iso"
    iso.write_bytes(b"\0" * 2048)
    assert "fs=64m" in BurnPlan("iso", [str(iso)], str(tmp_path / "w")).burn_commands("/dev/sr0")[0]
    cmd = BurnPlan("audio", [str(a)], str(tmp_path / "w"), audio_engine="cdrdao").burn_commands("/dev/sr0")[0]
    assert cmd[cmd.index("--buffers") + 1] == "380"          # 64 MB / 176,400 bytes per second


def test_burn_dir_in_ram(tmp_path, monkeypatch):
    from rimage2410 import burn
    a = tmp_path / "1.wav"
    make_wav(a, 5)
    ram = tmp_path / "shm"
    ram.mkdir()
    monkeypatch.setattr(burn, "_mem_available", lambda: 8 * 1024 ** 3)
    monkeypatch.setattr(burn, "RAM_RESERVE", 0)
    d, in_ram = burn.pick_burn_dir("audio", [str(a)], "job1", "/disk", str(ram))
    assert in_ram and d == str(ram / "rimage2410" / "job1")
    assert burn.pick_burn_dir("iso", [str(a)], "job1", "/disk", str(ram)) == ("/disk", False)
    assert burn.pick_burn_dir("audio", [str(a)], "job1", "/disk", None) == ("/disk", False)
    monkeypatch.setattr(burn, "_mem_available", lambda: 1024)      # not enough memory
    assert burn.pick_burn_dir("audio", [str(a)], "job1", "/disk", str(ram)) == ("/disk", False)


def test_useful_output_drops_progress_noise():
    from rimage2410.burn import useful_output
    s = ("cdrskin: working pre-track (burning since 10 seconds)         \r\n"
         "cdrskin: thank you for being patient for 154 seconds                     "
         "Cdrskin: fifo had 24897 puts and 24897 gets.\n"
         "Cdrskin: fifo was 0 times empty and 0 times full, min fill was 75%.\n")
    assert useful_output(s) == ["Cdrskin: fifo had 24897 puts and 24897 gets.",
                                "Cdrskin: fifo was 0 times empty and 0 times full, min fill was 75%."]
