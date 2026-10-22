"""Releases: everything needed to make a CD again, in one directory (print on demand).

Layout of a release (`<data dir>/releases/<id>/`):

    release.json     name, artist, album, label settings (including the disc type), audio options,
                     track list (file, title, pause, ISRC), measurements per track, defaults
                     for a job (ribbon, bins) and the history of jobs made from it
    artwork.<ext>    the original artwork, unchanged
    tracks/          the original source files (WAV, FLAC, ISO ...), unchanged
    preview.png      preview of the printed disc (for display only)

Everything in the directory is plain and readable. You can copy the directory, keep it in a
zip or restore it. Files are added as hard links where possible (saves space and time),
otherwise they are copied.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import unicodedata
import uuid
import zipfile

FORMAT = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class ReleaseError(ValueError):
    pass


def slugify(name: str) -> str:
    t = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:60] or "release"


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


class ReleaseStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    # -- paths ------------------------------------------------------------
    def path(self, rid: str) -> str:
        if not ID_RE.match(rid or ""):
            raise ReleaseError("invalid release")
        return os.path.join(self.root, rid)

    def exists(self, rid: str) -> bool:
        return os.path.isfile(os.path.join(self.path(rid), "release.json"))

    def artwork_path(self, rid: str) -> str | None:
        meta = self.get(rid)
        return os.path.join(self.path(rid), meta["artwork"]) if meta.get("artwork") else None

    def source_paths(self, rid: str) -> list[str]:
        meta = self.get(rid)
        d = os.path.join(self.path(rid), "tracks")
        return [os.path.join(d, f) for f in meta.get("source", {}).get("files", [])]

    # -- reading ------------------------------------------------------------
    def get(self, rid: str) -> dict:
        p = os.path.join(self.path(rid), "release.json")
        if not os.path.isfile(p):
            raise ReleaseError("release not found")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def list(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.root)):
            if ID_RE.match(name) and os.path.isfile(os.path.join(self.root, name, "release.json")):
                try:
                    out.append(self.get(name))
                except (OSError, ValueError):
                    continue
        return sorted(out, key=lambda m: m.get("updated", 0), reverse=True)

    # -- writing ------------------------------------------------------------
    def new_id(self, name: str) -> str:
        base = slugify(name)
        rid, n = base, 1
        while os.path.exists(os.path.join(self.root, rid)):
            n += 1
            rid = f"{base}-{n}"
        return rid

    def save(self, meta: dict, *, rid: str | None, artwork: str | None, sources: list[str],
             preview_png: bytes | None = None) -> str:
        """Save or update a release. artwork and sources are existing files (uploads)."""
        name = str(meta.get("name", "")).strip()
        if not name:
            raise ReleaseError("give the release a name")
        if rid and not self.exists(rid):
            raise ReleaseError("release not found")
        rid = rid or self.new_id(name)
        final = self.path(rid)
        old = self.get(rid) if os.path.isdir(final) else {}
        stage = os.path.join(self.root, f".{rid}.{uuid.uuid4().hex[:6]}")
        os.makedirs(os.path.join(stage, "tracks"))
        try:
            art_name = None
            if artwork:
                art_name = "artwork" + os.path.splitext(artwork)[1].lower()
                _link_or_copy(artwork, os.path.join(stage, art_name))
            files = []
            for src in sources:
                fn = os.path.basename(src)
                if fn in files:
                    raise ReleaseError(f"duplicate file name: {fn}")
                _link_or_copy(src, os.path.join(stage, "tracks", fn))
                files.append(fn)
            if preview_png:
                with open(os.path.join(stage, "preview.png"), "wb") as f:
                    f.write(preview_png)
            now = time.time()
            meta = {**meta, "format": FORMAT, "id": rid, "name": name, "artwork": art_name,
                    "source": {**meta.get("source", {}), "files": files},
                    "created": old.get("created", now), "updated": now,
                    "history": old.get("history", [])}
            with open(os.path.join(stage, "release.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            if os.path.isdir(final):
                trash = final + f".old-{uuid.uuid4().hex[:6]}"
                os.replace(final, trash)
                os.replace(stage, final)
                shutil.rmtree(trash, ignore_errors=True)
            else:
                os.replace(stage, final)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return rid

    def add_history(self, rid: str, entry: dict) -> None:
        meta = self.get(rid)
        meta.setdefault("history", []).append({**entry, "date": time.time()})
        with open(os.path.join(self.path(rid), "release.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def delete(self, rid: str) -> None:
        p = self.path(rid)
        if not os.path.isdir(p):
            raise ReleaseError("release not found")
        shutil.rmtree(p)

    def zip_to(self, rid: str, dest: str) -> str:
        """The whole release as a zip (directory with release.json, artwork and tracks)."""
        base = self.path(rid)
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as z:
            for dirpath, _dirs, filenames in os.walk(base):
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    z.write(full, os.path.join(rid, os.path.relpath(full, base)))
        return dest
