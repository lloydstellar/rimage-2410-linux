"""Job queue in SQLite (one file in the data directory)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,          -- queued, running, done, failed, stopped
    title TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL,          -- JSON
    log TEXT NOT NULL DEFAULT '',
    progress TEXT NOT NULL DEFAULT ''   -- JSON: latest progress (see rimage2410/progress.py)
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
            if "progress" not in cols:      # database created before progress existed
                c.execute("ALTER TABLE jobs ADD COLUMN progress TEXT NOT NULL DEFAULT ''")
            # jobs that were still running at restart count as stopped
            c.execute("UPDATE jobs SET status='stopped', updated=? WHERE status IN ('running', 'preparing')",
                      (time.time(),))

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["progress"] = json.loads(d["progress"]) if d.get("progress") else None
        return d

    def create(self, params: dict, title: str = "", status: str = "queued") -> str:
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO jobs (id, created, updated, status, title, params) VALUES (?,?,?,?,?,?)",
                      (job_id, now, now, status, title, json.dumps(params)))
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def next_queued(self) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created ASC LIMIT 1").fetchone())

    def set_status(self, job_id: str, status: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET status=?, updated=? WHERE id=?", (status, time.time(), job_id))

    def append_log(self, job_id: str, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET log = log || ?, updated=? WHERE id=?",
                      (f"{stamp} {line}\n", time.time(), job_id))

    def set_progress(self, job_id: str, progress: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET progress=? WHERE id=?", (json.dumps(progress), job_id))

    def update_params(self, job_id: str, params: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET params=?, updated=? WHERE id=?", (json.dumps(params), time.time(), job_id))

    def delete(self, job_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
