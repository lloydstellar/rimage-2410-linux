import io
import os
import time
import wave

import pytest
from PIL import Image

from rimage2410.web.app import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app({"DATA_DIR": str(tmp_path / "data"), "START_WORKER": False, "FAST_WORKER": True,
                      "SIM_STEP_DELAY": 0, "SIM_BURN_S": 0, "TESTING": True})
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def png_bytes(size=(600, 600), color=(30, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    buf.seek(0)
    return buf


def wav_bytes(seconds=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\0\0\0\0" * int(44100 * seconds))
    buf.seek(0)
    return buf


def upload_image(client, size=(600, 600)):
    r = client.post("/api/image", data={"image": (png_bytes(size), "photo.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    return r.json


def test_pages(client):
    for path in ("/", "/jobs", "/machine"):
        assert client.get(path).status_code == 200
    r = client.get("/template.png")
    assert r.status_code == 200 and r.mimetype == "image/png"


def test_image_upload_and_preview(client):
    j = upload_image(client)
    assert j["width"] == 600 and any(c["code"] == "resolution" and c["level"] == "warning" for c in j["checks"])
    r = client.get(f"/api/preview.png?image={j['id']}&size=300&fit=contain&scale=1.2&ox=5&rot=30&bg=%23ff0000&hub=1")
    assert r.status_code == 200
    im = Image.open(io.BytesIO(r.data))
    assert im.size == (300, 300) and im.mode == "RGBA"
    assert client.get("/api/preview.png?size=200").status_code == 200   # without an image


def test_bad_uploads(client):
    r = client.post("/api/image", data={"image": (io.BytesIO(b"not an image"), "x.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    r = client.post("/api/image", data={"image": (io.BytesIO(b"x"), "x.exe")}, content_type="multipart/form-data")
    assert r.status_code == 400
    r = client.post("/api/source", data={"files": [(io.BytesIO(b"x"), "a.iso"), (wav_bytes(), "b.wav")]},
                    content_type="multipart/form-data")
    assert r.status_code == 400          # ISO and audio mixed
    r = client.post("/api/source", data={"files": [(io.BytesIO(b"x"), "a.bin")]}, content_type="multipart/form-data")
    assert r.status_code == 400          # BIN without CUE


def test_source_upload_audio(client):
    r = client.post("/api/source", data={"files": [(wav_bytes(), "02 two.wav"), (wav_bytes(), "01 one.wav")]},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    assert r.json["kind"] == "audio" and r.json["files"] == ["01_one.wav", "02_two.wav"]


def test_job_validation(client):
    assert client.post("/api/jobs", json={}).status_code == 400
    img = upload_image(client)
    assert client.post("/api/jobs", json={"image_id": img["id"], "input_bin": 1, "output_bin": 1}).status_code == 400
    assert client.post("/api/jobs", json={"image_id": img["id"], "ribbon": "red"}).status_code == 400


def test_job_runs_in_dry_run(app, client):
    img = upload_image(client, (1900, 1900))
    src = client.post("/api/source", data={"files": [(io.BytesIO(b"\0" * 4096), "data.iso")]},
                      content_type="multipart/form-data").json
    r = client.post("/api/jobs", json={"image_id": img["id"], "source_id": src["id"], "copies": 2,
                                        "ribbon": "cmy", "input_bin": 1, "output_bin": 2, "reject_bin": 3,
                                        "title": "Test", "label": {"fit": "cover", "hub": 0}})
    assert r.status_code == 201, r.json
    job_id = r.json["id"]
    assert client.get(f"/jobs/{job_id}/preview.png").status_code == 200
    worker = app.extensions["rimage_worker"]
    job = app.extensions["rimage_store"].next_queued()
    assert job["id"] == job_id
    assert worker.run_one(job) == "done", app.extensions["rimage_store"].get(job_id)["log"]
    j = client.get(f"/api/jobs/{job_id}").json
    assert j["status"] == "done" and "job done" in j["log"]
    m = client.get("/api/machine").json
    assert m["bins"] == {"1": 23, "2": 2, "3": 0}
    assert client.get(f"/jobs/{job_id}").status_code == 200
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_stop_queued_job(client):
    img = upload_image(client)
    job_id = client.post("/api/jobs", json={"image_id": img["id"]}).json["id"]
    assert client.post(f"/api/jobs/{job_id}/stop").json["ok"]
    assert client.get(f"/api/jobs/{job_id}").json["status"] == "stopped"


def test_access_code(tmp_path):
    app = create_app({"DATA_DIR": str(tmp_path / "d"), "START_WORKER": False, "ACCESS_CODE": "secret"})
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/api/jobs").status_code == 401
    assert c.post("/login", data={"code": "wrong"}).status_code == 200
    r = c.post("/login?next=/jobs", data={"code": "secret"})
    assert r.status_code == 302 and r.location.endswith("/jobs")
    assert c.get("/api/jobs").status_code == 200


def test_upload_limit(tmp_path):
    app = create_app({"DATA_DIR": str(tmp_path / "d"), "START_WORKER": False, "MAX_CONTENT_LENGTH": 1000})
    r = app.test_client().post("/api/image", data={"image": (png_bytes((900, 900)), "a.png")},
                               content_type="multipart/form-data")
    assert r.status_code == 413


def test_preview_bad_size_does_not_crash(client):
    r = client.get("/api/preview.png?size=abc")
    assert r.status_code == 200


def test_printjob_preview_route_404_before_job(client):
    assert client.get("/jobs/doesnotexist/printjob.png").status_code == 404


def test_machine_supplies_and_sim_ribbon(client):
    m = client.get("/api/machine").get_json()
    assert m["ribbon"] == "cmy" and m["ribbon_remaining"] == 437 and m["film_percent"] == 82
    assert client.post("/api/machine/sim-ribbon", json={"ribbon": "mono"}).status_code == 200
    m = client.get("/api/machine").get_json()
    assert m["ribbon"] == "mono" and m["ribbon_remaining"] == 1000


def test_psd_template_download(client):
    r = client.get("/template.psd")
    assert r.status_code == 200 and r.data[:4] == b"8BPS"
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_operator_question_and_answer(app, client):
    """An error pauses the job; the question shows up in the progress and the answer comes in through the API."""
    import threading
    img = upload_image(client, (1900, 1900))
    r = client.post("/api/jobs", json={"image_id": img["id"], "copies": 1, "ribbon": "cmy",
                                        "input_bin": 1, "output_bin": 2, "reject_bin": 3})
    job_id = r.json["id"]
    assert client.post("/api/machine/sim-fault", json={"fault": "bin_empty"}).status_code == 200
    assert client.post(f"/api/jobs/{job_id}/answer", json={"choice": "retry"}).status_code == 409
    worker = app.extensions["rimage_worker"]
    job = app.extensions["rimage_store"].next_queued()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", worker.run_one(job)), daemon=True)
    t.start()
    try:
        q = None
        deadline = time.time() + 60          # the colour conversion of the label takes a while
        while time.time() < deadline:
            prog = client.get(f"/api/jobs/{job_id}").json["progress"] or {}
            q = prog.get("problem")
            if (q and q.get("waiting")) or not t.is_alive():
                break
            time.sleep(0.05)
        assert q and q["code"] == "bin_empty" and [o["id"] for o in q["options"]] == ["retry", "stop"]
        assert client.post(f"/api/jobs/{job_id}/answer", json={"choice": "reject"}).status_code == 409
        worker.state.bins[200] = 5
        assert client.post(f"/api/jobs/{job_id}/answer", json={"choice": "retry"}).status_code == 200
        t.join(30)
        assert out["r"] == "done"
    finally:
        worker.stop_job(job_id)
        t.join(10)
    assert client.post("/api/machine/sim-fault", json={"fault": "nonsense"}).status_code == 400


def wav24_bytes(seconds=5):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(44100)
        w.writeframes(b"\0\0\0" * 2 * int(44100 * seconds))
    buf.seek(0)
    return buf


def test_audio_cd_composer(app, client):
    """Audio CD composer: measuring on upload, adding tracks, custom order, pauses and options."""
    r = client.post("/api/source", data={"files": [(wav24_bytes(), "01 intro.wav"), (wav24_bytes(6), "02 song.wav")]},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    src = r.json
    assert [t["file"] for t in src["tracks"]] == ["01_intro.wav", "02_song.wav"]
    t0 = src["tracks"][0]
    assert t0["bits"] == 24 and t0["sample_rate"] == 44100 and "dither" in t0["conversion"]
    r = client.post(f"/api/source/{src['id']}/files", data={"files": [(wav24_bytes(5), "03 outro.wav")]},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.json["tracks"][0]["file"] == "03_outro.wav"
    tracks = [{"file": "02_song.wav", "title": "Song", "pause": 2},
              {"file": "01_intro.wav", "title": "Intro", "pause": 0},
              {"file": "03_outro.wav", "title": "Outro", "pause": 4, "isrc": "NL-A1B-26-00003"}]
    bad = client.post("/api/jobs", json={"source_id": src["id"], "tracks": [{"file": "x.wav"}]})
    assert bad.status_code == 400
    bad = client.post("/api/jobs", json={"source_id": src["id"], "tracks": tracks, "audio": {"upc": "12"}})
    assert bad.status_code == 400 and "UPC" in bad.json["error"]
    r = client.post("/api/jobs", json={"source_id": src["id"], "tracks": tracks, "album": "Demo", "artist": "Artist",
                                        "audio": {"gapless": False, "dither": "lipshitz", "gain_db": -1,
                                                  "speed": "16", "copy_permitted": True}})
    assert r.status_code == 201, r.json
    store = app.extensions["rimage_store"]
    p = store.get(r.json["id"])["params"]
    assert [os.path.basename(x) for x in p["source_paths"]] == ["02_song.wav", "01_intro.wav", "03_outro.wav"]
    assert p["titles"] == ["Song", "Intro", "Outro"] and p["audio"]["pauses"] == [2, 0, 4]
    assert p["audio"]["dither"] == "lipshitz" and p["audio"]["speed"] == 16
    worker = app.extensions["rimage_worker"]
    assert worker.run_one(store.next_queued()) == "done"
    log = store.get(r.json["id"])["log"]
    assert "dither_method=lipshitz" in log and "volume=-1.00dB" in log
    assert "sao_pregap=300" in log and "isrc=NLA1B2600003" in log and "speed=16" in log


def test_artwork_checks_and_profiles(client, tmp_path):
    """Adobe RGB is converted to sRGB, CMYK is reported, and text under the centre hole is detected."""
    from PIL import ImageCms, ImageDraw
    # text in the centre on a plain background
    im = Image.new("RGB", (1889, 1889), (40, 90, 160))
    ImageDraw.Draw(im).text((790, 915), "ALBUM", font=__import__("PIL.ImageFont", fromlist=["x"]).load_default(60), fill="white")
    buf = io.BytesIO(); im.save(buf, "PNG"); buf.seek(0)
    j = client.post("/api/image", data={"image": (buf, "centre.png")}, content_type="multipart/form-data").json
    assert any(c["code"] == "centre" for c in j["checks"])
    r = client.get(f"/api/label-check?image={j['id']}&fit=contain&scale=0.5&ox=40")
    assert r.status_code == 200 and "ppi" in r.json
    # CMYK
    buf = io.BytesIO(); Image.new("CMYK", (1889, 1889), (0, 80, 80, 0)).save(buf, "JPEG"); buf.seek(0)
    j = client.post("/api/image", data={"image": (buf, "cmyk.jpg")}, content_type="multipart/form-data").json
    assert any(c["code"] == "cmyk" for c in j["checks"]) and j["info"]["mode"] == "CMYK"
    # upload the PSD template directly
    psd = client.get("/template.psd").data
    j = client.post("/api/image", data={"image": (io.BytesIO(psd), "template.psd")},
                    content_type="multipart/form-data").json
    assert j["width"] == 1889 and any(c["code"] == "psd" for c in j["checks"])


def test_media_profiles(client, app):
    """The disc type sets the print area; the default is FalconMedia discs with bleed over the white."""
    from rimage2410.web.app import _label_opts
    o = _label_opts({})
    assert (o["inner_mm"], o["outer_mm"]) == (22.0, 119.0)
    o = _label_opts({"media": "rimage"})
    assert (o["inner_mm"], o["outer_mm"]) == (24.0, 118.0)
    o = _label_opts({"media": "custom", "inner": "5", "outer": "130"})
    assert (o["inner_mm"], o["outer_mm"]) == (16.0, 119.5)          # clamped
    img = upload_image(client, (1900, 1900))
    a = client.get(f"/api/preview.png?image={img['id']}&media=rimage").data
    b = client.get(f"/api/preview.png?image={img['id']}&media=falcon").data
    assert a != b
    r = client.get("/template.psd?media=falcon")
    assert r.status_code == 200 and "22-119mm" in r.headers["Content-Disposition"]


def test_releases_print_on_demand(app, client):
    """Save a release, open it, make it on demand, update it, download it as a zip and delete it."""
    import zipfile
    img = upload_image(client, (1900, 1900))
    src = client.post("/api/source", data={"files": [(wav24_bytes(5), "01 Test Artist - Intro.wav"),
                                                     (wav24_bytes(6), "02 Test Artist - Song.wav")]},
                      content_type="multipart/form-data").json
    tracks = [{"file": "02_Test_Artist_-_Song.wav", "title": "Song", "pause": 0},
              {"file": "01_Test_Artist_-_Intro.wav", "title": "Intro", "pause": 1.5}]
    body = {"name": "Test Artist - Test EP", "image_id": img["id"], "source_id": src["id"], "tracks": tracks,
            "album": "Test EP", "artist": "Test Artist", "label": {"fit": "cover", "scale": "1.2", "media": "falcon"},
            "audio": {"gapless": False, "dither": "lipshitz", "gain_db": 0, "speed": "8", "copy_permitted": True},
            "ribbon": "auto", "input_bin": 1, "output_bin": 2, "reject_bin": 3, "copies": 2}
    assert client.post("/api/releases", json={**body, "name": ""}).status_code == 400
    r = client.post("/api/releases", json=body)
    assert r.status_code == 201, r.json
    rid = r.json["id"]
    assert rid == "test-artist-test-ep"
    rel = app.extensions["rimage_releases"]
    d = rel.path(rid)
    assert sorted(os.listdir(os.path.join(d, "tracks"))) == ["01_Test_Artist_-_Intro.wav", "02_Test_Artist_-_Song.wav"]
    assert os.path.isfile(os.path.join(d, "preview.png")) and os.path.isfile(os.path.join(d, "artwork.png"))
    meta = client.get(f"/api/releases/{rid}").json
    assert meta["tracks"][0]["title"] == "Song" and meta["audio"]["speed"] == 8 and meta["tracks_info"][0]["bits"] == 24
    lst = client.get("/api/releases").json["releases"]
    assert lst[0]["id"] == rid and lst[0]["tracks"] == 2 and lst[0]["made"] == 0
    # make on demand: 3 discs, with the saved settings
    r = client.post(f"/api/releases/{rid}/jobs", json={"copies": 3})
    assert r.status_code == 201, r.json
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    job = store.get(r.json["id"])
    p = job["params"]
    assert p["copies"] == 3 and p["titles"] == ["Song", "Intro"] and p["audio"]["dither"] == "lipshitz"
    assert p["label_opts"]["inner_mm"] == 22.0 and p["source_paths"][0].startswith(d)
    assert worker.run_one(store.next_queued()) == "done"
    assert client.get("/api/releases").json["releases"][0]["made"] == 3
    # open in the job page: new uploads with the same content
    o = client.post(f"/api/releases/{rid}/open").json
    assert o["image_id"] and o["source_id"] and o["kind"] == "audio" and o["release"]["name"] == body["name"]
    assert client.get(f"/api/image/{o['image_id']}/file").status_code == 200
    # update: same id, history is kept
    r = client.post("/api/releases", json={**body, "release_id": rid, "image_id": o["image_id"],
                                           "source_id": o["source_id"], "tracks": tracks[:1]})
    assert r.status_code == 201 and r.json["id"] == rid
    meta = client.get(f"/api/releases/{rid}").json
    assert len(meta["tracks"]) == 1 and meta["history"][0]["copies"] == 3
    assert os.listdir(os.path.join(d, "tracks")) == ["02_Test_Artist_-_Song.wav"]
    # zip
    z = client.get(f"/api/releases/{rid}/zip")
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert f"{rid}/release.json" in names and f"{rid}/tracks/02_Test_Artist_-_Song.wav" in names
    # a second release with the same name gets its own folder
    r2 = client.post("/api/releases", json={**body, "image_id": o["image_id"], "source_id": o["source_id"]})
    assert r2.json["id"] == "test-artist-test-ep-2"
    assert client.delete(f"/api/releases/{rid}").status_code == 200
    assert client.get(f"/api/releases/{rid}").status_code == 404
    assert client.get("/releases").status_code == 200


def test_disk_full_gives_clear_message(client, monkeypatch):
    import errno
    from werkzeug.datastructures import FileStorage

    def full(self, dst, buffer_size=16384):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(FileStorage, "save", full)
    r = client.post("/api/source", data={"files": [(wav24_bytes(5), "a.wav")]}, content_type="multipart/form-data")
    assert r.status_code == 507 and "disk of this computer is full" in r.json["error"]


def test_burn_only_without_printing(app, client):
    """Print label off: the artwork stays in the release, the job only burns."""
    img = upload_image(client, (1900, 1900))
    src = client.post("/api/source", data={"files": [(io.BytesIO(b"\0" * 4096), "data.iso")]},
                      content_type="multipart/form-data").json
    store, worker = app.extensions["rimage_store"], app.extensions["rimage_worker"]
    # without a source there is nothing to do
    assert client.post("/api/jobs", json={"image_id": img["id"], "print": False}).status_code == 400
    r = client.post("/api/jobs", json={"image_id": img["id"], "source_id": src["id"], "print": False,
                                        "input_bin": 1, "output_bin": "side"})
    assert r.status_code == 201, r.json
    job = store.get(r.json["id"])
    assert job["params"]["label_path"] is None
    assert worker.run_one(store.next_queued()) == "done", store.get(job["id"])["log"]
    log = client.get(f"/api/jobs/{job['id']}").json["log"]
    assert "burn only" in log and "print job" not in log
    # release: artwork and source saved, made without a label
    rid = client.post("/api/releases", json={"name": "Burn only", "image_id": img["id"],
                                             "source_id": src["id"]}).json["id"]
    r = client.post(f"/api/releases/{rid}/jobs", json={"copies": 1, "input_bin": 2, "print": False})
    assert r.status_code == 201, r.json
    p = store.get(r.json["id"])["params"]
    assert p["label_path"] is None and p["input_bin"] == 2
    r = client.post(f"/api/releases/{rid}/jobs", json={"copies": 1})
    assert store.get(r.json["id"])["params"]["label_path"]
    # release with artwork only: burning only is not possible
    rid2 = client.post("/api/releases", json={"name": "Label only", "image_id": img["id"]}).json["id"]
    assert client.post(f"/api/releases/{rid2}/jobs", json={"print": False}).status_code == 400
