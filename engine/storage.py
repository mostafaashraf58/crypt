"""Local SQLite state, idempotency and append-only audit records."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


class Store:
    def __init__(self, data_dir: str | Path | None = None):
        self.lock = RLock()
        if data_dir is None:
            import os
            data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Axiom"
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.instance_file = open(self.directory / "engine.lock", "a+b")
        try:
            import os
            if os.name == "nt":
                import msvcrt
                self.instance_file.seek(0)
                if not self.instance_file.read(1):
                    self.instance_file.write(b"0")
                    self.instance_file.flush()
                self.instance_file.seek(0)
                msvcrt.locking(self.instance_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.instance_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.instance_file.close()
            raise RuntimeError("An Axiom engine is already using this data directory") from None
        self.db = sqlite3.connect(self.directory / "axiom.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        self.db.commit()

    def load(self) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key='engine'").fetchone()
            return json.loads(row[0]) if row else None

    def event(self, event_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT result FROM events WHERE id=?", (event_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, state: dict, timestamp: str, event: str, payload: Any,
             event_id: str | None = None, result: dict | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO state VALUES ('engine',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(state, allow_nan=False),))
            if event_id:
                self.db.execute("INSERT INTO events VALUES (?,?)", (event_id, json.dumps(result, allow_nan=False)))
            self.db.execute("INSERT INTO audit(timestamp,event,payload) VALUES (?,?,?)", (timestamp, event, json.dumps(payload, allow_nan=False)))

    def audit(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM audit ORDER BY sequence DESC LIMIT ?", (min(max(int(limit), 1), 1000),)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def close(self) -> None:
        with self.lock:
            self.db.close()
            self.instance_file.close()
