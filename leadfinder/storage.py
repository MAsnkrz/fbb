"""SQLite persistence for runs, leads, presets and logs (thread-safe, single file)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CATALOG_DIR = DATA_DIR / "catalogues"
CATALOG_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, created REAL, finished REAL,
  status TEXT, stage TEXT, progress REAL DEFAULT 0,
  params TEXT, summary TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS job_log (
  job_id INTEGER, ts REAL, msg TEXT
);
CREATE TABLE IF NOT EXISTS leads (
  job_id INTEGER, asin TEXT, data TEXT,
  PRIMARY KEY (job_id, asin)
);
CREATE TABLE IF NOT EXISTS presets (
  name TEXT PRIMARY KEY, params TEXT, updated REAL
);
"""


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or DATA_DIR / "leadfinder.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, args: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # ---------------------------------------------------------------- jobs
    def create_job(self, name: str, params: dict) -> int:
        cur = self._exec("INSERT INTO jobs (name, created, status, stage, params) VALUES (?,?,?,?,?)",
                         (name, time.time(), "queued", "queued", json.dumps(params)))
        return cur.lastrowid

    def update_job(self, job_id: int, **fields):
        if not fields:
            return
        if "summary" in fields and not isinstance(fields["summary"], str):
            fields["summary"] = json.dumps(fields["summary"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def get_job(self, job_id: int) -> dict | None:
        rows = self._all("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        j = rows[0]
        j["params"] = json.loads(j["params"] or "{}")
        j["summary"] = json.loads(j["summary"]) if j.get("summary") else {}
        return j

    def list_jobs(self, limit: int = 50) -> list[dict]:
        rows = self._all("SELECT id, name, created, finished, status, stage, progress, summary, error "
                         "FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["summary"] = json.loads(r["summary"]) if r.get("summary") else {}
        return rows

    def delete_job(self, job_id: int):
        for t in ("leads", "job_log"):
            self._exec(f"DELETE FROM {t} WHERE job_id=?", (job_id,))
        self._exec("DELETE FROM jobs WHERE id=?", (job_id,))

    def mark_interrupted(self):
        self._exec("UPDATE jobs SET status='interrupted', error='App restarted while this run was active' "
                   "WHERE status IN ('queued','running','cancelling')")

    def log(self, job_id: int, msg: str):
        self._exec("INSERT INTO job_log (job_id, ts, msg) VALUES (?,?,?)", (job_id, time.time(), msg))

    def get_log(self, job_id: int, limit: int = 200) -> list[dict]:
        rows = self._all("SELECT ts, msg FROM job_log WHERE job_id=? ORDER BY rowid DESC LIMIT ?",
                         (job_id, limit))
        return list(reversed(rows))

    # --------------------------------------------------------------- leads
    def save_leads(self, job_id: int, leads: list[dict]):
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO leads (job_id, asin, data) VALUES (?,?,?)",
                [(job_id, l["asin"], json.dumps(l)) for l in leads])
            self._conn.commit()

    def get_leads(self, job_id: int) -> list[dict]:
        return [json.loads(r["data"]) for r in
                self._all("SELECT data FROM leads WHERE job_id=?", (job_id,))]

    # ------------------------------------------------------------- presets
    def save_preset(self, name: str, params: dict):
        self._exec("INSERT OR REPLACE INTO presets (name, params, updated) VALUES (?,?,?)",
                   (name, json.dumps(params), time.time()))

    def list_presets(self) -> dict[str, dict]:
        return {r["name"]: json.loads(r["params"]) for r in
                self._all("SELECT name, params FROM presets ORDER BY name")}

    def delete_preset(self, name: str):
        self._exec("DELETE FROM presets WHERE name=?", (name,))
