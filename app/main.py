"""FastAPI 애플리케이션 (중앙 서버)."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, scheduler
from .db import json_list
from .models import (
    DeviceRegister,
    DeviceUpdate,
    Heartbeat,
    RemoteSettings,
    ScheduleCreate,
    ScheduleUpdate,
    SmsIncoming,
    TaskResult,
    TelegramSettings,
    TelegramTest,
)
from . import telegram_client
from .timeutil import iso, local_str, now, parse_dt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("auto-dialer")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if config.SCHEDULER_ENABLED:
        scheduler.start_scheduler()
    try:
        yield
    finally:
        scheduler.stop_scheduler()


app = FastAPI(
    title="안드로이드 자동 발신 서버",
    description="여러 안드로이드 폰으로 전화/문자를 예약 발신하고, 수신 문자를 텔레그램으로 전달합니다.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    if not config.API_KEY:
        return
    supplied = x_api_key or ""
    if supplied.strip() != config.API_KEY:
        raise HTTPException(status_code=401, detail="API 키가 필요합니다. X-API-Key 헤더를 확인하세요.")


api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


# --------------------------------------------------------------------------
# 공통 도우미
# --------------------------------------------------------------------------
def get_conn():
    """요청마다 새 SQLite 연결을 열고 끝나면 닫는다."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def device_is_online(row: sqlite3.Row, current: datetime | None = None) -> bool:
    current = current or now()
    last_seen = parse_dt(row["last_seen"])
    if last_seen is None:
        return False
    return (current - last_seen).total_seconds() <= config.ONLINE_TIMEOUT_SEC


def _setting_int(settings: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(settings.get(key, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def remote_config(conn: sqlite3.Connection, device_id: str | None = None) -> dict:
    """폰이 하트비트·풀링 때마다 받아가는 동작 설정.

    APK를 다시 설치하지 않아도 대시보드에서 바꾼 값이 수 초 안에 모든 폰에 적용된다.
    """
    settings = db.get_settings(conn)
    cfg = {
        "poll_interval_sec": _setting_int(settings, "poll_interval_sec", 5, 2, 300),
        "call_gap_sec": _setting_int(settings, "call_gap_sec", 3, 0, 600),
        "sms_forward_enabled": settings.get("sms_forward_enabled", "1") == "1",
        "mms_forward_enabled": settings.get("mms_forward_enabled", "1") == "1",
        # 통화 중 동작: 녹음(기본 끔) · 통화 볼륨 0(기본 켬) · 내 소리 차단(기본 켬)
        "call_recording_enabled": settings.get("call_recording_enabled", "0") == "1",
        "call_mute_volume": settings.get("call_mute_volume", "1") == "1",
        "call_mute_mic": settings.get("call_mute_mic", "1") == "1",
        "device_enabled": True,
    }
    if device_id:
        row = conn.execute("SELECT enabled FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is not None:
            cfg["device_enabled"] = bool(row["enabled"])
    return cfg


def device_dict(row: sqlite3.Row, current: datetime | None = None) -> dict:
    current = current or now()
    last_seen = parse_dt(row["last_seen"])
    return {
        "id": row["id"],
        "name": row["name"] or row["id"],
        "model": row["model"] or "",
        "android_version": row["android_version"] or "",
        "app_version": row["app_version"] or "",
        "battery": row["battery"],
        "network": row["network"] or "",
        "note": row["note"] or "",
        "enabled": bool(row["enabled"]),
        "online": device_is_online(row, current),
        "last_seen": row["last_seen"],
        "last_seen_text": local_str(row["last_seen"], "%m-%d %H:%M:%S") if row["last_seen"] else "-",
        "seconds_since_seen": int((current - last_seen).total_seconds()) if last_seen else None,
        "created_at": row["created_at"],
    }


def ensure_device(conn: sqlite3.Connection, device_id: str, name: str | None = None) -> sqlite3.Row:
    device_id = (device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id 가 필요합니다.")
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO devices(id, name, created_at) VALUES (?, ?, ?)",
            (device_id, (name or device_id)[:80], iso(now())),
        )
        db.log(conn, f"기기 자동 등록: {device_id}", "info")
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return row


def task_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "schedule_id": row["schedule_id"],
        "schedule_name": row["schedule_name"] or "",
        "type": row["type"],
        "phone_number": row["phone_number"],
        "seq": row["seq"],
        "repeat_index": row["repeat_index"],
        "max_call_duration_sec": row["max_call_duration_sec"],
        "sms_body": row["sms_body"],
        "device_id": row["device_id"],
        "status": row["status"],
        "attempts": row["attempts"],
        "result": row["result"],
        "error": row["error"],
        "duration_sec": row["duration_sec"],
        "claimed_at": row["claimed_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
    }


def schedule_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    numbers = json_list(row["numbers"])
    latest = conn.execute(
        "SELECT * FROM runs WHERE schedule_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
    ).fetchone()
    opens = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE schedule_id = ? AND status IN ('pending','assigned','running')",
        (row["id"],),
    ).fetchone()["c"]
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "numbers": numbers,
        "number_count": len(numbers),
        "repeat_count": row["repeat_count"],
        "order_mode": row["order_mode"] or "sequence",
        "max_call_duration_sec": row["max_call_duration_sec"],
        "sms_body": row["sms_body"],
        "start_at": row["start_at"],
        "start_at_text": local_str(row["start_at"], "%Y-%m-%d %H:%M"),
        "recurrence": row["recurrence"],
        "device_id": row["device_id"],
        "status": row["status"],
        "next_run_at": row["next_run_at"],
        "next_run_text": local_str(row["next_run_at"], "%Y-%m-%d %H:%M") if row["next_run_at"] else "-",
        "last_run_at": row["last_run_at"],
        "run_count": row["run_count"],
        "tasks_per_run": len(numbers) * max(1, row["repeat_count"] or 1),
        "pending_tasks": opens,
        "latest_run": (
            {
                "id": latest["id"],
                "status": latest["status"],
                "total_tasks": latest["total_tasks"],
                "done_tasks": latest["done_tasks"],
                "failed_tasks": latest["failed_tasks"],
                "scheduled_for": latest["scheduled_for"],
                "created_at": latest["created_at"],
            }
            if latest
            else None
        ),
        "created_at": row["created_at"],
    }


def sms_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "sender": row["sender"],
        "body": row["body"],
        "received_at": row["received_at"],
        "received_at_text": local_str(row["received_at"], "%Y-%m-%d %H:%M:%S") if row["received_at"] else "-",
        "created_at": row["created_at"],
        "telegram_status": row["telegram_status"],
        "telegram_error": row["telegram_error"],
        "telegram_attempts": row["telegram_attempts"],
        "telegram_sent_at": row["telegram_sent_at"],
    }


# --------------------------------------------------------------------------
# 상태/대시보드
# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"detail": "static/index.html 이 없습니다."}, status_code=500)
    return FileResponse(index_file)


@app.get("/api/health", include_in_schema=False)
def public_health():
    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    finally:
        conn.close()
    return {
        "ok": db_ok,
        "server_time": iso(now()),
        "server_time_text": local_str(now()),
        "timezone": config.TZ_NAME,
        "scheduler_running": scheduler.is_running(),
        "scheduler_interval_sec": config.SCHEDULER_INTERVAL_SEC,
        "api_key_required": bool(config.API_KEY),
    }


@api.get("/stats")
def stats(conn: sqlite3.Connection = Depends(get_conn)):
    current = now()
    devices = conn.execute("SELECT * FROM devices").fetchall()
    online = sum(1 for row in devices if device_is_online(row, current))
    today_start = iso(current.replace(hour=0, minute=0, second=0, microsecond=0))

    active_schedules = conn.execute("SELECT COUNT(*) AS c FROM schedules WHERE status = 'active'").fetchone()["c"]
    pending_tasks = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status IN ('pending','assigned','running')"
    ).fetchone()["c"]
    done_today = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status = 'done' AND finished_at >= ?", (today_start,)
    ).fetchone()["c"]
    failed_today = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status = 'failed' AND finished_at >= ?", (today_start,)
    ).fetchone()["c"]
    calls_today = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE type = 'call' AND status IN ('done','failed') AND finished_at >= ?",
        (today_start,),
    ).fetchone()["c"]
    sms_today = conn.execute(
        "SELECT COUNT(*) AS c FROM sms_inbox WHERE created_at >= ?", (today_start,)
    ).fetchone()["c"]
    telegram_failed = conn.execute(
        "SELECT COUNT(*) AS c FROM sms_inbox WHERE telegram_status IN ('failed','pending')"
    ).fetchone()["c"]
    upcoming = conn.execute(
        """SELECT * FROM schedules WHERE status = 'active' AND next_run_at IS NOT NULL
           ORDER BY next_run_at ASC LIMIT 5"""
    ).fetchall()
    recent_runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5").fetchall()

    return {
        "devices": {"total": len(devices), "online": online},
        "schedules": {"active": active_schedules},
        "tasks": {
            "pending": pending_tasks,
            "done_today": done_today,
            "failed_today": failed_today,
            "calls_today": calls_today,
        },
        "sms": {"today": sms_today, "telegram_issue": telegram_failed},
        "upcoming": [
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "next_run_at": row["next_run_at"],
                "next_run_text": local_str(row["next_run_at"], "%m-%d %H:%M"),
            }
            for row in upcoming
        ],
        "recent_runs": [
            {
                "id": row["id"],
                "schedule_name": row["schedule_name"],
                "type": row["type"],
                "status": row["status"],
                "total_tasks": row["total_tasks"],
                "done_tasks": row["done_tasks"],
                "failed_tasks": row["failed_tasks"],
                "created_at_text": local_str(row["created_at"], "%m-%d %H:%M"),
            }
            for row in recent_runs
        ],
        "server_time": iso(current),
        "server_time_text": local_str(current),
    }


# --------------------------------------------------------------------------
# 기기 관리
# --------------------------------------------------------------------------
@api.get("/devices")
def list_devices(conn: sqlite3.Connection = Depends(get_conn)):
    current = now()
    rows = conn.execute("SELECT * FROM devices ORDER BY created_at ASC").fetchall()
    return {"devices": [device_dict(row, current) for row in rows]}


@api.post("/devices")
def register_device(payload: DeviceRegister, conn: sqlite3.Connection = Depends(get_conn)):
    current = iso(now())
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (payload.device_id,)).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO devices(id, name, model, android_version, app_version, note, enabled, last_seen, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                payload.device_id,
                payload.name or payload.device_id,
                payload.model,
                payload.android_version,
                payload.app_version,
                payload.note,
                current,
                current,
            ),
        )
        db.log(conn, f"기기 등록: {payload.device_id} ({payload.name})")
    else:
        conn.execute(
            """UPDATE devices SET name = ?, model = ?, android_version = ?, app_version = ?,
                      note = COALESCE(NULLIF(?, ''), note), last_seen = ? WHERE id = ?""",
            (
                payload.name or row["name"] or payload.device_id,
                payload.model or row["model"],
                payload.android_version or row["android_version"],
                payload.app_version or row["app_version"],
                payload.note,
                current,
                payload.device_id,
            ),
        )
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (payload.device_id,)).fetchone()
    return {"ok": True, "device": device_dict(row)}


@api.post("/devices/{device_id}/heartbeat")
def device_heartbeat(device_id: str, payload: Heartbeat | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    body = payload or Heartbeat(device_id=device_id)
    row = ensure_device(conn, device_id)
    cfg = remote_config(conn, device_id)
    conn.execute(
        "UPDATE devices SET last_seen = ?, battery = COALESCE(?, battery), network = COALESCE(NULLIF(?, ''), network) WHERE id = ?",
        (iso(now()), body.battery, body.network, row["id"]),
    )
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending' AND (device_id IS NULL OR device_id = ?)",
        (device_id,),
    ).fetchone()["c"]
    return {
        "ok": True,
        "server_time": iso(now()),
        "poll_interval_sec": cfg["poll_interval_sec"],
        "pending_tasks": pending,
        # 대시보드에서 바꾼 원격 설정(확인 주기·발신 간격·문자 전달·기기 사용 여부)
        "config": cfg,
    }


@api.patch("/devices/{device_id}")
def update_device(device_id: str, payload: DeviceUpdate, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="기기를 찾을 수 없습니다.")
    updates, values = [], []
    if payload.name is not None:
        updates.append("name = ?")
        values.append(payload.name[:80] or device_id)
    if payload.note is not None:
        updates.append("note = ?")
        values.append(payload.note[:400])
    if payload.enabled is not None:
        updates.append("enabled = ?")
        values.append(1 if payload.enabled else 0)
    if updates:
        values.append(device_id)
        conn.execute(f"UPDATE devices SET {', '.join(updates)} WHERE id = ?", values)
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return {"ok": True, "device": device_dict(row)}


@api.delete("/devices/{device_id}")
def delete_device(device_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="기기를 찾을 수 없습니다.")
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    # 미완료 작업(대기 포함)과 스케줄을 공유 대기열로 되돌린다.
    conn.execute(
        """UPDATE tasks SET device_id = NULL, status = 'pending', claimed_at = NULL
           WHERE device_id = ? AND status IN ('pending','assigned','running')""",
        (device_id,),
    )
    conn.execute(
        "UPDATE schedules SET device_id = NULL WHERE device_id = ?",
        (device_id,),
    )
    db.log(conn, f"기기 삭제: {device_id}", "warning")
    return {"ok": True, "deleted": device_id}


# --------------------------------------------------------------------------
# 스케줄(발신 캠페인)
# --------------------------------------------------------------------------
@api.get("/schedules")
def list_schedules(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()
    return {"schedules": [schedule_dict(conn, row) for row in rows]}


@api.post("/schedules")
def create_schedule(payload: ScheduleCreate, conn: sqlite3.Connection = Depends(get_conn)):
    start_at = parse_dt(payload.start_at)
    assert start_at is not None
    created = iso(now())
    with db.transaction(conn):
        cursor = conn.execute(
            """INSERT INTO schedules(name, type, numbers, repeat_count, order_mode, max_call_duration_sec,
                                     sms_body, start_at, recurrence, device_id, status, next_run_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                payload.name,
                payload.type,
                json.dumps(payload.numbers, ensure_ascii=False),
                payload.repeat_count,
                payload.order_mode,
                payload.max_call_duration_sec,
                payload.sms_body,
                iso(start_at),
                payload.recurrence,
                payload.device_id,
                iso(start_at),
                created,
            ),
        )
        schedule_id = int(cursor.lastrowid)
        db.log(
            conn,
            f"스케줄 등록: '{payload.name}' {payload.type} {len(payload.numbers)}개 번호 × {payload.repeat_count}회 "
            f"({payload.recurrence}, {iso(start_at)})",
        )
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return {"ok": True, "schedule": schedule_dict(conn, row)}


@api.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    return {"schedule": schedule_dict(conn, row)}


@api.patch("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, payload: ScheduleUpdate, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    if payload.name is not None:
        conn.execute("UPDATE schedules SET name = ? WHERE id = ?", (payload.name[:120], schedule_id))
    if payload.status is not None:
        next_run = row["next_run_at"]
        if payload.status == "active" and not next_run:
            start_at = parse_dt(row["start_at"]) or now()
            next_run = iso(start_at if start_at > now() else now())
        conn.execute(
            "UPDATE schedules SET status = ?, next_run_at = ? WHERE id = ?",
            (payload.status, next_run, schedule_id),
        )
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return {"ok": True, "schedule": schedule_dict(conn, row)}


@api.post("/schedules/{schedule_id}/pause")
def pause_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return update_schedule(schedule_id, ScheduleUpdate(status="paused"), conn)


@api.post("/schedules/{schedule_id}/resume")
def resume_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    start_at = parse_dt(row["start_at"]) or now()
    current = now()
    next_run = start_at
    if row["recurrence"] != "once":
        guard = 0
        while next_run <= current and guard < 1000:
            from .timeutil import next_occurrence

            following = next_occurrence(next_run, row["recurrence"])
            if following is None:
                break
            next_run = following
            guard += 1
    elif start_at <= current:
        next_run = current
    conn.execute(
        "UPDATE schedules SET status = 'active', next_run_at = ? WHERE id = ?",
        (iso(next_run), schedule_id),
    )
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return {"ok": True, "schedule": schedule_dict(conn, row)}


@api.post("/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    run_id = scheduler.run_schedule_now(conn, schedule_id)
    if run_id is None:
        raise HTTPException(status_code=400, detail="발신할 번호가 없습니다.")
    return {"ok": True, "run_id": run_id}


@api.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    conn.execute(
        """UPDATE tasks SET status = 'skipped', finished_at = ?, error = '스케줄 삭제로 취소'
           WHERE schedule_id = ? AND status IN ('pending','assigned','running')""",
        (iso(now()), schedule_id),
    )
    conn.execute("UPDATE runs SET status = 'canceled', finished_at = ? WHERE schedule_id = ? AND status IN ('pending','running')", (iso(now()), schedule_id))
    conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    db.log(conn, f"스케줄 삭제: #{schedule_id} {row['name']}", "warning")
    return {"ok": True, "deleted": schedule_id}


# --------------------------------------------------------------------------
# 작업(대기열)
# --------------------------------------------------------------------------
def claim_next_task(conn: sqlite3.Connection, device_id: str) -> dict | None:
    """기기 지정 작업 우선, 그다음 공유 대기열에서 가장 오래된 작업 하나를 원자적으로 가져간다."""
    with db.transaction(conn):
        row = conn.execute(
            """SELECT t.* FROM tasks t
               JOIN runs r ON r.id = t.run_id
               WHERE t.status = 'pending'
                 AND r.status IN ('pending', 'running')
                 AND (t.device_id IS NULL OR t.device_id = ?)
               ORDER BY (CASE WHEN t.device_id IS NULL THEN 1 ELSE 0 END), t.id ASC
               LIMIT 1""",
            (device_id,),
        ).fetchone()
        if row is None:
            return None
        claimed_at = iso(now())
        conn.execute(
            """UPDATE tasks SET status = 'assigned', device_id = ?, claimed_at = ?,
                      attempts = attempts + 1 WHERE id = ? AND status = 'pending'""",
            (device_id, claimed_at, row["id"]),
        )
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ? AND status = 'pending'", (row["run_id"],)
        )
        fresh = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return task_dict(fresh) if fresh else None


@api.get("/tasks/poll")
def poll_task(device_id: str = Query(min_length=1, max_length=64), conn: sqlite3.Connection = Depends(get_conn)):
    ensure_device(conn, device_id)
    cfg = remote_config(conn, device_id)
    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (iso(now()), device_id))
    # 대시보드에서 '사용 중지'된 기기에는 작업을 배정하지 않는다.
    task = claim_next_task(conn, device_id) if cfg["device_enabled"] else None
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending' AND (device_id IS NULL OR device_id = ?)",
        (device_id,),
    ).fetchone()["c"]
    return {
        "task": task,
        "pending_tasks": pending,
        "poll_interval_sec": cfg["poll_interval_sec"],
        "server_time": iso(now()),
        "config": cfg,
    }


@api.post("/tasks/{task_id}/start")
def start_task(task_id: int, payload: dict | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    conn.execute(
        "UPDATE tasks SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
        (iso(now()), task_id),
    )
    return {"ok": True}


@api.post("/tasks/{task_id}/result")
def report_task_result(task_id: int, payload: TaskResult, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    if row["device_id"] and payload.device_id and row["device_id"] != payload.device_id:
        raise HTTPException(status_code=409, detail="다른 기기에 배정된 작업입니다.")
    if row["status"] in ("done", "failed", "skipped"):
        return {"ok": True, "duplicate": True, "task": task_dict(row)}

    finished = iso(now())
    conn.execute(
        """UPDATE tasks SET status = ?, result = ?, error = ?, duration_sec = ?, finished_at = ?
           WHERE id = ?""",
        (
            payload.status,
            (payload.result or "")[:1000] or None,
            (payload.error or "")[:1000] or None,
            payload.duration_sec,
            finished,
            task_id,
        ),
    )
    scheduler.refresh_run(conn, row["run_id"])
    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return {"ok": True, "task": task_dict(updated)}


@api.get("/tasks")
def list_tasks(
    status: Optional[str] = None,
    schedule_id: Optional[int] = None,
    device_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_conn),
):
    clauses, values = [], []
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            values.extend(statuses)
    if schedule_id is not None:
        clauses.append("schedule_id = ?")
        values.append(schedule_id)
    if device_id:
        clauses.append("device_id = ?")
        values.append(device_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(limit)
    rows = conn.execute(
        f"""SELECT * FROM tasks {where} ORDER BY
              CASE status WHEN 'running' THEN 0 WHEN 'assigned' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,
              id DESC LIMIT ?""",
        values,
    ).fetchall()
    return {"tasks": [task_dict(row) for row in rows], "count": len(rows)}


@api.get("/runs")
def list_runs(limit: int = Query(default=30, ge=1, le=200), conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {
        "runs": [
            {
                "id": row["id"],
                "schedule_id": row["schedule_id"],
                "schedule_name": row["schedule_name"],
                "type": row["type"],
                "status": row["status"],
                "scheduled_for": row["scheduled_for"],
                "scheduled_for_text": local_str(row["scheduled_for"], "%Y-%m-%d %H:%M"),
                "total_tasks": row["total_tasks"],
                "done_tasks": row["done_tasks"],
                "failed_tasks": row["failed_tasks"],
                "created_at_text": local_str(row["created_at"], "%m-%d %H:%M:%S"),
                "finished_at": row["finished_at"],
            }
            for row in rows
        ]
    }


# --------------------------------------------------------------------------
# 수신 문자 + 텔레그램
# --------------------------------------------------------------------------
@api.post("/sms/incoming")
def incoming_sms(payload: SmsIncoming, background: BackgroundTasks, conn: sqlite3.Connection = Depends(get_conn)):
    device = ensure_device(conn, payload.device_id)
    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (iso(now()), payload.device_id))

    received_at = None
    if payload.received_at:
        try:
            received_at = iso(parse_dt(payload.received_at))
        except ValueError:
            received_at = None
    if received_at is None and payload.received_ts:
        received_at = iso(datetime.fromtimestamp(payload.received_ts / 1000, tz=config.TZ))
    if received_at is None:
        received_at = iso(now())

    cursor = conn.execute(
        """INSERT INTO sms_inbox(device_id, sender, body, received_at, created_at, telegram_status)
           VALUES (?, ?, ?, ?, ?, 'pending')""",
        (payload.device_id, payload.sender, payload.body, received_at, iso(now())),
    )
    sms_id = int(cursor.lastrowid)
    db.log(conn, f"수신 문자 저장: {payload.sender} → {device['name']} (id={sms_id})")

    settings = db.get_settings(conn)
    if settings.get("sms_forward_enabled", "1") == "1" and settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        background.add_task(_forward_sms, sms_id)
    else:
        conn.execute(
            "UPDATE sms_inbox SET telegram_status = 'skipped', telegram_error = ? WHERE id = ?",
            ("텔레그램 설정 또는 문자 전달이 꺼져 있습니다.", sms_id),
        )

    row = conn.execute("SELECT * FROM sms_inbox WHERE id = ?", (sms_id,)).fetchone()
    return {"ok": True, "id": sms_id, "sms": sms_dict(row)}


def _forward_sms(sms_id: int) -> None:
    conn = db.connect()
    try:
        ok, error = telegram_client.deliver_sms_notification(conn, sms_id)
        if not ok:
            logger.warning("텔레그램 전달 실패 (sms=%s): %s", sms_id, error)
    finally:
        conn.close()


@api.get("/sms")
def list_sms(limit: int = Query(default=50, ge=1, le=500), conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM sms_inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    names = {row["id"]: (row["name"] or row["id"]) for row in conn.execute("SELECT id, name FROM devices")}
    items = []
    for row in rows:
        item = sms_dict(row)
        item["device_name"] = names.get(row["device_id"], row["device_id"])
        items.append(item)
    return {"messages": items, "count": len(items)}


@api.post("/sms/{sms_id}/resend")
def resend_sms(sms_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM sms_inbox WHERE id = ?", (sms_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="수신 문자를 찾을 수 없습니다.")
    conn.execute(
        "UPDATE sms_inbox SET telegram_status = 'pending', telegram_attempts = 0, telegram_error = NULL WHERE id = ?",
        (sms_id,),
    )
    ok, error = telegram_client.deliver_sms_notification(conn, sms_id)
    return {"ok": ok, "error": error}


@api.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_conn)):
    settings = db.get_settings(conn)
    return {
        "telegram": {
            "bot_token": settings.get("telegram_bot_token", ""),
            "chat_id": settings.get("telegram_chat_id", ""),
            "enabled": settings.get("telegram_enabled", "1") == "1",
            "sms_forward_enabled": settings.get("sms_forward_enabled", "1") == "1",
            "configured": bool(settings.get("telegram_bot_token") and settings.get("telegram_chat_id")),
        },
        # 폰에 내려보내는 원격 동작 설정(APK 재설치 없이 변경)
        "remote": remote_config(conn),
        "server": {
            "timezone": config.TZ_NAME,
            "online_timeout_sec": config.ONLINE_TIMEOUT_SEC,
            "scheduler_interval_sec": config.SCHEDULER_INTERVAL_SEC,
            "api_key_required": bool(config.API_KEY),
        },
    }


@api.post("/settings/remote")
def save_remote_settings(payload: RemoteSettings, conn: sqlite3.Connection = Depends(get_conn)):
    """확인 주기·발신 간격·문자 전달·기기 사용 설정을 저장한다(폰은 30초 안에 적용)."""
    if payload.poll_interval_sec is not None:
        db.set_setting(conn, "poll_interval_sec", str(payload.poll_interval_sec))
    if payload.call_gap_sec is not None:
        db.set_setting(conn, "call_gap_sec", str(payload.call_gap_sec))
    if payload.sms_forward_enabled is not None:
        db.set_setting(conn, "sms_forward_enabled", "1" if payload.sms_forward_enabled else "0")
    if payload.mms_forward_enabled is not None:
        db.set_setting(conn, "mms_forward_enabled", "1" if payload.mms_forward_enabled else "0")
    if payload.call_recording_enabled is not None:
        db.set_setting(conn, "call_recording_enabled", "1" if payload.call_recording_enabled else "0")
    if payload.call_mute_volume is not None:
        db.set_setting(conn, "call_mute_volume", "1" if payload.call_mute_volume else "0")
    if payload.call_mute_mic is not None:
        db.set_setting(conn, "call_mute_mic", "1" if payload.call_mute_mic else "0")
    cfg = remote_config(conn)
    db.log(
        conn,
        "원격 기기 설정 저장: 확인 {}초 · 간격 {}초 · SMS 전달 {} · MMS 전달 {} · 통화 녹음 {} · 통화 볼륨 0 {} · 내 소리 차단 {}".format(
            cfg["poll_interval_sec"],
            cfg["call_gap_sec"],
            "ON" if cfg["sms_forward_enabled"] else "OFF",
            "ON" if cfg["mms_forward_enabled"] else "OFF",
            "ON" if cfg["call_recording_enabled"] else "OFF",
            "ON" if cfg["call_mute_volume"] else "OFF",
            "ON" if cfg["call_mute_mic"] else "OFF",
        ),
    )
    return {"ok": True, "remote": cfg}


@api.post("/settings")
def save_settings(payload: RemoteSettings, conn: sqlite3.Connection = Depends(get_conn)):
    """POST /api/settings 도 /api/settings/remote 와 동일하게 동작한다."""
    return save_remote_settings(payload, conn)


@api.post("/settings/telegram")
def save_telegram_settings(payload: TelegramSettings, conn: sqlite3.Connection = Depends(get_conn)):
    if payload.bot_token is not None:
        token = payload.bot_token.strip()
        if token and ":" not in token:
            raise HTTPException(status_code=400, detail="봇 토큰 형식이 올바르지 않습니다. (예: 123456789:AA... )")
        db.set_setting(conn, "telegram_bot_token", token)
    if payload.chat_id is not None:
        db.set_setting(conn, "telegram_chat_id", payload.chat_id.strip())
    if payload.enabled is not None:
        db.set_setting(conn, "telegram_enabled", "1" if payload.enabled else "0")
    if payload.sms_forward_enabled is not None:
        db.set_setting(conn, "sms_forward_enabled", "1" if payload.sms_forward_enabled else "0")
    db.log(conn, "텔레그램 설정 저장")
    return get_settings(conn)


@api.post("/telegram/test")
def telegram_test(payload: TelegramTest | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    settings = db.get_settings(conn)
    text = (payload.text if payload else None) or "✅ 텔레그램 연동 테스트 메시지입니다."
    ok, error = telegram_client.send_message(
        settings.get("telegram_bot_token", ""), settings.get("telegram_chat_id", ""), text
    )
    db.log(conn, f"텔레그램 테스트 발송 {'성공' if ok else '실패'}: {error or ''}", "info" if ok else "warning")
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return {"ok": True}


@api.get("/logs")
def list_logs(limit: int = Query(default=100, ge=1, le=500), conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {
        "logs": [
            {
                "id": row["id"],
                "level": row["level"],
                "message": row["message"],
                "created_at": row["created_at"],
                "created_at_text": local_str(row["created_at"], "%m-%d %H:%M:%S"),
            }
            for row in rows
        ]
    }


@api.post("/scheduler/tick")
def manual_tick(conn: sqlite3.Connection = Depends(get_conn)):
    """스케줄러를 즉시 한 번 돌린다(운영/점검용)."""
    return {"ok": True, **scheduler.tick(conn)}


app.include_router(api)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
