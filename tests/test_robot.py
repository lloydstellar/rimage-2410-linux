import pytest

from rimage2410.hardware import DryRunLoader, SimState
from rimage2410.robot import commands as c
from rimage2410.robot.sequence import Stopped, LoaderError, Robot, Timing
from rimage2410 import runner


def fast_robot(state=None, logs=None):
    state = state or SimState()
    logs = [] if logs is None else logs
    loader = DryRunLoader(state, logs.append)
    return Robot(loader, logs.append, timing=Timing(poll_s=0, printer_ready_s=0.01,
                                                     preprint_s=0.01, roller_warm_s=0.01, print_done_s=0.01),
                 sleep=lambda _s: None), state, logs


def test_command_bytes():
    assert c.request_sense().data == b"\x1b!e1C"
    assert c.position_medium(c.Element.BIN_1, c.Action.PICK).data == b"\x1b!f12002C"
    assert c.home().data == b"\x1b!f12600C"
    assert c.open_close_element(c.Element.PRINTER, True).data == b"\x1b!f22601C"
    assert c.start_retransfer().data == b"\x1b!f12604C"
    assert c.fan_control(True).data == b"\x1b!e22C"
    assert c.fan_control(False).data == b"\x1b!e20C"
    assert c.read_element_status(200).data == b"\x1b!f0200C"


@pytest.mark.parametrize("raw", [b"\x1b!f6000C", b"\x1b!f7000C", b"\x1b!f9099C011", b"\x1b!g5C",
                                 b"\x1b!g1C", b"\x1b!h1C", b"\x1b!k8012345678;", b"\x1b!kb1C",
                                 b"\x1b!k6x", b"\x1b!b38400C", b"e1C", b"\x1b!z000" + bytes(32) + b"C"])
def test_blocked(raw):
    with pytest.raises(c.BlockedCommand):
        c.check_allowed(raw)


def test_open_close_only_printer():
    with pytest.raises(ValueError):
        c.open_close_element(200, True)


def test_parsers():
    cmd = c.request_sense()
    assert c.parse_sense(cmd, b"\x1b!e1002009C") == c.Sense(2, 9)
    cmd = c.read_element_status(240)
    st = c.parse_element_status(cmd, b"\x1b!f01300244C")
    assert st.ok and st.occupied and st.value == 244
    cmd = c.inquiry()
    inq = c.parse_inquiry(cmd, b"\x1b!e032CLIBERTY 4       VERSION 6.014E  ")
    assert inq.length == 32 and inq.text.startswith("LIBERTY 4")
    with pytest.raises(c.ResponseError):
        c.parse_simple(c.home(), b"\x1b!e11C")


def test_pick_place_and_errors():
    robot, state, _ = fast_robot()
    robot.pick(200)
    assert state.gripper and state.bins[200] == 24
    robot.place(201)
    assert not state.gripper and state.bins[201] == 1
    state.bins[202] = 0
    with pytest.raises(LoaderError):
        robot.pick(202)


def test_door_open_waits_until_closed():
    # waits without a time limit and continues as soon as the door is closed
    robot, state, logs = fast_robot()
    notes = []
    robot.notice = notes.append
    state.door_open, state.door_polls = True, 5
    robot.pick(200)
    assert state.gripper and not state.door_open
    assert notes[0].code == "door" and notes[-1] is None


def test_door_open_can_be_stopped():
    robot, state, _ = fast_robot()
    state.door_open = True
    polls = {"n": 0}

    def stop():
        polls["n"] += 1
        return polls["n"] > 20
    robot.should_stop = stop
    with pytest.raises(Stopped):
        robot.pick(200)


def test_full_job_dry_run(tmp_path, monkeypatch):
    logs = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path), log=logs.append)
    iso = tmp_path / "test.iso"
    iso.write_bytes(b"\0" * 4096)
    label = tmp_path / "label.png"
    label.write_bytes(b"png")
    monkeypatch.setattr(runner, "build_print_job", lambda *a, **k: b"\x1b&e" + b"\0" * 13)
    job = runner.DiscJob("t1", copies=2, source_kind="iso", source_paths=[str(iso)],
                         label_path=str(label), input_bin=1, output_bin=2, reject_bin=3)
    timing = Timing(poll_s=0, printer_ready_s=1, preprint_s=1, roller_warm_s=1, print_done_s=1)
    result = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=logs.append,
                            timing=timing, workdir=str(tmp_path / "w"), sleep=lambda _s: None)
    assert result == "done", "\n".join(logs)
    st = loader.state
    assert st.bins[200] == 23 and st.bins[201] == 2 and not st.gripper and not st.printer_disc
    assert len(list((tmp_path / "printjobs").iterdir())) == 2


def test_stop_request(tmp_path):
    logs = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path))
    job = runner.DiscJob("t2", copies=1)
    result = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=logs.append,
                            should_stop=lambda: True, workdir=str(tmp_path / "w"))
    assert result == "stopped"


def test_temperature_parser():
    cmd = c.everest_temperature()
    assert c.parse_temperature(cmd, b"\x1b!f<13+0025;+0180;+0030;C") == [25, 180, 30]


def test_temperature_parser_firmware_format():
    # the other response format the firmware can send, with '3' at byte 5
    cmd = c.everest_temperature()
    assert c.parse_temperature(cmd, b"\x1b!f<03C+00040;+00158;+00030;") == [40, 158, 30]


def test_wait_roller_warm_reports_temperature():
    robot, state, _ = fast_robot()
    state.roller_temp = 25
    seen = []
    robot.wait_roller_warm(seen.append)
    assert seen == sorted(seen) and seen[0] < 155 <= seen[-1]
    assert len(seen) > 2


def test_roller_cools_in_standby():
    robot, state, _ = fast_robot()
    robot.printer_standby()
    assert state.roller_temp == 25


def test_locate_drive():
    from rimage2410.hardware import DryRunBurner
    robot, state, logs = fast_robot()
    b1, b2 = DryRunBurner(state, 241, "/dev/sr0"), DryRunBurner(state, 242, "/dev/sr1")
    robot.burners = [b1, b2]
    assert robot.locate_drive(b2) == 242 and robot.locate_drive(b1) == 241
    assert not state.drives[241]["tray_open"] and not state.drives[242]["tray_open"]
    assert any("position of the open tray" in l for l in logs)


def test_guard_closes_unexpected_open_tray():
    from rimage2410.hardware import DryRunBurner
    robot, state, logs = fast_robot()
    b1, b2 = DryRunBurner(state, 241, "/dev/sr0"), DryRunBurner(state, 242, "/dev/sr1")
    robot.burners = [b1, b2]
    state.drives[242]["tray_open"] = True          # someone opened the tray
    robot.pick(200)
    assert not state.drives[242]["tray_open"]
    assert any("safety: tray of /dev/sr1" in l for l in logs)


def test_guard_asks_when_tray_stays_open():
    from rimage2410.hardware import DryRunBurner
    robot, state, logs = fast_robot()
    b1 = DryRunBurner(state, 241, "/dev/sr0")
    robot.burners = [b1]
    state.drives[241]["tray_open"] = True
    state.faults["tray_stuck"] = 1
    asked = []
    robot.ask = lambda p: asked.append(p) or "retry"
    robot.pick(200)
    assert asked and asked[0].code == "tray_close" and not state.drives[241]["tray_open"]


def test_intended_open_tray_is_allowed():
    # while loading, the tray of the target recorder is open on purpose; the guard must not close it
    from rimage2410.hardware import DryRunBurner
    robot, state, logs = fast_robot()
    b1, b2 = DryRunBurner(state, 241, "/dev/sr0"), DryRunBurner(state, 242, "/dev/sr1")
    robot.burners = [b1, b2]
    robot.load_drive(b1, 200)
    assert state.drives[241]["disc"] and not state.drives[241]["tray_open"]
    assert not any("safety" in l for l in logs)


@pytest.mark.parametrize("with_label", [False, True])
def test_parallel_two_drives(tmp_path, monkeypatch, with_label):
    logs = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path), log=logs.append, burn_s=0.05)
    iso = tmp_path / "t.iso"
    iso.write_bytes(b"\0" * 2048)
    label = None
    if with_label:
        label = tmp_path / "l.png"
        label.write_bytes(b"x")
        monkeypatch.setattr(runner, "build_print_job", lambda *a, **k: b"\x1b!m2\x01\x000\x00C\x1b&e" + b"\0" * 13)
    job = runner.DiscJob("p", copies=5, source_kind="iso", source_paths=[str(iso)],
                         label_path=str(label) if label else None)
    timing = Timing(poll_s=0, printer_ready_s=1, preprint_s=1, roller_warm_s=1, print_done_s=1)
    res = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=logs.append,
                         timing=timing, workdir=str(tmp_path / "w"), sleep=lambda _s: None)
    assert res == "done", "\n".join(logs[-30:])
    st = loader.state
    assert st.bins[200] == 20 and st.bins[201] == 5 and not st.gripper
    assert not any(d["disc"] for d in st.drives.values())
    devices = {l.split(" to ")[-1].split(" ")[0] for l in logs if l.startswith("--- disc")}
    assert devices == {"/dev/sr0", "/dev/sr1"}
    if with_label:
        assert len(list((tmp_path / "printjobs").iterdir())) == 5


@pytest.mark.parametrize("code,requested,expect", [("2", "auto", "done"), ("2", "cmy", "failed"),
                                                    ("0", "cmy", "done"), ("E", "auto", "failed")])
def test_ribbon_check(tmp_path, monkeypatch, code, requested, expect):
    logs = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path), log=logs.append)
    loader.state.ribbon_code = code
    seen = {}
    def fake_build(path, ribbon, method=0, offset_mm=(0.0, 0.0), plane_shift=None,
                   plane_gain=None):
        seen["ribbon"] = ribbon
        return b"\x1b&e" + b"\0" * 13
    monkeypatch.setattr(runner, "build_print_job", fake_build)
    label = tmp_path / "l.png"
    label.write_bytes(b"x")
    job = runner.DiscJob("r", copies=1, label_path=str(label), ribbon=requested)
    res = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=logs.append,
                         timing=Timing(poll_s=0, printer_ready_s=1, preprint_s=1, roller_warm_s=1, print_done_s=1),
                         workdir=str(tmp_path / "w"), sleep=lambda _s: None)
    assert res == expect, "\n".join(logs)
    if expect == "done":
        assert seen["ribbon"] == ("mono" if code == "2" else "cmy")


def test_progress_batch_of_ten(tmp_path, monkeypatch):
    from rimage2410.progress import Tracker
    snaps = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path), burn_s=0.02)
    iso = tmp_path / "t.iso"
    iso.write_bytes(b"\0" * 2048)
    label = tmp_path / "l.png"
    label.write_bytes(b"x")
    monkeypatch.setattr(runner, "build_print_job", lambda *a, **k: b"\x1b&e" + b"\0" * 13)
    tr = Tracker(10, on_change=snaps.append, printing=True)
    job = runner.DiscJob("b", copies=10, source_kind="iso", source_paths=[str(iso)], label_path=str(label))
    res = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=lambda _m: None,
                         timing=Timing(poll_s=0, printer_ready_s=1, preprint_s=1, roller_warm_s=1, print_done_s=1),
                         workdir=str(tmp_path / "w"), sleep=lambda _s: None, progress=tr)
    assert res == "done"
    final = tr.snapshot()
    assert final["phase"] == "done" and final["done"] == 10 and final["rejected"] == 0
    assert all(d["stage"] == "done" and d["where"] == "bin 2" for d in final["discs"])
    assert set(final["drives"]) == {"/dev/sr0", "/dev/sr1"}
    assert all(d["state"] == "idle" for d in final["drives"].values())
    # at some point both recorders were burning at the same time
    assert any(all(d["state"] == "burning" for d in s["drives"].values()) for s in snaps if len(s["drives"]) == 2)
    # and the printer printed discs
    assert any(s["printer"]["state"] == "printing (retransfer)" for s in snaps)


def test_trays_checked_right_after_door_closes():
    # the eject buttons are behind the door: check the trays right after the door was open
    from rimage2410.hardware import DryRunBurner
    robot, state, logs = fast_robot()
    b1 = DryRunBurner(state, 241, "/dev/sr0")
    robot.burners = [b1]
    state.door_open, state.door_polls = True, 3
    state.drives[241]["tray_open"] = True          # someone pressed the button with the door open
    robot.wait_door_closed()
    assert not state.drives[241]["tray_open"]
    text = "\n".join(logs)
    assert text.index("door closed; checking recorder trays") < text.index("safety: tray of /dev/sr0")
