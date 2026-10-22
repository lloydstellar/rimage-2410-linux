"""Flask web interface for the Rimage 2410: design a label, upload a source, job queue."""

from __future__ import annotations

import io
import os
import re
import secrets
import shutil
import uuid
from functools import wraps

from flask import (Flask, abort, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from .. import artwork, runner
from .. import audio as audiomod
from .. import label as lbl
from ..burn import AUDIO_EXT, BurnError, BurnPlan
from ..geometry import CANVAS_PX, DEFAULT_MEDIA, MEDIA, MEDIA_LIMITS
from .releases import ReleaseError, ReleaseStore
from .store import Store
from .worker import Worker

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".psd"}
PREVIEW_SOURCE_MAX = 2400     # scaled-down copy of the source for previews and checks
SOURCE_EXT = AUDIO_EXT | {".iso", ".cue", ".bin"}
RIBBONS = {"auto": "Automatic (whatever is in the printer)", "cmy": "CMY (colour)", "cmyw": "CMY + white", "mono": "Black"}
ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

DEFAULTS = {
    "DATA_DIR": os.path.expanduser("~/rimage-data"),
    "ACCESS_CODE": "",                       # empty = no login
    "MAX_CONTENT_LENGTH": 2 * 1024 ** 3,     # 2 GB per request
    "HARDWARE": "dry-run",                   # "dry-run" (simulated) or "real" (the real 2410, --hardware)
    "HW_BACKENDS": None,                     # tests: replaces runner.make_hardware_backends
    "SIM_STEP_DELAY": 0.3,                   # seconds per move in the dry run
    "SIM_BURN_S": 3.0,                       # seconds of "burning" in the dry run
    "PRINT_FALLBACK": True,                  # simple conversion if the colour conversion fails
    "START_WORKER": True,
    "FAST_WORKER": False,                    # tests: no waiting
    "RAM_DIR": "/dev/shm",                   # converted audio in RAM; "" = always on disk
}


def _label_opts(src) -> dict:
    """Label settings from a dict (query string or JSON), clamped to limits."""
    if not hasattr(src, "get"):
        src = {}
    def num(name, default, lo, hi):
        try:
            v = float(src.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    bg = str(src.get("bg", "#ffffff"))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", bg):
        bg = "#ffffff"
    hub = str(src.get("hub", "0")).lower() in ("1", "true", "on", "yes")
    media = str(src.get("media", DEFAULT_MEDIA))
    if media == "custom":
        inner = num("inner", MEDIA[DEFAULT_MEDIA]["inner"], *MEDIA_LIMITS["inner"])
        outer = num("outer", MEDIA[DEFAULT_MEDIA]["outer"], *MEDIA_LIMITS["outer"])
    else:
        media = media if media in MEDIA else DEFAULT_MEDIA
        inner, outer = MEDIA[media]["inner"], MEDIA[media]["outer"]
    return {
        "fit": "contain" if src.get("fit") == "contain" else "cover",
        "scale": num("scale", 1.0, 0.2, 5.0),
        "offset_x": num("ox", 0.0, -60.0, 60.0),
        "offset_y": num("oy", 0.0, -60.0, 60.0),
        "rotate": num("rot", 0.0, -360.0, 360.0),
        "background": tuple(int(bg[i:i + 2], 16) for i in (1, 3, 5)),
        "print_hub": hub,
        "inner_mm": inner,
        "outer_mm": outer,
    }


def _png(img: Image.Image):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(DEFAULTS)
    if config:
        app.config.update(config)
    data = os.path.abspath(os.path.expanduser(app.config["DATA_DIR"]))
    app.config["DATA_DIR"] = data
    for sub in ("uploads", "jobs", "work", "dryrun"):
        os.makedirs(os.path.join(data, sub), exist_ok=True)

    key_file = os.path.join(data, "secret.key")
    if not os.path.exists(key_file):
        with open(key_file, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(key_file, 0o600)
    app.secret_key = open(key_file).read().strip()

    if app.config["HARDWARE"] not in ("dry-run", "real"):
        raise RuntimeError("HARDWARE must be 'dry-run' or 'real'")
    real = app.config["HARDWARE"] == "real"
    os.makedirs(os.path.join(data, "logs"), exist_ok=True)

    store = Store(os.path.join(data, "jobs.sqlite"))
    worker = Worker(store, data, step_delay_s=app.config["SIM_STEP_DELAY"], burn_s=app.config["SIM_BURN_S"],
                    print_fallback=app.config["PRINT_FALLBACK"], fast=app.config["FAST_WORKER"],
                    ram_dir=app.config["RAM_DIR"] or None, hardware=real,
                    hw_backends=app.config["HW_BACKENDS"])
    app.extensions["rimage_store"] = store
    app.extensions["rimage_worker"] = worker
    if app.config["START_WORKER"]:
        worker.start()

    uploads = os.path.join(data, "uploads")
    releases = ReleaseStore(os.path.join(data, "releases"))
    app.extensions["rimage_releases"] = releases

    # ---- access -------------------------------------------------------------
    def login_required(view):
        @wraps(view)
        def wrapper(*a, **kw):
            if app.config["ACCESS_CODE"] and not session.get("ok"):
                if request.path.startswith("/api/"):
                    return jsonify(error="not logged in"), 401
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapper

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if secrets.compare_digest(request.form.get("code", ""), app.config["ACCESS_CODE"]):
                session["ok"] = True
                session.permanent = True
                nxt = request.args.get("next", "/")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith(("//", "/\\")) else "/")
            error = "Incorrect access code"
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.errorhandler(413)
    def too_large(_e):
        mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return jsonify(error=f"file too large (maximum {mb} MB)"), 413

    # ---- pages --------------------------------------------------------------
    @app.route("/")
    @login_required
    def new_job():
        return render_template("new.html", ribbons=RIBBONS, canvas=CANVAS_PX, media=MEDIA,
                               default_media=DEFAULT_MEDIA, limits=MEDIA_LIMITS)

    @app.route("/jobs")
    @login_required
    def jobs_page():
        return render_template("jobs.html")

    @app.route("/jobs/<job_id>")
    @login_required
    def job_page(job_id):
        job = store.get(job_id)
        if not job:
            abort(404)
        return render_template("job.html", job=job, ribbons=RIBBONS)

    @app.route("/machine")
    @login_required
    def machine_page():
        return render_template("machine.html")

    @app.errorhandler(OSError)
    def disk_full(e):
        """Disk or quota full: a clear message instead of a server error."""
        import errno
        if getattr(e, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
            msg = ("The disk of this computer is full. Delete old jobs or releases, "
                   "or move the data directory to a larger disk.")
            if request.path.startswith("/api/"):
                return jsonify(error=msg), 507
            return msg, 507
        raise e

    @app.route("/releases")
    @login_required
    def releases_page():
        return render_template("releases.html")

    # ---- image and preview -------------------------------------------------
    def _image_path(image_id: str) -> str:
        if not ID_RE.match(image_id or ""):
            abort(400)
        for name in os.listdir(uploads):
            if name.startswith(f"img-{image_id}."):
                return os.path.join(uploads, name)
        abort(404)

    @app.post("/api/image")
    @login_required
    def upload_image():
        f = request.files.get("image")
        if not f or not f.filename:
            return jsonify(error="no image received"), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in IMAGE_EXT:
            return jsonify(error=f"file type {ext or '?'} is not supported"), 400
        image_id = uuid.uuid4().hex[:12]
        path = os.path.join(uploads, f"img-{image_id}{ext}")
        f.save(path)
        try:
            src = _preview_source(image_id)
        except (UnidentifiedImageError, OSError, ValueError):
            os.remove(path)
            return jsonify(error="this file is not a readable image"), 400
        opts = _label_opts({})
        return jsonify(id=image_id, width=src["size"][0], height=src["size"][1],
                       name=secure_filename(f.filename), info=src["info"],
                       checks=_artwork_checks(src, opts), warnings=[])

    _sources: dict[str, dict] = {}

    def _preview_source(image_id: str) -> dict:
        return _load_source(_image_path(image_id), image_id)

    def _load_source(path: str, cache_id: str | None = None) -> dict:
        """Open the source once, convert to sRGB and scale down; after that it comes from memory (fast)."""
        image_id = cache_id or path
        key = f"{path}:{os.path.getmtime(path)}"
        hit = _sources.get(image_id)
        if hit and hit["key"] == key:
            return hit
        with Image.open(path) as im:
            im.load()
            info = artwork.source_info(im, os.path.basename(path))
            if ext_of(path) == ".psd":
                info["note"] = "Photoshop file: the merged image (visible layers) is used"
            rgb, notes = artwork.to_srgb(im)
        size = rgb.size
        if max(size) > PREVIEW_SOURCE_MAX:
            rgb = rgb.copy()
            rgb.thumbnail((PREVIEW_SOURCE_MAX, PREVIEW_SOURCE_MAX), Image.LANCZOS)
        if len(_sources) > 16:
            _sources.pop(next(iter(_sources)))
        _sources[image_id] = {"key": key, "img": rgb, "size": size, "info": info, "notes": notes}
        return _sources[image_id]

    def ext_of(path: str) -> str:
        return os.path.splitext(path)[1].lower()

    def _artwork_checks(src: dict, opts: dict, canvas: int = 900) -> list[dict]:
        placed = lbl.place_image(src["img"], fit=opts["fit"], scale=opts["scale"], offset_x=opts["offset_x"],
                                 offset_y=opts["offset_y"], rotate=opts["rotate"], background=opts["background"],
                                 canvas=canvas, srgb=True)
        found = list(src["notes"]) + artwork.check(src["img"], placed, fit=opts["fit"], scale=opts["scale"],
                                                   print_hub=opts["print_hub"], source_size=src["size"],
                                                   inner_mm=opts["inner_mm"], outer_mm=opts["outer_mm"])
        if src["info"].get("note"):
            found.append(artwork.Finding("info", "psd", src["info"]["note"]))
        return artwork.findings_dicts(found)

    @app.get("/api/label-check")
    @login_required
    def label_check():
        """Artwork checks with the current settings (scale, offset, hub)."""
        src = _preview_source(request.args.get("image", ""))
        opts = _label_opts(request.args)
        ppi = artwork.effective_ppi(*src["size"], opts["fit"], opts["scale"])
        return jsonify(checks=_artwork_checks(src, opts), ppi=round(ppi))

    @app.get("/api/preview.png")
    @login_required
    def preview():
        try:
            size = max(100, min(1200, int(request.args.get("size", 600))))
        except ValueError:
            size = 600
        opts = _label_opts(request.args)
        image_id = request.args.get("image", "")
        if image_id:
            # render at screen size from the scaled-down source in memory: fast enough for live updates
            label = lbl.prepare_label(_preview_source(image_id)["img"], **opts, canvas=size, srgb=True)
        else:
            label = Image.new("RGB", (size, size), (255, 255, 255))
        return _png(lbl.render_preview(label, size=size, print_hub=opts["print_hub"],
                                       outer_mm=opts["outer_mm"], inner_mm=opts["inner_mm"]))

    @app.get("/template.png")
    @login_required
    def template():
        buf = io.BytesIO()
        lbl.make_template().save(buf, "PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png", as_attachment=True,
                         download_name="cd-template-1889px-400dpi.png")

    @app.get("/template.psd")
    @login_required
    def template_psd():
        """Photoshop template with layers; made once and kept in the data directory."""
        from .. import template as tpl
        opts = _label_opts(request.args)
        tag = f"{opts['inner_mm']:g}-{opts['outer_mm']:g}".replace(".", "_")
        path = os.path.join(data, f"cd-template-{tag}.psd")
        if not os.path.exists(path):
            tmp = path + ".tmp"
            tpl.make_psd_template(tmp, opts["inner_mm"], opts["outer_mm"])
            os.replace(tmp, path)
        return send_file(path, mimetype="image/vnd.adobe.photoshop", as_attachment=True,
                         download_name=f"cd-template-1889px-400ppi-{tag}mm.psd")

    # ---- source for burning ----------------------------------------------------
    def _detect_kind(names: list[str]) -> str:
        exts = {os.path.splitext(n)[1].lower() for n in names}
        if exts == {".iso"}:
            return "iso"
        if ".cue" in exts and exts <= {".cue", ".bin"}:
            return "cue"
        if exts and exts <= AUDIO_EXT:
            return "audio"
        raise BurnError("choose one ISO, a BIN/CUE pair, or only audio files")

    @app.post("/api/source")
    @login_required
    def upload_source():
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            return jsonify(error="no files received"), 400
        names = [secure_filename(f.filename) or f"file{i}" for i, f in enumerate(files)]
        for n in names:
            if os.path.splitext(n)[1].lower() not in SOURCE_EXT:
                return jsonify(error=f"file type of {n} is not supported"), 400
        try:
            kind = _detect_kind(names)
        except BurnError as e:
            return jsonify(error=str(e)), 400
        source_id = uuid.uuid4().hex[:12]
        d = os.path.join(uploads, f"src-{source_id}")
        os.makedirs(d)
        paths = []
        for f, n in zip(files, names):
            p = os.path.join(d, n)
            f.save(p)
            paths.append(p)
        if kind == "audio":
            paths.sort()  # tracks sorted by file name
        try:
            warnings = BurnPlan(kind, paths, os.path.join(d, "work")).validate() if kind != "audio" else []
            tracks = _audio_tracks(paths) if kind == "audio" else []
        except (BurnError, audiomod.AudioError) as e:
            shutil.rmtree(d, ignore_errors=True)
            return jsonify(error=str(e)), 400
        return jsonify(id=source_id, kind=kind, files=[os.path.basename(p) for p in paths], warnings=warnings,
                       tracks=tracks)

    def _audio_tracks(paths: list[str]) -> list[dict]:
        """Per file: format, length, peaks and what happens during conversion."""
        out = []
        for p in paths:
            info = audiomod.probe(p)
            try:
                audiomod.measure(info)
            except Exception:  # noqa: BLE001 - measuring is a help, not a requirement
                pass
            out.append(info.as_dict())
        return out

    @app.post("/api/source/<source_id>/files")
    @login_required
    def add_source_files(source_id):
        """Add tracks to an existing audio source (the user chooses the order)."""
        existing = _source_paths(source_id)
        if _detect_kind([os.path.basename(p) for p in existing]) != "audio":
            return jsonify(error="files can only be added to an audio CD"), 400
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            return jsonify(error="no files received"), 400
        d = os.path.dirname(existing[0])
        added = []
        for i, f in enumerate(files):
            n = secure_filename(f.filename) or f"track{i}.wav"
            if os.path.splitext(n)[1].lower() not in AUDIO_EXT:
                return jsonify(error=f"{n} is not an audio file"), 400
            base, ext = os.path.splitext(n)
            k = 1
            while os.path.exists(os.path.join(d, n)):
                k += 1
                n = f"{base}-{k}{ext}"
            p = os.path.join(d, n)
            f.save(p)
            added.append(p)
        try:
            tracks = _audio_tracks(added)
        except audiomod.AudioError as e:
            for p in added:
                os.remove(p)
            return jsonify(error=str(e)), 400
        return jsonify(id=source_id, kind="audio", tracks=tracks)

    def _source_paths(source_id: str) -> list[str]:
        if not ID_RE.match(source_id):
            abort(400)
        d = os.path.join(uploads, f"src-{source_id}")
        if not os.path.isdir(d):
            abort(404)
        return sorted(os.path.join(d, n) for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)))

    # ---- jobs -------------------------------------------------------------------
    @app.post("/api/jobs")
    @login_required
    def create_job():
        body = request.get_json(silent=True) or {}
        image_id = body.get("image_id") or ""
        source_id = body.get("source_id") or ""
        if not image_id and not source_id:
            return jsonify(error="choose an image to print, a source to burn, or both"), 400
        image_path = _image_path(image_id) if image_id else None
        sources = _source_paths(source_id) if source_id else None
        res, code = _make_job(body, image_path, image_id or None, sources)
        return jsonify(res), code

    def _make_job(body: dict, image_path: str | None, image_key: str | None,
                  sources: list[str] | None) -> tuple[dict, int]:
        """Create a job from a label (file) and source files; also used by releases."""
        def err(msg):
            return {"error": msg}, 400
        try:
            copies = int(body.get("copies", 1))
            in_bin = int(body.get("input_bin", 1))
            out_bin = runner.resolve_output(body.get("output_bin", "auto"), copies)
            rej = body.get("reject_bin")
            rej = int(rej) if rej not in (None, "", "0", 0) else None
        except (TypeError, ValueError):
            return err("invalid numbers")
        if not 1 <= copies <= 50:
            return err("copies must be 1 to 50")
        if in_bin not in (1, 2, 3) or (out_bin != "side" and out_bin not in (1, 2, 3)) \
                or (rej is not None and rej not in (1, 2, 3)):
            return err("bins are 1, 2 or 3; the output can also be the side bin")
        if in_bin == out_bin or (rej is not None and rej == in_bin):
            return err("the input bin must differ from the output bin and the reject bin")
        ribbon = body.get("ribbon", "auto")
        if ribbon not in RIBBONS:
            return err("unknown ribbon")

        params = {"copies": copies, "ribbon": ribbon, "input_bin": in_bin, "output_bin": out_bin,
                  "reject_bin": rej, "source_kind": "none", "source_paths": [], "label_path": None,
                  "titles": [str(t)[:80] for t in body.get("titles", [])][:99],
                  "album": str(body.get("album", ""))[:80], "artist": str(body.get("artist", ""))[:80]}
        if sources:
            paths = list(sources)
            kind = _detect_kind([os.path.basename(p) for p in paths])
            if kind == "audio" and body.get("tracks"):
                try:
                    paths, audio_opts = _audio_job(paths, body)
                except (ValueError, TypeError, BurnError) as e:
                    return err(str(e))
                params["titles"] = audio_opts.pop("titles")
                params["audio"] = audio_opts
            params["source_paths"] = paths
            params["source_kind"] = kind
            params["source_files"] = [os.path.basename(p) for p in paths]
            if kind == "audio":
                try:
                    warn = BurnPlan(kind, paths, os.path.join(data, "tmp"), titles=params["titles"],
                                    album=params["album"], artist=params["artist"],
                                    **{k: v for k, v in params.get("audio", {}).items()
                                       if k in ("performers", "pauses", "gapless", "dither", "gain_db",
                                                "isrc", "upc", "copy_permitted")}).validate()
                except BurnError as e:
                    return err(str(e))
                params["audio_warnings"] = warn

        title = str(body.get("title", "")).strip()[:80]
        job_id = store.create(params, title, status="preparing")
        job_dir = os.path.join(data, "jobs", job_id)
        os.makedirs(job_dir, exist_ok=True)
        if image_path:
            opts = _label_opts(body.get("label", {}))
            try:
                with Image.open(image_path) as im:
                    label = lbl.prepare_label(im, **opts)
            except Exception as e:  # noqa: BLE001
                store.append_log(job_id, f"creating the label failed: {e}")
                store.set_status(job_id, "failed")
                return err(f"creating the label failed: {e}")
            label_path = os.path.join(job_dir, "label.png")
            label.save(label_path)
            lbl.render_preview(label, size=600, print_hub=opts["print_hub"], outer_mm=opts["outer_mm"],
                               inner_mm=opts["inner_mm"]).save(
                os.path.join(job_dir, "preview.png"))
            params["label_path"] = label_path
            params["label_opts"] = {**opts, "background": list(opts["background"])}
            try:
                params["artwork_checks"] = [c for c in _artwork_checks(_load_source(image_path, image_key), opts)
                                            if c["level"] != "info"]
            except Exception:  # noqa: BLE001 - the check is only a help
                params["artwork_checks"] = []
        store.update_params(job_id, params)
        store.append_log(job_id, "job created")
        for w in params.get("audio_warnings", []):
            store.append_log(job_id, f"warning: {w}")
        for c in params.get("artwork_checks", []):
            store.append_log(job_id, f"label, {c['level']}: {c['message']}")
        store.set_status(job_id, "queued")
        worker.notify()
        return {"id": job_id}, 201

    def _audio_job(paths: list[str], body: dict) -> tuple[list[str], dict]:
        """Track list and audio options from the job (order, titles, pauses, codes)."""
        by_name = {os.path.basename(p): p for p in paths}
        tracks = body.get("tracks") or []
        if not isinstance(tracks, list) or not tracks:
            raise ValueError("no tracks chosen")
        if len(tracks) > audiomod.MAX_TRACKS:
            raise ValueError(f"at most {audiomod.MAX_TRACKS} tracks")
        ordered, titles, performers, pauses, isrc = [], [], [], [], []
        for t in tracks:
            name = str(t.get("file", ""))
            if name not in by_name:
                raise ValueError(f"unknown file in the track list: {name}")
            ordered.append(by_name[name])
            titles.append(str(t.get("title", ""))[:80])
            performers.append(str(t.get("performer", ""))[:80])
            pause = float(t.get("pause", 2))
            if not 0 <= pause <= audiomod.MAX_PAUSE_S:
                raise ValueError(f"pause must be between 0 and {audiomod.MAX_PAUSE_S:.0f} seconds")
            pauses.append(pause)
            isrc.append(str(t.get("isrc", ""))[:20])
        a = body.get("audio") or {}
        speed = a.get("speed")
        speed = int(speed) if speed not in (None, "", "max") else None
        if speed is not None and not 1 <= speed <= 52:
            raise ValueError("invalid write speed")
        dither = str(a.get("dither", "triangular"))
        if dither not in audiomod.DITHER_METHODS:
            raise ValueError("unknown dither method")
        opts = {"titles": titles, "performers": performers, "pauses": pauses, "isrc": isrc,
                "gapless": bool(a.get("gapless", False)), "dither": dither,
                "gain_db": float(a.get("gain_db", 0) or 0), "upc": str(a.get("upc", ""))[:20],
                "copy_permitted": bool(a.get("copy_permitted", True)), "speed": speed}
        return ordered, opts

    def _job_json(j: dict) -> dict:
        p = j["params"]
        return {"id": j["id"], "title": j["title"], "status": j["status"], "created": j["created"],
                "updated": j["updated"], "copies": p.get("copies"), "ribbon": p.get("ribbon"),
                "source_kind": p.get("source_kind"), "source_files": p.get("source_files", []),
                "has_label": bool(p.get("label_path")), "current": worker.current_job == j["id"]}

    @app.get("/api/jobs")
    @login_required
    def list_jobs():
        return jsonify(jobs=[_job_json(j) for j in store.list()])

    @app.get("/api/jobs/<job_id>")
    @login_required
    def get_job(job_id):
        j = store.get(job_id)
        if not j:
            abort(404)
        prog = worker.progress(job_id) or j.get("progress")
        return jsonify({**_job_json(j), "log": j["log"], "progress": prog})

    @app.post("/api/jobs/<job_id>/answer")
    @login_required
    def answer_job(job_id):
        """Operator choice after an error (Retry, Reject disc, Stop job, Yes, No)."""
        if not store.get(job_id):
            abort(404)
        body = request.get_json(silent=True) or {}
        err = worker.answer(job_id, str(body.get("choice", "")))
        if err:
            return jsonify(error=err), 409
        return jsonify(ok=True)

    @app.post("/api/machine/sim-fault")
    @login_required
    def sim_fault():
        """Dry run only: put a fault in the simulated machine to see how it is handled."""
        if worker.hardware:
            return jsonify(error="dry run only"), 409
        body = request.get_json(silent=True) or {}
        fault = str(body.get("fault", ""))
        with worker.state.lock:
            st = worker.state
            if fault == "door":
                st.door_open, st.door_polls = True, 8
            elif fault == "bin_empty":
                st.bins[200] = 0
            elif fault == "ribbon_out":
                st.faults["printer_error"] = 1
                st.printer_error_code = 21
            elif fault == "cover_open":
                st.faults["printer_error"] = 1
                st.printer_error_code = 24
            elif fault == "tray_open":
                st.drives[242]["tray_open"] = True          # someone opened a tray outside the software
            elif fault in ("clamp", "burn", "label_lost", "print_zero", "tray_stuck", "pick_bin"):
                st.faults[fault] = st.faults.get(fault, 0) + (3 if fault == "clamp" else 1)
            else:
                return jsonify(error="unknown fault"), 400
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/stop")
    @login_required
    def stop_job(job_id):
        if not store.get(job_id):
            abort(404)
        return jsonify(ok=worker.stop_job(job_id))

    @app.delete("/api/jobs/<job_id>")
    @login_required
    def delete_job(job_id):
        j = store.get(job_id)
        if not j:
            abort(404)
        if j["status"] in ("queued", "running", "preparing"):
            return jsonify(error="stop the job first"), 409
        store.delete(job_id)
        shutil.rmtree(os.path.join(data, "jobs", job_id), ignore_errors=True)
        shutil.rmtree(os.path.join(data, "work", job_id), ignore_errors=True)
        return jsonify(ok=True)

    @app.get("/jobs/<job_id>/preview.png")
    @login_required
    def job_preview(job_id):
        if not ID_RE.match(job_id):
            abort(400)
        p = os.path.join(data, "jobs", job_id, "preview.png")
        if not os.path.exists(p):
            abort(404)
        return send_file(p, mimetype="image/png", max_age=0)

    @app.get("/jobs/<job_id>/printjob.png")
    @login_required
    def job_printjob_preview(job_id):
        """Image after colour conversion, as the printer receives it (approximation)."""
        if not ID_RE.match(job_id):
            abort(400)
        p = os.path.join(data, "work", job_id, "printjob-preview.png")
        if not os.path.exists(p):
            abort(404)
        return send_file(p, mimetype="image/png", max_age=0)

    # ---- machine ---------------------------------------------------------------
    @app.get("/api/machine")
    @login_required
    def machine():
        from ..robot import sense
        if worker.hardware:
            info = {"mode": "real", **worker.hw_status()}
            info["current_job"] = worker.current_job
            info["progress"] = worker.progress(worker.current_job) if worker.current_job else None
            info.update(_printer_supplies())
            return jsonify(info)
        st = worker.state
        with st.lock:
            info = {
                "mode": "dry-run",
                "bins": {str(k - 199): v for k, v in st.bins.items()},
                "gripper": st.gripper,
                "drives": {str(k): dict(v) for k, v in st.drives.items()},
                "drawer_open": st.drawer_open,
                "printer_disc": st.printer_disc,
                "printer_sense": st.printer_sense,
                "printer_text": sense.printer_text(st.printer_sense),
                "changer_sense": st.changer_sense,
                "changer_text": sense.changer_text(st.changer_sense),
                "awake": st.awake,
            }
        info["current_job"] = worker.current_job
        info["progress"] = worker.progress(worker.current_job) if worker.current_job else None
        info.update(_printer_supplies())
        return jsonify(info)

    supplies_cache: dict = {}

    def _printer_supplies() -> dict:
        """Ribbon, supply and film via ESC!r3C and ESC!r6C. On the real printer at most every 10 s,
        and during a job only the last known values."""
        import time as _time
        if worker.hardware:
            if worker.printer is None:
                return {}
            age = _time.monotonic() - supplies_cache.get("t", -1e9)
            if worker.current_job or age < 10:
                return supplies_cache.get("data", {})
            supplies_cache["data"] = _read_supplies()
            supplies_cache["t"] = _time.monotonic()
            return supplies_cache["data"]
        return _read_supplies()

    def _read_supplies() -> dict:
        from ..everest import job as ej
        try:
            st3 = ej.parse_r3(worker.printer.query(ej.status_command("r3"), 13))
            cons = ej.parse_r6(worker.printer.query(ej.status_command("r6"), 20))
        except Exception as e:  # noqa: BLE001
            return {"supplies_error": str(e)}
        rib = ej.ribbon_from_status(st3)
        return {"ribbon": rib, "ribbon_name": ej.RIBBON_NAMES.get(rib or "", "no ribbon or unknown ribbon"),
                "ribbon_remaining": cons.ribbon_remaining, "ribbon_percent": cons.ribbon_percent(rib),
                "film_percent": cons.film_percent, "film_present": st3.film != "E",
                "film_remaining": cons.film_value,
                "printer_counter": cons.counter_a, "printer_state": st3.text}

    @app.post("/api/machine/sim-ribbon")
    @login_required
    def sim_ribbon():
        """Dry run only: put a different ribbon in the simulated printer."""
        if worker.hardware:
            return jsonify(error="dry run only"), 409
        body = request.get_json(silent=True) or {}
        codes = {"cmy": ("0", 500), "cmyw": ("1", 325), "mono": ("2", 1000), "none": ("E", 0)}
        if body.get("ribbon") not in codes:
            return jsonify(error="unknown ribbon"), 400
        code, cap = codes[body["ribbon"]]
        with worker.state.lock:
            worker.state.ribbon_code = code
            worker.state.ribbon_remaining = cap
        return jsonify(ok=True)

    @app.post("/api/machine/reset")
    @login_required
    def machine_reset():
        if worker.hardware:
            return jsonify(error="dry run only"), 409
        if worker.current_job:
            return jsonify(error="a job is running"), 409
        worker.reset_simulation()
        return jsonify(ok=True)


    # ---- releases (everything for a CD in one directory; made again on demand) ------
    def _release_summary(m: dict) -> dict:
        info = m.get("tracks_info") or []
        hist = m.get("history") or []
        return {"id": m["id"], "name": m["name"], "artist": m.get("artist", ""), "album": m.get("album", ""),
                "kind": m.get("source", {}).get("kind", "none"), "tracks": len(m.get("tracks") or []),
                "duration_s": round(sum(t.get("duration_s", 0) for t in info)), "has_label": bool(m.get("artwork")),
                "updated": m.get("updated"), "made": sum(h.get("copies", 0) for h in hist),
                "last_made": hist[-1]["date"] if hist else None, "job_defaults": m.get("job_defaults", {})}

    @app.get("/api/releases")
    @login_required
    def list_releases():
        return jsonify(releases=[_release_summary(m) for m in releases.list()])

    @app.post("/api/releases")
    @login_required
    def save_release():
        """Save the current job as a release (new, or update with release_id)."""
        body = request.get_json(silent=True) or {}
        image_id, source_id = body.get("image_id") or "", body.get("source_id") or ""
        if not image_id and not source_id:
            return jsonify(error="nothing to save: choose artwork, music or both"), 400
        artwork = _image_path(image_id) if image_id else None
        sources, kind, tracks, tracks_info, audio = [], "none", [], [], {}
        if source_id:
            sources = _source_paths(source_id)
            kind = _detect_kind([os.path.basename(p) for p in sources])
            if kind == "audio":
                try:
                    sources, opts = _audio_job(sources, body)
                except (ValueError, TypeError, BurnError) as e:
                    return jsonify(error=str(e)), 400
                titles = opts.pop("titles")
                tracks = [{"file": os.path.basename(p), "title": titles[i], "performer": opts["performers"][i],
                           "pause": opts["pauses"][i], "isrc": opts["isrc"][i]} for i, p in enumerate(sources)]
                audio = {k: v for k, v in opts.items() if k not in ("performers", "pauses", "isrc")}
                try:
                    tracks_info = _audio_tracks(sources)
                except audiomod.AudioError as e:
                    return jsonify(error=str(e)), 400
        label = body.get("label") if isinstance(body.get("label"), dict) else {}
        preview = None
        if artwork:
            opts = _label_opts(label)
            img = lbl.prepare_label(_load_source(artwork, image_id)["img"], **opts, canvas=600, srgb=True)
            buf = io.BytesIO()
            lbl.render_preview(img, size=600, print_hub=opts["print_hub"], outer_mm=opts["outer_mm"],
                               inner_mm=opts["inner_mm"]).save(buf, "PNG")
            preview = buf.getvalue()
        meta = {"name": str(body.get("name", "")).strip()[:80], "artist": str(body.get("artist", ""))[:80],
                "album": str(body.get("album", ""))[:80], "label": label, "tracks": tracks, "audio": audio,
                "tracks_info": tracks_info, "source": {"kind": kind},
                "job_defaults": {k: body.get(k) for k in ("ribbon", "input_bin", "output_bin", "reject_bin", "copies")}}
        try:
            rid = releases.save(meta, rid=body.get("release_id") or None, artwork=artwork, sources=sources,
                                preview_png=preview)
        except ReleaseError as e:
            return jsonify(error=str(e)), 400
        return jsonify(id=rid, name=meta["name"]), 201

    def _release_or_404(rid: str) -> dict:
        try:
            return releases.get(rid)
        except ReleaseError:
            abort(404)

    @app.get("/api/releases/<rid>")
    @login_required
    def get_release(rid):
        return jsonify(_release_or_404(rid))

    @app.get("/api/releases/<rid>/preview.png")
    @login_required
    def release_preview(rid):
        _release_or_404(rid)
        p = os.path.join(releases.path(rid), "preview.png")
        if not os.path.isfile(p):
            abort(404)
        return send_file(p, mimetype="image/png", max_age=0)

    @app.get("/api/releases/<rid>/zip")
    @login_required
    def release_zip(rid):
        _release_or_404(rid)
        tmp = os.path.join(data, "work", f"release-{rid}.zip")
        releases.zip_to(rid, tmp)
        return send_file(tmp, mimetype="application/zip", as_attachment=True, download_name=f"{rid}.zip")

    @app.delete("/api/releases/<rid>")
    @login_required
    def delete_release(rid):
        _release_or_404(rid)
        releases.delete(rid)
        return jsonify(ok=True)

    @app.post("/api/releases/<rid>/jobs")
    @login_required
    def make_release(rid):
        """Make a release: a job with the saved settings and the requested number of discs."""
        m = _release_or_404(rid)
        body = request.get_json(silent=True) or {}
        defaults = m.get("job_defaults") or {}
        job_body = {"label": m.get("label") or {}, "album": m.get("album", ""), "artist": m.get("artist", ""),
                    "title": f"{m['name']}", "tracks": m.get("tracks") or None, "audio": m.get("audio") or {}}
        for k in ("copies", "ribbon", "input_bin", "output_bin", "reject_bin"):
            v = body.get(k, defaults.get(k))
            if v is not None:
                job_body[k] = v
        res, code = _make_job(job_body, releases.artwork_path(rid), f"release:{rid}",
                              releases.source_paths(rid) or None)
        if code == 201:
            releases.add_history(rid, {"job": res["id"], "copies": int(job_body.get("copies", 1))})
            store.append_log(res["id"], f"made from release '{m['name']}' ({rid})")
        return jsonify(res), code

    @app.post("/api/releases/<rid>/open")
    @login_required
    def open_release(rid):
        """Load a release into the job page: its files become uploads again, with all settings."""
        m = _release_or_404(rid)
        out = {"release": m, "image_id": None, "source_id": None}
        art = releases.artwork_path(rid)
        if art:
            image_id = uuid.uuid4().hex[:12]
            _link_or_copy_file(art, os.path.join(uploads, f"img-{image_id}{os.path.splitext(art)[1].lower()}"))
            src = _preview_source(image_id)
            out.update(image_id=image_id, image={"width": src["size"][0], "height": src["size"][1],
                                                  "info": src["info"], "name": os.path.basename(art)})
        paths = releases.source_paths(rid)
        if paths:
            source_id = uuid.uuid4().hex[:12]
            d = os.path.join(uploads, f"src-{source_id}")
            os.makedirs(d)
            for p in paths:
                _link_or_copy_file(p, os.path.join(d, os.path.basename(p)))
            out["source_id"] = source_id
            out["kind"] = m.get("source", {}).get("kind")
            out["files"] = [os.path.basename(p) for p in paths]
        return jsonify(out)

    def _link_or_copy_file(src: str, dst: str) -> None:
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)

    @app.get("/api/image/<image_id>/file")
    @login_required
    def image_file(image_id):
        """The original file, so the browser can draw the preview live."""
        return send_file(_image_path(image_id), max_age=0)

    return app
