"""SQLite 스키마와 연결 관리."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import config
from .phoneutil import normalize_phone

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL DEFAULT '',
    model            TEXT DEFAULT '',
    android_version  TEXT DEFAULT '',
    app_version      TEXT DEFAULT '',
    battery          INTEGER,
    network          TEXT DEFAULT '',
    phone_number     TEXT DEFAULT '',
    note             TEXT DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    -- 대시보드에서 직접 고친 이름·번호·모델·안드로이드 버전.
    -- 1 이면 앱이 30초마다 보내는 등록 정보가 그 값을 덮어쓰지 않는다.
    name_customized    INTEGER NOT NULL DEFAULT 0,
    phone_customized   INTEGER NOT NULL DEFAULT 0,
    model_customized   INTEGER NOT NULL DEFAULT 0,
    android_customized INTEGER NOT NULL DEFAULT 0,
    last_seen        TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT NOT NULL DEFAULT '',
    type                   TEXT NOT NULL CHECK (type IN ('call', 'sms')),
    numbers                TEXT NOT NULL,
    repeat_count           INTEGER NOT NULL DEFAULT 1,
    total_calls            INTEGER,
    order_mode             TEXT NOT NULL DEFAULT 'sequence',
    max_call_duration_sec  INTEGER,
    sms_body               TEXT,
    start_at               TEXT NOT NULL,
    -- 실행 종료일(YYYY-MM-DD). 비어 있으면 종료일 없이 계속 반복한다.
    end_date               TEXT NOT NULL DEFAULT '',
    recurrence             TEXT NOT NULL DEFAULT 'once',
    device_id              TEXT,
    target_device_ids      TEXT NOT NULL DEFAULT '[]',
    status                 TEXT NOT NULL DEFAULT 'active',
    next_run_at            TEXT,
    last_run_at            TEXT,
    run_count              INTEGER NOT NULL DEFAULT 0,
    -- 삭제 시각(소프트 삭제). 값이 있으면 웹 목록에서는 숨기고 삭제 보관함에 남긴다.
    deleted_at             TEXT DEFAULT NULL,
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
    deleted_at    TEXT DEFAULT NULL,
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
    deleted_at     TEXT DEFAULT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_inbox (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id          TEXT,
    sender             TEXT DEFAULT '',
    receiver_number    TEXT DEFAULT '',
    body               TEXT DEFAULT '',
    received_at        TEXT,
    created_at         TEXT NOT NULL,
    telegram_status    TEXT NOT NULL DEFAULT 'pending',
    telegram_error     TEXT,
    telegram_attempts  INTEGER NOT NULL DEFAULT 0,
    telegram_sent_at   TEXT,
    attachment_count   INTEGER NOT NULL DEFAULT 0,
    attachment_paths   TEXT,
    deleted_at         TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS device_aliases (
    -- 기기 키가 수신번호로 바뀌기 전의 기기 ID(예: 5656). 폰에 옛 앱이 남아 있어도
    -- 같은 기기로 이어지도록 device_id 로 풀어 준다.
    alias      TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'info',
    category   TEXT NOT NULL DEFAULT '',
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

# 로그 분류. 메시지 앞부분으로 자동 분류하고, 대시보드에서 분류별로 걸러 볼 수 있다.
LOG_CATEGORIES: dict[str, str] = {
    "device": "기기",
    "schedule": "스케줄",
    "run": "실행 기록",
    "queue": "대기열",
    "sms": "수신 문자",
    "telegram": "텔레그램",
    "settings": "설정",
    "system": "시스템",
}

_LOG_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("settings", ("설정 저장", "설정 변경")),
    ("schedule", ("스케줄",)),
    ("run", ("실행 기록", "실행 묶음")),
    ("queue", ("대기열",)),
    ("sms", ("수신 문자", "문자 전송")),
    ("device", ("기기",)),
    ("telegram", ("텔레그램",)),
)


def guess_log_category(message: str) -> str:
    """로그 메시지로 분류를 정한다(알 수 없으면 system)."""
    text = message or ""
    for key, words in _LOG_CATEGORY_RULES:
        if any(word in text for word in words):
            return key
    return "system"

DEFAULT_SETTINGS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_enabled": "1",
    "sms_forward_enabled": "1",
    "mms_forward_enabled": "1",
    # 앱(폰)에 내려보내는 원격 동작 설정. APK 재설치 없이 대시보드에서 바꿀 수 있다.
    "poll_interval_sec": "5",
    "call_gap_sec": "3",
    # 통화 중 동작: 녹음(기본 끔) · 통화 볼륨 0(기본 켬) · 내 소리 차단/마이크 음소거(기본 켬)
    "call_recording_enabled": "0",
    "call_mute_volume": "1",
    "call_mute_mic": "1",
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


def _fill_empty_device_fields(conn: sqlite3.Connection, target_id: str, source: sqlite3.Row) -> None:
    """같은 번호로 합칠 때 비어 있는 항목만 원본 값으로 채운다(손실 없이)."""
    target = conn.execute("SELECT * FROM devices WHERE id = ?", (target_id,)).fetchone()
    if target is None:
        return
    updates: list[str] = []
    values: list = []
    for column in ("name", "model", "android_version", "app_version", "note"):
        current = (target[column] or "").strip()
        # 이름이 기기 키 그대로면(자동 등록 기본값) 비어 있는 것으로 보고 원본 이름을 받아들인다.
        if column == "name" and current == target_id:
            current = ""
        if not current and (source[column] or "").strip():
            updates.append(f"{column} = ?")
            values.append(source[column])
    if target["battery"] is None and source["battery"] is not None:
        updates.append("battery = ?")
        values.append(source["battery"])
    last_seen, source_seen = target["last_seen"], source["last_seen"]
    if source_seen and (not last_seen or str(source_seen) > str(last_seen)):
        updates.append("last_seen = ?")
        values.append(source_seen)
    for column in ("name_customized", "phone_customized", "model_customized", "android_customized"):
        if source[column] and not target[column]:
            updates.append(f"{column} = 1")
    if updates:
        conn.execute(f"UPDATE devices SET {', '.join(updates)} WHERE id = ?", values + [target_id])


def _replace_device_in_target_lists(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """여러 대 지정 스케줄의 기기 목록(JSON 배열)에서 옛 기기 키를 새 키로 바꾼다."""
    for sched in conn.execute("SELECT id, target_device_ids FROM schedules").fetchall():
        targets = json_list(sched["target_device_ids"])
        if old_id not in targets:
            continue
        replaced = [new_id if item == old_id else item for item in targets]
        # 바뀐 뒤 같은 기기가 두 번 들어가지 않도록 순서는 두고 중복만 지운다.
        deduped: list[str] = []
        for item in replaced:
            if item not in deduped:
                deduped.append(item)
        conn.execute(
            "UPDATE schedules SET target_device_ids = ? WHERE id = ?",
            (json.dumps(deduped, ensure_ascii=False), sched["id"]),
        )


def move_device_records(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """기기 키를 옛 ID → 새 키(수신번호)로 한 트랜잭션에서 옮긴다.

    새 키의 기기가 이미 있으면(같은 번호가 두 번 등록된 경우) 기록을 그쪽으로 모으고
    비어 있는 항목만 채운 뒤 옛 줄을 지운다(자료 손실 없음). 옛 ID 는 device_aliases 에
    남겨 폰에 옛 앱이 남아 있어도 같은 기기로 이어진다.
    """
    if not old_id or not new_id or old_id == new_id:
        return
    source = conn.execute("SELECT * FROM devices WHERE id = ?", (old_id,)).fetchone()
    if source is None:
        return
    with transaction(conn):
        target = conn.execute("SELECT 1 FROM devices WHERE id = ?", (new_id,)).fetchone()
        if target is None:
            conn.execute("UPDATE devices SET id = ?, phone_number = ? WHERE id = ?", (new_id, new_id, old_id))
        else:
            _fill_empty_device_fields(conn, new_id, source)
            conn.execute("DELETE FROM devices WHERE id = ?", (old_id,))
        conn.execute("UPDATE tasks SET device_id = ? WHERE device_id = ?", (new_id, old_id))
        conn.execute("UPDATE schedules SET device_id = ? WHERE device_id = ?", (new_id, old_id))
        conn.execute("UPDATE sms_inbox SET device_id = ? WHERE device_id = ?", (new_id, old_id))
        _replace_device_in_target_lists(conn, old_id, new_id)
        conn.execute(
            "INSERT OR REPLACE INTO device_aliases(alias, device_id, created_at) VALUES (?, ?, ?)",
            (old_id, new_id, datetime.now().isoformat(timespec="seconds")),
        )
        log(conn, f"기기 키 전환(수신번호): {old_id} → {new_id}", "warning", category="system")


def _migrate_device_keys(conn: sqlite3.Connection) -> list[str]:
    """기기 키를 수신번호로 옮긴다(손실 없이, 한 트랜잭션에서).

    예전에는 기기 ID(예: `5656`)와 수신번호(`010-5656-3939`)가 따로 있었다.
    이제 키는 수신번호 하나뿐이므로 devices.id · schedules.device_id ·
    schedules.target_device_ids(JSON) · tasks.device_id · sms_inbox.device_id 를
    모두 정규화된 수신번호로 바꾼다. 옛 ID 는 device_aliases 에 남겨, 폰에 옛 앱이
    남아 있어도 같은 기기로 이어진다.
    """
    moved: list[str] = []
    rows = conn.execute("SELECT id, phone_number FROM devices ORDER BY created_at ASC").fetchall()
    for row in rows:
        old_id = row["id"]
        phone = normalize_phone(row["phone_number"])
        if not phone or phone == old_id:
            continue
        move_device_records(conn, old_id, phone)
        moved.append(f"devices.{old_id}→{phone}")
    return moved


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """기존 DB(운영 중인 Railway 볼륨)에 없는 컬럼을 조용히 추가한다."""
    added: list[str] = []
    schedule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
    if "total_calls" not in schedule_columns:
        # 총 발신 횟수(지정 시 번호 개수 × 반복 횟수 대신 사용). 기존 스케줄은 NULL = 기존 방식 유지.
        conn.execute("ALTER TABLE schedules ADD COLUMN total_calls INTEGER")
        added.append("schedules.total_calls")
    if "end_date" not in schedule_columns:
        # 실행 종료일(선택). 기존 스케줄은 빈 값 = 종료일 없음(지금까지처럼 계속 반복).
        conn.execute("ALTER TABLE schedules ADD COLUMN end_date TEXT NOT NULL DEFAULT ''")
        added.append("schedules.end_date")
    if "target_device_ids" not in schedule_columns:
        # 발신 기기 여러 대 지정(JSON 배열). 기존 스케줄의 device_id 는 그대로 두고 배열에도 옮겨 담는다.
        conn.execute("ALTER TABLE schedules ADD COLUMN target_device_ids TEXT NOT NULL DEFAULT '[]'")
        added.append("schedules.target_device_ids")
        # 기존 스케줄(운영 중 #2 등)이 쓰던 지정 기기를 잃지 않도록 손실 없이 채워 넣는다.
        legacy = conn.execute(
            "SELECT id, device_id FROM schedules WHERE device_id IS NOT NULL AND TRIM(device_id) <> ''"
        ).fetchall()
        for row in legacy:
            conn.execute(
                "UPDATE schedules SET target_device_ids = ? WHERE id = ?",
                (json.dumps([row["device_id"]], ensure_ascii=False), row["id"]),
            )

    device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    if "phone_number" not in device_columns:
        # 기기가 문자를 받는 번호(대시보드 “기기 관리”에서 입력). 기존 기기는 빈 값 = 미설정.
        conn.execute("ALTER TABLE devices ADD COLUMN phone_number TEXT DEFAULT ''")
        added.append("devices.phone_number")

    sms_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sms_inbox)")}
    if "receiver_number" not in sms_columns:
        # 이 문자를 받은 번호. 비어 있으면 기기의 phone_number 로 대신 보여준다.
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN receiver_number TEXT DEFAULT ''")
        added.append("sms_inbox.receiver_number")
    if "attachment_count" not in sms_columns:
        # MMS 사진 첨부 개수. 기존 문자는 0 = 첨부 없음.
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN attachment_count INTEGER NOT NULL DEFAULT 0")
        added.append("sms_inbox.attachment_count")
    if "attachment_paths" not in sms_columns:
        # 서버에 저장한 첨부 파일 목록(JSON: path/filename/content_type).
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN attachment_paths TEXT")
        added.append("sms_inbox.attachment_paths")

    # 대시보드에서 고친 기기 이름·번호·모델·안드로이드 버전을 앱 등록 정보가 덮어쓰지 않도록 표시한다.
    for column, note in (
        ("name_customized", "기기 이름 직접 수정 여부"),
        ("phone_customized", "기기 번호 직접 수정 여부"),
        ("model_customized", "기기 모델명 직접 수정 여부"),
        ("android_customized", "기기 안드로이드 버전 직접 수정 여부"),
    ):
        if column not in device_columns:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            added.append(f"devices.{column} ({note})")

    # 소프트 삭제(삭제 보관함). 예전 DB 에도 손실 없이 컬럼만 추가한다.
    for table in ("schedules", "runs", "tasks", "sms_inbox"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "deleted_at" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT DEFAULT NULL")
            added.append(f"{table}.deleted_at")

    # 로그 분류(검색·필터용). 기존 로그는 메시지로 분류를 채워 넣는다.
    log_columns = {row["name"] for row in conn.execute("PRAGMA table_info(logs)")}
    if "category" not in log_columns:
        conn.execute("ALTER TABLE logs ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        added.append("logs.category")
    for row in conn.execute("SELECT id, message, category FROM logs WHERE category IS NULL OR category = ''").fetchall():
        conn.execute(
            "UPDATE logs SET category = ? WHERE id = ?", (guess_log_category(row["message"]), row["id"])
        )
    # 분류 컬럼이 준비된 뒤에 인덱스를 만든다(예전 DB 는 컬럼이 없을 수 있다).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)")

    # 기기 키를 수신번호로 전환(예전 DB 의 devices.id → devices.phone_number).
    # 바뀐 내용은 _migrate_device_keys 안에서 로그로 남긴다.
    _migrate_device_keys(conn)
    return added


def init_db(path: str | Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        added = _migrate(conn)
        if added:
            log(
                conn,
                f"DB 스키마 마이그레이션: {', '.join(added)} 컬럼 추가",
                "warning",
                category="system",
            )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (key, value),
            )
    finally:
        conn.close()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def device_alias_target(conn: sqlite3.Connection, alias: str) -> str | None:
    """옛 기기 ID(예: `5656`)를 지금의 기기 키로 푼다. 모르는 값이면 None."""
    row = conn.execute("SELECT device_id FROM device_aliases WHERE alias = ?", (alias,)).fetchone()
    return row["device_id"] if row else None


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


def log(conn: sqlite3.Connection, message: str, level: str = "info", category: str | None = None) -> None:
    from .timeutil import now_iso

    kind = category or guess_log_category(message)
    conn.execute(
        "INSERT INTO logs(level, category, message, created_at) VALUES (?, ?, ?, ?)",
        (level, kind, message[:2000], now_iso()),
    )
