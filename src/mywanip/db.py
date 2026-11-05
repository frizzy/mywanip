from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dns_records (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    hostname TEXT NOT NULL,
    zone TEXT NOT NULL,
    secret TEXT NOT NULL,
    ttl INTEGER NOT NULL DEFAULT 300,
    proxied INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_ip TEXT, last_status TEXT, last_at TEXT
);
CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    secret TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_ip TEXT, last_status TEXT, last_at TEXT
);
"""

DEFAULTS = {
    "poll_interval": 300,
    "sources": [
        "https://api.ipify.org",
        "https://ipv4.icanhazip.com",
        "https://checkip.amazonaws.com",
    ],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    """Thin sqlite3 wrapper. Table and column names come from code, never from requests.

    shortcut: one shared connection used from the event loop thread only, no
    pooling; fine for a handful of rows, revisit if handlers go multi-threaded.
    """

    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        if path != ":memory:":
            os.chmod(path, 0o600)  # holds provider tokens in plaintext

    def get(self, key: str):
        row = self.c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else DEFAULTS.get(key)

    def set(self, key: str, value) -> None:
        self.c.execute(
            "INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.c.commit()

    def rows(self, table: str) -> list[dict]:
        return [dict(r) for r in self.c.execute(f"SELECT * FROM {table} ORDER BY id")]

    def row(self, table: str, id: int) -> dict | None:
        r = self.c.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone()
        return dict(r) if r else None

    def insert(self, table: str, **f) -> int:
        cur = self.c.execute(
            f"INSERT INTO {table} ({','.join(f)}) VALUES ({','.join('?' * len(f))})",
            list(f.values()),
        )
        self.c.commit()
        return cur.lastrowid

    def update(self, table: str, id: int, **f) -> None:
        if f:
            self.c.execute(
                f"UPDATE {table} SET {','.join(k + '=?' for k in f)} WHERE id=?",
                [*f.values(), id],
            )
            self.c.commit()

    def delete(self, table: str, id: int) -> bool:
        cur = self.c.execute(f"DELETE FROM {table} WHERE id=?", (id,))
        self.c.commit()
        return cur.rowcount > 0
