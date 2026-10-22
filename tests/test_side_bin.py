"""Side output bin (element 300): used automatically for small runs; when full, the operator is asked."""
import threading
import time

from rimage2410 import runner
from rimage2410.robot.commands import Element

from test_web import app, client, upload_image  # noqa: F401  (fixtures)


def test_resolve_output():
    assert runner.resolve_output("auto", 1) == "side"
    assert runner.resolve_output("auto", 5) == "side"
    assert runner.resolve_output("auto", 6) == 2
    assert runner.resolve_output(None, 3) == "side"
    assert runner.resolve_output("side", 20) == "side"
    assert runner.resolve_output("3", 2) == 3
    assert runner.OUTPUTS["side"] == Element.MAILSLOT == 300


def _job(client, **kw):
    img = upload_image(client, (1889, 1889))
    body = {"image_id": img["id"], "ribbon": "cmy", "input_bin": 1, "reject_bin": 3, **kw}
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 201, r.json
    return r.json["id"]


def test_small_run_goes_to_side_bin(app, client):
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    job_id = _job(client, copies=2)
    assert store.get(job_id)["params"]["output_bin"] == "side"
    assert worker.run_one(store.next_queued()) == "done"
    assert worker.state.side_bin == 2 and worker.state.bins[201] == 0
    assert "side bin" in client.get(f"/jobs/{job_id}").get_data(as_text=True)


def test_large_run_goes_to_bin_2(app, client):
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    job_id = _job(client, copies=6)
    assert store.get(job_id)["params"]["output_bin"] == 2
    assert worker.run_one(store.next_queued()) == "done"
    assert worker.state.bins[201] == 6 and worker.state.side_bin == 0


def test_full_side_bin_asks_to_empty(app, client):
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    worker.state.side_bin = 4                      # discs from an earlier job are still there
    job_id = _job(client, copies=2, output_bin="side")
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", worker.run_one(store.next_queued())), daemon=True)
    t.start()
    for _ in range(1000):
        p = (worker.progress(job_id) or {}).get("problem")
        if p:
            break
        time.sleep(0.02)
    assert p and p["code"] == "bin_full" and "side bin" in p["title"].lower()
    with worker.state.lock:
        worker.state.side_bin = 0                  # the operator takes the discs out
    client.post(f"/api/jobs/{job_id}/answer", json={"choice": "retry"})
    t.join(10)
    assert result["r"] == "done" and worker.state.side_bin == 1


def test_side_bin_cannot_be_input(client):
    img = upload_image(client, (1889, 1889))
    r = client.post("/api/jobs", json={"image_id": img["id"], "input_bin": "side"})
    assert r.status_code == 400
