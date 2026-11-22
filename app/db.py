"""SQLite 스키마와 연결 관리."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL DEFAULT '',
    model            TEXT DEFAULT '',
    android_version  TEXT DEFAULT '',
    app_version      TEXT DEFAULT '',
    battery          INTEGER,
    network          TEXT DEFAULT '',
    note             TEXT DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_seen        TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT NOT NULL DEFAULT '',
    type                   TEXT NOT NULL CHECK (type IN ('call', 'sms')),
    numbers                TEXT NOT NULL,
    repeat_count           INTEGER NOT NULL DEFAULT 1,
    order_mode             TEXT NOT NULL DEFAULT 'sequence',
    max_call_duration_sec  INTEGER,
    sms_body               TEXT,
    start_at               TEXT NOT NULL,
    recurrence             TEXT NOT NULL DEFAULT 'once',
    device_id              TEXT,
    status                 TEXT NOT NULL DEFAULT 'active',
    next_run_at            TEXT,
    last_run_at            TEXT,
    run_count              INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id   INTEGER,
    schedule_name TEXT DEFAULT '',
    type          TEXT NOT NULL DEFAULT 'call',
    scheduled_for TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    total_tasks   INTEGER NOT NULL DEFAULT 0,
    done_tasks    INTEGER NOT NULL DEFAULT 0,
    failed_tasks  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL,
    schedule_id    INTEGER,
    schedule_name  TEXT DEFAULT '',
    type           TEXT NOT NULL,
    phone_number   TEXT NOT NULL,
    seq            INTEGER NOT NULL DEFAULT 0,
    repeat_index   INTEGER NOT NULL DEFAULT 1,
    max_call_duration_sec INTEGER,
    sms_body       TEXT,
    device_id      TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    result         TEXT,
    error          TEXT,
    duration_sec   REAL,
    claimed_at     TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_inbox (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id          TEXT,
    sender             TEXT DEFAULT '',
    body               TEXT DEFAULT '',
    received_at        TEXT,
    created_at         TEXT NOT NULL,
    telegram_status    TEXT NOT NULL DEFAULT 'pending',
    telegram_error     TEXT,
    telegram_attempts  INTEGER NOT NULL DEFAULT 0,
    telegram_sent_at   TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_device ON tasks(device_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_runs_schedule ON runs(schedule_id);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_sms_created ON sms_inbox(created_at);
CREATE INDEX IF NOT EXISTS idx_sms_telegram ON sms_inbox(telegram_status);
"""

DEFAULT_SETTINGS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_enabled": "1",
    "sms_forward_enabled": "1",
}


def db_path() -> Path:
    return config.DB_PATH


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else config.DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(path: str | Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (key, value),
            )
    finally:
        conn.close()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    data = {row["key"]: (row["value"] or "") for row in conn.execute("SELECT key, value FROM settings")}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return merged


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value),
    )


def log(conn: sqlite3.Connection, message: str, level: str = "info") -> None:
    from .timeutil import now_iso

    conn.execute(
        "INSERT INTO logs(level, message, created_at) VALUES (?, ?, ?)",
        (level, message[:2000], now_iso()),
    )
