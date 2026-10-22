"""Web app in 'real machine' mode, with the simulated machine behind the same guard as HidLoader."""
import json
import os
import threading
import time

import pytest

from rimage2410 import runner
from rimage2410.hardware import READ_ONLY_PREFIXES, HardwareError
from rimage2410.motion_policy import TESTED, MotionPolicy
from rimage2410.robot.commands import check_allowed
from rimage2410.web.app import create_app

from test_web import upload_image


class GuardedLoader:
    """DryRunLoader with the same check as HidLoader.transact: reading is allowed, moving only with approval."""

    def __init__(self, inner, allow_motion):
        self.inner, self.allow_motion = inner, allow_motion
        self.state = inner.state
        self.sent: list[bytes] = []

    def transact(self, command: bytes, expect: int, timeout_s: float = 10.0) -> bytes:
        check_allowed(command)
        body = command.lstrip(b"\x1b")
        if not body.startswith(READ_ONLY_PREFIXES) and not self.allow_motion(command):
            raise HardwareError(f"motion command {command!r} refused")
        self.sent.append(command)
        return self.inner.transact(command, expect, timeout_s)


@pytest.fixture
def hw(tmp_path):
    made = {}

    def factory(*, allow_motion, log=None):
        loader, printer, burners = runner.make_dry_run_backends(str(tmp_path / "sim"))
        made["loader"] = GuardedLoader(loader, allow_motion)
        return made["loader"], printer, burners

    app = create_app({"DATA_DIR": str(tmp_path / "data"), "START_WORKER": False, "FAST_WORKER": True,
                      "SIM_STEP_DELAY": 0, "SIM_BURN_S": 0, "TESTING": True, "HARDWARE": "real",
                      "HW_BACKENDS": factory})
    return app, app.test_client(), made


def test_banner_and_sim_endpoints(hw):
    app, client, _ = hw
    assert "Real machine" in client.get("/machine").get_data(as_text=True)
    assert client.post("/api/machine/sim-fault", json={"fault": "door"}).status_code == 409
    assert client.post("/api/machine/reset").status_code == 409
    m = client.get("/api/machine").json
    assert m["mode"] == "real" and m["busy"] is False and m["bins"]["1"] == 25 and m["gripper"] is False


def test_policy_refuses_without_job(tmp_path):
    p = MotionPolicy(str(tmp_path))
    cmd = next(iter(TESTED))
    assert p(cmd) is False
    p.active = True
    assert p(cmd) is True
    assert p(b"\x1b!f12606C") is False          # new and nobody to ask


def test_job_with_tested_moves_runs_without_questions(hw):
    app, client, made = hw
    img = upload_image(client, (1889, 1889))
    r = client.post("/api/jobs", json={"image_id": img["id"], "copies": 1, "ribbon": "cmy",
                                        "input_bin": 1, "output_bin": 2, "reject_bin": 3})
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    assert worker.run_one(store.next_queued()) == "done"
    assert not worker.policy.active
    moves = [c for c in made["loader"].sent if not c.lstrip(b"\x1b").startswith(READ_ONLY_PREFIXES)]
    assert moves and all(c in TESTED for c in moves)
    assert os.path.exists(os.path.join(app.config["DATA_DIR"], "logs"))


def test_new_command_is_asked_once_and_remembered(hw, monkeypatch):
    # !f12001C is a tested command; this test treats it as new
    from rimage2410 import motion_policy
    monkeypatch.setattr(motion_policy, "TESTED", motion_policy.TESTED - {b"\x1b!f12001C"})
    app, client, made = hw
    made["loader"].state.bins[201] = 5
    img = upload_image(client, (1889, 1889))
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    job_id = client.post("/api/jobs", json={"image_id": img["id"], "copies": 1, "ribbon": "cmy",
                                             "input_bin": 2, "output_bin": 1, "reject_bin": 3}).json["id"]
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", worker.run_one(store.next_queued())), daemon=True)
    t.start()
    for _ in range(1000):                         # wait for the question about !f12001C (disc in bin 1)
        p = (worker.progress(job_id) or {}).get("problem")
        if p:
            break
        time.sleep(0.02)
    assert p and p["code"] == "new_command" and "f12001C" in p["message"]
    assert [o["id"] for o in p["options"]] == ["allow", "stop"]
    assert client.post(f"/api/jobs/{job_id}/answer", json={"choice": "allow"}).status_code == 200
    t.join(10)
    assert result["r"] == "done"
    with open(os.path.join(app.config["DATA_DIR"], "approved_commands.json")) as f:
        assert "!f12001C" in json.load(f)["commands"]
    assert worker.policy.known(b"\x1b!f12001C")


def test_refusing_a_new_command_stops_the_job(hw, monkeypatch):
    # !f12001C is a tested command; this test treats it as new
    from rimage2410 import motion_policy
    monkeypatch.setattr(motion_policy, "TESTED", motion_policy.TESTED - {b"\x1b!f12001C"})
    app, client, made = hw
    made["loader"].state.bins[201] = 5
    img = upload_image(client, (1889, 1889))
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    job_id = client.post("/api/jobs", json={"image_id": img["id"], "copies": 1, "ribbon": "cmy",
                                             "input_bin": 2, "output_bin": 1, "reject_bin": 3}).json["id"]
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", worker.run_one(store.next_queued())), daemon=True)
    t.start()
    for _ in range(1000):
        if (worker.progress(job_id) or {}).get("problem"):
            break
        time.sleep(0.02)
    client.post(f"/api/jobs/{job_id}/answer", json={"choice": "stop"})
    t.join(10)
    assert result["r"] in ("failed", "stopped")
    assert not os.path.exists(os.path.join(app.config["DATA_DIR"], "approved_commands.json"))


def test_machine_off_at_start_connects_later(tmp_path):
    state = {"on": False}

    def factory(*, allow_motion, log=None):
        if not state["on"]:
            raise HardwareError("autoloader (0cc3:000d) not found")
        loader, printer, burners = runner.make_dry_run_backends(str(tmp_path / "sim"))
        return GuardedLoader(loader, allow_motion), printer, burners

    app = create_app({"DATA_DIR": str(tmp_path / "data"), "START_WORKER": False, "FAST_WORKER": True,
                      "SIM_STEP_DELAY": 0, "SIM_BURN_S": 0, "TESTING": True, "HARDWARE": "real",
                      "HW_BACKENDS": factory})
    client = app.test_client()
    m = client.get("/api/machine").json
    assert m["connected"] is False and "not found" in m["error"]
    img = upload_image(client, (1889, 1889))
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    client.post("/api/jobs", json={"image_id": img["id"], "copies": 1, "ribbon": "cmy",
                                   "input_bin": 1, "output_bin": 2, "reject_bin": 3})
    assert worker.run_one(store.next_queued()) == "failed"
    state["on"] = True
    assert client.get("/api/machine").json["connected"] is True
