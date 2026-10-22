"""The full chain of a job: preloading, burn failures, clamp errors, operator questions and
printer recovery. Everything runs against the simulated machine."""

import pytest

from rimage2410 import runner
from rimage2410.everest import job as ej
from rimage2410.progress import Tracker
from rimage2410.robot.sequence import Timing

FAST = Timing(poll_s=0, printer_ready_s=1, preprint_s=1, roller_warm_s=1, print_done_s=1,
              retransfer_start_s=1, tray_settle_s=0, tray_recheck_s=0)
JOB_BYTES = b"\x1b&e" + b"\0" * 13


def run(tmp_path, monkeypatch, *, copies=3, source=True, label=True, faults=None, ask=None,
        bins=None, parallel=True, setup=None):
    logs = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path), log=logs.append)
    st = loader.state
    st.faults.update(faults or {})
    if bins:
        st.bins.update(bins)
    if setup:
        setup(st)
    paths = []
    if source:
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\0" * 4096)
        paths = [str(iso)]
    lbl = None
    if label:
        lbl = tmp_path / "label.png"
        lbl.write_bytes(b"png")
        monkeypatch.setattr(runner, "build_print_job", lambda *a, **k: JOB_BYTES)
    job = runner.DiscJob("t", copies=copies, source_kind="iso" if source else "none", source_paths=paths,
                         label_path=str(lbl) if lbl else None, parallel=parallel)
    tr = Tracker(copies)
    asked = []

    def answer(p):
        asked.append(p)
        return ask(p, st) if ask else "stop"
    result = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=logs.append,
                            timing=FAST, workdir=str(tmp_path / "w"), sleep=lambda _s: None,
                            progress=tr, ask=answer if ask else None)
    return result, st, tr.snapshot(), logs, asked


def test_print_job_without_m2_and_end_block():
    import numpy as np
    planes = {p: np.zeros((4, 1920), np.uint8) for p in (1, 2, 3)}
    data = ej.build_job(planes, ribbon="cmy", media_in_header=True, include_m2=False, include_end=False)
    blocks = ej.parse_job(data)
    assert blocks[0]["kind"] == "block" and blocks[0]["name"] == "e"          # no ESC!m2
    assert all(b.get("code") != 0x0C for b in blocks)                          # no end block
    assert blocks[1]["args"][4] == ord("0")                                    # media byte in ESC&A


def test_burn_failure_goes_to_reject_without_label(tmp_path, monkeypatch):
    result, st, snap, logs, _ = run(tmp_path, monkeypatch, copies=3, faults={"burn": 1})
    assert result == "done", "\n".join(logs)
    assert snap["done"] == 2 and snap["rejected"] == 1
    assert st.bins[201] == 2 and st.bins[202] == 1
    assert st.printed_total == 1234 + 2                 # the failed disc was not printed
    assert any("kept for the next disc" in l for l in logs)
    assert not st.label_on_film                         # leftover label discarded at the end


def test_preload_before_retransfer(tmp_path, monkeypatch):
    result, st, snap, logs, _ = run(tmp_path, monkeypatch, copies=4)
    assert result == "done", "\n".join(logs)
    text = "\n".join(logs)
    # disc 3 goes into a recorder before disc 1 is printed (preloading)
    assert text.index("--- disc 3 of 4") < text.index("disc 1: label printed")
    assert snap["done"] == 4 and st.bins[201] == 4


def test_clamp_error_recovers_automatically(tmp_path, monkeypatch):
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=1, source=False, faults={"clamp": 2})
    assert result == "done", "\n".join(logs)
    assert sum("clamp error in the printer, attempt" in l for l in logs) == 2
    assert not asked


def test_clamp_error_operator_rejects_disc(tmp_path, monkeypatch):
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=2, source=False, faults={"clamp": 3},
                                        ask=lambda p, st: "reject")
    assert result == "done", "\n".join(logs)
    assert asked[0].code == "clamp_error"
    assert snap["done"] == 1 and snap["rejected"] == 1 and st.bins[202] == 1
    assert not st.drawer_open and not st.gripper


def test_empty_input_bin_asks_and_continues(tmp_path, monkeypatch):
    def refill(p, st):
        assert p.code == "bin_empty"
        st.bins[200] = 5
        return "retry"
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=2, source=False, bins={200: 1}, ask=refill)
    assert result == "done", "\n".join(logs)
    assert len(asked) == 1 and snap["done"] == 2


def test_without_operator_a_problem_fails_the_job(tmp_path, monkeypatch):
    result, st, snap, logs, _ = run(tmp_path, monkeypatch, copies=2, source=False, bins={200: 1})
    assert result == "failed"
    assert "empty" in snap["error"]


def test_operator_stop(tmp_path, monkeypatch):
    result, st, snap, logs, _ = run(tmp_path, monkeypatch, copies=2, source=False, bins={200: 0},
                                    ask=lambda p, st: "stop")
    assert result == "stopped"


def test_lost_label_is_sent_again(tmp_path, monkeypatch):
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=1, source=False, faults={"label_lost": 1})
    assert result == "done", "\n".join(logs)
    assert any("label lost" in l for l in logs) and not asked
    assert len(list((tmp_path / "printjobs").iterdir())) == 2


def test_print_failure_asks_and_rejects(tmp_path, monkeypatch):
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=1, source=False, faults={"print_zero": 1},
                                        ask=lambda p, st: "no")
    assert result == "done", "\n".join(logs)
    assert asked[0].code == "print_failed"
    assert snap["rejected"] == 1 and st.bins[202] == 1


def test_printer_error_auto_reset(tmp_path, monkeypatch):
    # 41 (0x29, halogen lamp) is on the list of errors that get one automatic reset
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=1, source=False,
                                        faults={"printer_error": 1})
    assert result == "done", "\n".join(logs)
    assert any("resetting the printer automatically" in l for l in logs) and not asked


def test_printer_supply_error_asks_operator(tmp_path, monkeypatch):
    def replace(p, st):
        st.printer_sense = 0
        return "retry"
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=1, source=False,
                                        faults={"printer_error": 1}, ask=replace,
                                        setup=lambda st: setattr(st, "printer_error_code", 21))
    assert result == "done", "\n".join(logs)
    assert asked[0].code == "printer_supply"


def test_tray_stuck_asks_operator(tmp_path, monkeypatch):
    def push(p, st):
        assert p.code == "tray_close"
        return "retry"
    result, st, snap, logs, asked = run(tmp_path, monkeypatch, copies=1, label=False, faults={"tray_stuck": 2},
                                        ask=push, parallel=False)
    assert result == "done", "\n".join(logs)
    assert len(asked) == 2          # one question each time the tray gets stuck


def test_problem_visible_in_progress(tmp_path, monkeypatch):
    seen = []

    def look(p, st):
        st.bins[200] = 3
        return "retry"
    logs = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path), log=logs.append)
    loader.state.bins[200] = 0
    tr = Tracker(1, on_change=lambda s: seen.append(s.get("problem")))
    job = runner.DiscJob("t", copies=1)
    runner.run_job(job, loader=loader, printer=printer, burners=burners, log=logs.append, timing=FAST,
                   workdir=str(tmp_path / "w"), sleep=lambda _s: None, progress=tr,
                   ask=lambda p: look(p, loader.state))
    shown = [p for p in seen if p]
    assert shown and shown[0]["waiting"] and shown[0]["options"][0]["label"] == "Retry"
    assert seen[-1] is None


def test_audio_workdir_in_ram_is_removed(tmp_path, monkeypatch):
    import wave
    from rimage2410 import burn
    monkeypatch.setattr(burn, "_mem_available", lambda: 8 * 1024 ** 3)
    monkeypatch.setattr(burn, "RAM_RESERVE", 0)
    wav = tmp_path / "t.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\0\0\0\0" * 44100 * 5)
    ram = tmp_path / "shm"
    ram.mkdir()
    logs = []
    loader, printer, burners = runner.make_dry_run_backends(str(tmp_path), log=logs.append)
    job = runner.DiscJob("ramjob", copies=2, source_kind="audio", source_paths=[str(wav)])
    result = runner.run_job(job, loader=loader, printer=printer, burners=burners, log=logs.append, timing=FAST,
                            workdir=str(tmp_path / "w"), sleep=lambda _s: None, ram_dir=str(ram))
    assert result == "done", "\n".join(logs)
    assert any("work files in RAM" in l for l in logs)
    assert not (ram / "rimage2410" / "ramjob").exists()
