"""FastAPI 애플리케이션 (중앙 서버)."""
from __future__ import annotations

import base64
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
    RunBulkDelete,
    ScheduleBulkDelete,
    ScheduleCopy,
    ScheduleCreate,
    ScheduleUpdate,
    SmsBulkDelete,
    SmsIncoming,
    TaskBulkDelete,
    TaskResult,
    TelegramSettings,
    TelegramTest,
)
from . import telegram_client
from .timeutil import iso, local_str, next_occurrence, now, parse_dt

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
        "phone_number": (_row_value(row, "phone_number") or ""),
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


def device_lookup(conn: sqlite3.Connection) -> dict[str, dict]:
    """기기 ID → 대시보드용 기기 정보(이름·번호·온라인)."""
    rows = conn.execute("SELECT * FROM devices").fetchall()
    return {row["id"]: device_dict(row) for row in rows}


def resolve_target_devices(
    conn: sqlite3.Connection, device_ids: list[str], devices: dict[str, dict] | None = None
) -> list[dict]:
    """스케줄의 발신 기기 ID 목록을 이름·번호·온라인 상태로 풀어 준다.

    기기가 이미 삭제됐으면 이름을 ID 로, 번호는 빈 값, missing=true 로 돌려준다.
    """
    if not device_ids:
        return []
    lookup = devices if devices is not None else device_lookup(conn)
    result: list[dict] = []
    for device_id in device_ids:
        info = lookup.get(device_id)
        if info is None:
            result.append(
                {
                    "device_id": device_id,
                    "name": device_id,
                    "phone_number": "",
                    "online": False,
                    "enabled": False,
                    "missing": True,
                }
            )
        else:
            result.append(
                {
                    "device_id": device_id,
                    "name": info.get("name") or device_id,
                    "phone_number": info.get("phone_number") or "",
                    "online": bool(info.get("online")),
                    "enabled": bool(info.get("enabled", True)),
                    "missing": False,
                }
            )
    return result


def schedule_dict(
    conn: sqlite3.Connection, row: sqlite3.Row, devices: dict[str, dict] | None = None
) -> dict:
    numbers = json_list(row["numbers"])
    total_calls = row["total_calls"] if "total_calls" in row.keys() else None
    per_run = total_calls if total_calls is not None else len(numbers) * max(1, row["repeat_count"] or 1)
    # 발신 기기 여러 대(예전 DB·예전 앱이 남긴 device_id 도 함께 살린다).
    target_ids = db.json_list(_row_value(row, "target_device_ids"))
    if not target_ids and row["device_id"]:
        target_ids = [row["device_id"]]
    target_devices = resolve_target_devices(conn, target_ids, devices)
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
        "total_calls": total_calls,
        "order_mode": row["order_mode"] or "sequence",
        "max_call_duration_sec": row["max_call_duration_sec"],
        "sms_body": row["sms_body"],
        "start_at": row["start_at"],
        "start_at_text": local_str(row["start_at"], "%Y-%m-%d %H:%M"),
        "recurrence": row["recurrence"],
        "device_id": row["device_id"],
        "target_device_ids": target_ids,
        "target_devices": target_devices,
        "target_device_count": len(target_ids),
        "status": row["status"],
        "next_run_at": row["next_run_at"],
        "next_run_text": local_str(row["next_run_at"], "%Y-%m-%d %H:%M") if row["next_run_at"] else "-",
        "last_run_at": row["last_run_at"],
        "run_count": row["run_count"],
        "tasks_per_run": per_run * max(1, len(target_ids)),
        "tasks_per_device": per_run,
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


def _row_value(row: sqlite3.Row, key: str, default=None):
    """예전 DB(칼럼이 없을 수 있음)에서도 안전하게 값을 꺼낸다."""
    try:
        return row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        return default


def sms_attachment_list(row: sqlite3.Row) -> list[dict]:
    """저장된 MMS 첨부 목록을 대시보드가 바로 쓸 수 있는 형태로 돌려준다."""
    try:
        items = json.loads(_row_value(row, "attachment_paths") or "[]")
    except (TypeError, ValueError):
        items = []
    if not isinstance(items, list):
        return []
    result: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        content_type = str(item.get("content_type") or "application/octet-stream")
        base_type = content_type.split(";")[0].strip().lower()
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        result.append(
            {
                "index": index,
                "filename": str(item.get("filename") or f"attachment-{index}"),
                "content_type": content_type,
                "size": size,
                "is_image": base_type.startswith("image/"),
                "url": f"/api/sms/{row['id']}/attachments/{index}",
            }
        )
    return result


def sms_dict(row: sqlite3.Row, device_phones: dict[str, str] | None = None) -> dict:
    attachments = sms_attachment_list(row)
    # 수신 번호: 문자에 기록된 값 → 비었으면 그 기기의 번호(devices.phone_number) 순서로 쓴다.
    receiver_number = (_row_value(row, "receiver_number") or "").strip()
    if not receiver_number and device_phones:
        receiver_number = (device_phones.get(row["device_id"]) or "").strip()
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "sender": row["sender"],
        "receiver_number": receiver_number,
        "body": row["body"],
        "received_at": row["received_at"],
        "received_at_text": local_str(row["received_at"], "%Y-%m-%d %H:%M:%S") if row["received_at"] else "-",
        "created_at": row["created_at"],
        "telegram_status": row["telegram_status"],
        "telegram_error": row["telegram_error"],
        "telegram_attempts": row["telegram_attempts"],
        "telegram_sent_at": row["telegram_sent_at"],
        "attachment_count": int(_row_value(row, "attachment_count", 0) or 0),
        "attachments": attachments,
        "has_mms": bool(attachments),
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
            """INSERT INTO devices(id, name, model, android_version, app_version, phone_number, note, enabled, last_seen, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                payload.device_id,
                payload.name or payload.device_id,
                payload.model,
                payload.android_version,
                payload.app_version,
                payload.phone_number,
                payload.note,
                current,
                current,
            ),
        )
        db.log(conn, f"기기 등록: {payload.device_id} ({payload.name})")
    else:
        conn.execute(
            """UPDATE devices SET name = ?, model = ?, android_version = ?, app_version = ?,
                      phone_number = COALESCE(NULLIF(?, ''), phone_number),
                      note = COALESCE(NULLIF(?, ''), note), last_seen = ? WHERE id = ?""",
            (
                payload.name or row["name"] or payload.device_id,
                payload.model or row["model"],
                payload.android_version or row["android_version"],
                payload.app_version or row["app_version"],
                payload.phone_number,
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
    if payload.phone_number is not None:
        updates.append("phone_number = ?")
        values.append(payload.phone_number[:30])
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
    # 여러 대 지정 스케줄의 기기 목록에서도 지운 기기를 빼낸다(스케줄은 남기고 나머지 기기로 계속 발신).
    for sched in conn.execute("SELECT id, device_id, target_device_ids FROM schedules").fetchall():
        targets = db.json_list(_row_value(sched, "target_device_ids"))
        if not targets or device_id not in targets:
            continue
        remaining = [item for item in targets if item != device_id]
        conn.execute(
            "UPDATE schedules SET target_device_ids = ?, device_id = ? WHERE id = ?",
            (json.dumps(remaining, ensure_ascii=False), remaining[0] if len(remaining) == 1 else None, sched["id"]),
        )
    db.log(conn, f"기기 삭제: {device_id}", "warning")
    return {"ok": True, "deleted": device_id}


# --------------------------------------------------------------------------
# 스케줄(발신 캠페인)
# --------------------------------------------------------------------------
@api.get("/schedules")
def list_schedules(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()
    devices = device_lookup(conn)
    return {"schedules": [schedule_dict(conn, row, devices) for row in rows]}


@api.post("/schedules")
def create_schedule(payload: ScheduleCreate, conn: sqlite3.Connection = Depends(get_conn)):
    start_at = parse_dt(payload.start_at)
    assert start_at is not None
    created = iso(now())
    with db.transaction(conn):
        cursor = conn.execute(
            """INSERT INTO schedules(name, type, numbers, repeat_count, total_calls, order_mode, max_call_duration_sec,
                                     sms_body, start_at, recurrence, device_id, target_device_ids, status, next_run_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                payload.name,
                payload.type,
                json.dumps(payload.numbers, ensure_ascii=False),
                payload.repeat_count,
                payload.total_calls,
                payload.order_mode,
                payload.max_call_duration_sec,
                payload.sms_body,
                iso(start_at),
                payload.recurrence,
                payload.device_id,
                json.dumps(payload.target_device_ids or [], ensure_ascii=False),
                iso(start_at),
                created,
            ),
        )
        schedule_id = int(cursor.lastrowid)
        amount = (
            f"총 {payload.total_calls}건"
            if payload.total_calls is not None
            else f"{len(payload.numbers)}개 번호 × {payload.repeat_count}회"
        )
        targets = payload.target_device_ids or []
        device_text = f"기기 {', '.join(targets)}" if targets else "전체 기기 자동 배분"
        db.log(
            conn,
            f"스케줄 등록: '{payload.name}' {payload.type} {amount} "
            f"({device_text}, {payload.order_mode}, {payload.recurrence}, {iso(start_at)})",
        )
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return {"ok": True, "schedule": schedule_dict(conn, row)}


@api.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    return {"schedule": schedule_dict(conn, row)}


def _next_run_after_edit(
    start_at: datetime,
    recurrence: str,
    current: datetime,
    allow_now: bool = False,
    run_count: int = 0,
) -> str | None:
    """수정된 시작 시각/주기로 다음 실행 시각을 계산한다."""
    if start_at > current:
        return iso(start_at)
    if recurrence != "once":
        candidate: datetime | None = start_at
        guard = 0
        while candidate is not None and candidate <= current and guard < 1000:
            candidate = next_occurrence(candidate, recurrence)
            guard += 1
        return iso(candidate) if candidate is not None else None
    # 1회성인데 시작 시각이 이미 지난 경우
    if allow_now:
        return iso(current)
    if run_count <= 0:
        return iso(start_at)
    return None


@api.patch("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, payload: ScheduleUpdate, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")

    provided = set(payload.model_fields_set)
    if not provided:
        raise HTTPException(status_code=400, detail="수정할 항목을 하나 이상 보내야 합니다.")

    new = {
        "name": row["name"],
        "type": row["type"],
        "numbers": json_list(row["numbers"]),
        "repeat_count": row["repeat_count"],
        "total_calls": row["total_calls"] if "total_calls" in row.keys() else None,
        "order_mode": row["order_mode"] or "sequence",
        "max_call_duration_sec": row["max_call_duration_sec"],
        "sms_body": row["sms_body"],
        "start_at": row["start_at"],
        "recurrence": row["recurrence"],
        "device_id": row["device_id"],
        "target_device_ids": db.json_list(_row_value(row, "target_device_ids"))
        or ([row["device_id"]] if row["device_id"] else []),
        "status": row["status"],
    }
    before = dict(new)

    if "name" in provided and payload.name is not None:
        new["name"] = payload.name.strip()
    if "type" in provided and payload.type is not None:
        new["type"] = payload.type
    if "numbers" in provided:
        new["numbers"] = list(payload.numbers or [])
    if "repeat_count" in provided and payload.repeat_count is not None:
        new["repeat_count"] = payload.repeat_count
    if "total_calls" in provided:
        new["total_calls"] = payload.total_calls
    if "order_mode" in provided and payload.order_mode is not None:
        new["order_mode"] = payload.order_mode
    if "max_call_duration_sec" in provided:
        new["max_call_duration_sec"] = payload.max_call_duration_sec
    if "sms_body" in provided:
        new["sms_body"] = payload.sms_body
    if "start_at" in provided and payload.start_at is not None:
        new["start_at"] = payload.start_at
    if "recurrence" in provided and payload.recurrence is not None:
        new["recurrence"] = payload.recurrence
    if "device_id" in provided:
        new["device_id"] = payload.device_id
    if {"target_device_ids", "target_device_id", "device_id"} & provided:
        # 여러 대 지정(PATCH) 이 오면 목록을 통째로 바꿔 덮어쓴다.
        new["target_device_ids"] = list(payload.target_device_ids or [])
        new["device_id"] = new["target_device_ids"][0] if len(new["target_device_ids"]) == 1 else None
    if "status" in provided and payload.status is not None:
        new["status"] = payload.status

    # 발신 유형에 맞춰 필수 항목 점검
    if new["type"] == "call":
        if not new["max_call_duration_sec"]:
            new["max_call_duration_sec"] = config.DEFAULT_CALL_MAX_SEC
        new["sms_body"] = None
    else:
        if not (new["sms_body"] or "").strip():
            raise HTTPException(status_code=400, detail="문자 발신에는 문자 내용(sms_body)이 필요합니다.")
        new["sms_body"] = new["sms_body"].strip()
        new["max_call_duration_sec"] = None

    if not new["numbers"]:
        raise HTTPException(status_code=400, detail="발신할 전화번호를 1개 이상 입력하세요.")

    per_device = new["total_calls"] if new["total_calls"] is not None else len(new["numbers"]) * max(
        1, new["repeat_count"] or 1
    )
    if len(new["target_device_ids"]) > 1 and per_device * len(new["target_device_ids"]) > config.MAX_TOTAL_CALLS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"기기 {len(new['target_device_ids'])}대 × {per_device}회 = "
                f"{per_device * len(new['target_device_ids'])}건은 한 번에 발신할 수 있는 "
                f"최대 {config.MAX_TOTAL_CALLS}건을 넘습니다. 기기 수나 총 발신 횟수를 줄이세요."
            ),
        )

    start_dt = parse_dt(new["start_at"]) or now()
    if not new["name"]:
        label = "전화" if new["type"] == "call" else "문자"
        new["name"] = f"{label} 발신 {start_dt.strftime('%m-%d %H:%M')}"

    # 다음 실행 시각 · 상태
    current = now()
    schedule_changed = bool({"start_at", "recurrence"} & provided)
    explicitly_status = "status" in provided and payload.status is not None
    if schedule_changed and not explicitly_status and new["status"] == "finished" and start_dt > current:
        # 지난 1회성 일정의 실행 시각을 미래로 옮기면 다시 예약한다.
        new["status"] = "active"

    if new["status"] == "finished":
        next_run = None
    elif new["status"] == "paused":
        # 일시정지는 예약을 잃지 않도록 기존 값을 두고, 시각이 바뀌었으면 다시 계산해 둔다.
        next_run = row["next_run_at"]
        if schedule_changed:
            next_run = _next_run_after_edit(start_dt, new["recurrence"], current, run_count=row["run_count"] or 0)
    elif schedule_changed or explicitly_status:
        next_run = _next_run_after_edit(
            start_dt,
            new["recurrence"],
            current,
            allow_now=explicitly_status,
            run_count=row["run_count"] or 0,
        )
    else:
        next_run = row["next_run_at"]

    with db.transaction(conn):
        conn.execute(
            """UPDATE schedules SET name = ?, type = ?, numbers = ?, repeat_count = ?, total_calls = ?,
                                  order_mode = ?, max_call_duration_sec = ?, sms_body = ?, start_at = ?,
                                  recurrence = ?, device_id = ?, target_device_ids = ?, status = ?, next_run_at = ?
               WHERE id = ?""",
            (
                new["name"][:120],
                new["type"],
                json.dumps(new["numbers"], ensure_ascii=False),
                new["repeat_count"],
                new["total_calls"],
                new["order_mode"],
                new["max_call_duration_sec"],
                new["sms_body"],
                iso(start_dt),
                new["recurrence"],
                new["device_id"],
                json.dumps(new["target_device_ids"], ensure_ascii=False),
                new["status"],
                next_run,
                schedule_id,
            ),
        )
        changes = []
        if before["numbers"] != new["numbers"]:
            changes.append(f"번호 {len(before['numbers'])}개→{len(new['numbers'])}개")
        if before["total_calls"] != new["total_calls"]:
            changes.append(f"총 발신 {before['total_calls'] or '-'}→{new['total_calls'] or '-'}회")
        if before["order_mode"] != new["order_mode"]:
            changes.append(f"순서 {before['order_mode']}→{new['order_mode']}")
        if before["type"] != new["type"]:
            changes.append(f"유형 {before['type']}→{new['type']}")
        if before["start_at"] != iso(start_dt):
            changes.append(f"실행 시각 {local_str(before['start_at'], '%m-%d %H:%M')}→{local_str(iso(start_dt), '%m-%d %H:%M')}")
        if before["recurrence"] != new["recurrence"]:
            changes.append(f"주기 {before['recurrence']}→{new['recurrence']}")
        if before["device_id"] != new["device_id"] or before["target_device_ids"] != new["target_device_ids"]:
            def _dev_label(ids):
                return ", ".join(ids) if ids else "전체 자동 배분"

            changes.append(f"기기 {_dev_label(before['target_device_ids'])}→{_dev_label(new['target_device_ids'])}")
        if before["status"] != new["status"]:
            changes.append(f"상태 {before['status']}→{new['status']}")
        if before["name"] != new["name"]:
            changes.append("이름 변경")
        detail = f" ({', '.join(changes)})" if changes else ""
        db.log(conn, f"스케줄 수정: #{schedule_id} '{new['name']}'{detail}")

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


def _purge_schedules(conn: sqlite3.Connection, schedule_ids: list[int]) -> dict:
    """스케줄과 그 실행 기록(runs)·발신 작업(tasks)을 함께 지운다.

    이미 실행된 스케줄을 지워도 실행 묶음·발신 작업이 고아(orphan)로 남지 않는다.
    FK 제약이 걸린 DB에서도 자식 행을 먼저 지우므로 오류가 나지 않는다.
    """
    ids = [int(item) for item in dict.fromkeys(schedule_ids or [])]
    if not ids:
        return {"deleted_ids": [], "deleted": 0, "deleted_runs": 0, "deleted_tasks": 0, "missing_ids": []}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, name FROM schedules WHERE id IN ({placeholders})", ids
    ).fetchall()
    found = [row["id"] for row in rows]
    found_set = set(found)
    missing = [item for item in ids if item not in found_set]
    if not found:
        return {"deleted_ids": [], "deleted": 0, "deleted_runs": 0, "deleted_tasks": 0, "missing_ids": missing}

    found_placeholders = ",".join("?" for _ in found)
    run_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM runs WHERE schedule_id IN ({found_placeholders})", found
        )
    ]

    if run_ids:
        run_placeholders = ",".join("?" for _ in run_ids)
        deleted_tasks = conn.execute(
            f"SELECT COUNT(*) AS c FROM tasks WHERE schedule_id IN ({found_placeholders}) OR run_id IN ({run_placeholders})",
            found + run_ids,
        ).fetchone()["c"]
        conn.execute(
            f"DELETE FROM tasks WHERE schedule_id IN ({found_placeholders}) OR run_id IN ({run_placeholders})",
            found + run_ids,
        )
        conn.execute(f"DELETE FROM runs WHERE id IN ({run_placeholders})", run_ids)
    else:
        deleted_tasks = conn.execute(
            f"SELECT COUNT(*) AS c FROM tasks WHERE schedule_id IN ({found_placeholders})", found
        ).fetchone()["c"]
        conn.execute(f"DELETE FROM tasks WHERE schedule_id IN ({found_placeholders})", found)

    deleted_schedules = conn.execute(
        f"DELETE FROM schedules WHERE id IN ({found_placeholders})", found
    ).rowcount
    return {
        "deleted_ids": found,
        "deleted": int(deleted_schedules or 0),
        "deleted_runs": len(run_ids),
        "deleted_tasks": int(deleted_tasks or 0),
        "missing_ids": missing,
    }


@api.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """스케줄 1개 삭제. 이미 실행된 스케줄이어도 실행 기록·발신 작업까지 함께 지운다."""
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    with db.transaction(conn):
        result = _purge_schedules(conn, [schedule_id])
        db.log(
            conn,
            f"스케줄 삭제: #{schedule_id} '{row['name']}' "
            f"(실행 기록 {result['deleted_runs']}개 · 발신 작업 {result['deleted_tasks']}건 함께 삭제)",
            "warning",
        )
    return {"ok": True, "deleted": schedule_id, **result}


@api.post("/schedules/delete")
def bulk_delete_schedules(payload: ScheduleBulkDelete, conn: sqlite3.Connection = Depends(get_conn)):
    """스케줄 여러 개 삭제.

    - ids           : 선택한 스케줄
    - executed_only : 이미 실행된 적이 있고 다시 예약되지 않는 스케줄
                      (실행 기록이 있으면서 status='finished' 이거나 next_run_at 이 없는 것)
    - all_schedules : 등록된 모든 스케줄
    실행 기록(runs)과 발신 작업(tasks)도 함께 지워 고아 기록이 남지 않는다.
    """
    if payload.all_schedules:
        ids = [row["id"] for row in conn.execute("SELECT id FROM schedules ORDER BY id")]
        label = "전체"
    elif payload.executed_only:
        # 되풀이되어 다시 실행될 스케줄(매일·매주 등 활성/일시정지)은 건드리지 않는다.
        ids = [
            row["id"]
            for row in conn.execute(
                """SELECT s.id FROM schedules s
                   WHERE (s.run_count > 0 OR s.last_run_at IS NOT NULL
                          OR EXISTS (SELECT 1 FROM runs r WHERE r.schedule_id = s.id))
                     AND (s.status = 'finished' OR s.next_run_at IS NULL)
                   ORDER BY s.id"""
            )
        ]
        label = "실행했던"
    else:
        ids = list(payload.ids)
        label = "선택"

    if not ids:
        raise HTTPException(status_code=404, detail="삭제할 스케줄이 없습니다.")

    with db.transaction(conn):
        result = _purge_schedules(conn, ids)
        db.log(
            conn,
            f"스케줄 {label} 삭제: {result['deleted']}개 "
            f"(실행 기록 {result['deleted_runs']}개 · 발신 작업 {result['deleted_tasks']}건)",
            "warning",
        )
    return {"ok": True, **result}


@api.post("/schedules/{schedule_id}/copy")
def copy_schedule(
    schedule_id: int,
    payload: ScheduleCopy | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """스케줄 복사. 이름·번호·발신량·순서·시각·주기·기기를 그대로 복사한다.

    기본은 일시정지(paused) 상태로 만들어, 이미 지난 시각의 스케줄을 복사해도
    확인하기 전에 저절로 발신되지 않게 한다. body 에 enabled=true 를 보내면 활성으로 만든다.
    """
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")

    options = payload or ScheduleCopy()
    base_name = (row["name"] or f"스케줄 {schedule_id}").strip()
    name = (options.name or f"{base_name} (복사)")[:120]

    start_at = parse_dt(row["start_at"]) or now()
    current = now()
    status = "active" if options.enabled else "paused"
    if options.enabled:
        next_run = _next_run_after_edit(
            start_at, row["recurrence"] or "once", current, allow_now=True, run_count=0
        )
    else:
        # 일시정지 복사본: 미래 시각일 때만 예약 시각을 보여 주고, 지난 시각이면 비워 둔다.
        next_run = iso(start_at) if start_at > current else None

    total_calls = row["total_calls"] if "total_calls" in row.keys() else None
    created = iso(current)
    with db.transaction(conn):
        cursor = conn.execute(
            """INSERT INTO schedules(name, type, numbers, repeat_count, total_calls, order_mode,
                                     max_call_duration_sec, sms_body, start_at, recurrence, device_id, target_device_ids,
                                     status, next_run_at, run_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                name,
                row["type"],
                row["numbers"],
                row["repeat_count"],
                total_calls,
                row["order_mode"] or "sequence",
                row["max_call_duration_sec"],
                row["sms_body"],
                row["start_at"],
                row["recurrence"],
                row["device_id"],
                json.dumps(
                    db.json_list(_row_value(row, "target_device_ids"))
                    or ([row["device_id"]] if row["device_id"] else []),
                    ensure_ascii=False,
                ),
                status,
                next_run,
                created,
            ),
        )
        new_id = int(cursor.lastrowid)
        db.log(
            conn,
            f"스케줄 복사: #{schedule_id} '{row['name']}' → #{new_id} '{name}' "
            f"({'활성' if options.enabled else '일시정지'})",
        )
    new_row = conn.execute("SELECT * FROM schedules WHERE id = ?", (new_id,)).fetchone()
    return {"ok": True, "source_id": schedule_id, "schedule": schedule_dict(conn, new_row)}


# 실행 묶음(run) 삭제: 스케줄을 남긴 채 실행 기록만 정리하고 싶을 때
FINISHED_RUN_STATUSES = ("done", "partial", "failed", "canceled")


def _purge_runs(conn: sqlite3.Connection, run_ids: list[int]) -> tuple[int, int]:
    """실행 묶음과 그 안의 발신 작업을 지운다. (지운 작업 수, 지운 묶음 수)"""
    ids = [int(item) for item in dict.fromkeys(run_ids or [])]
    if not ids:
        return 0, 0
    placeholders = ",".join("?" for _ in ids)
    deleted_tasks = conn.execute(
        f"SELECT COUNT(*) AS c FROM tasks WHERE run_id IN ({placeholders})", ids
    ).fetchone()["c"]
    conn.execute(f"DELETE FROM tasks WHERE run_id IN ({placeholders})", ids)
    deleted_runs = conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", ids).rowcount
    return int(deleted_tasks or 0), int(deleted_runs or 0)


@api.delete("/runs/{run_id}")
def delete_run(run_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """실행 묶음 1개 삭제(그 안의 발신 작업 포함)."""
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다.")
    with db.transaction(conn):
        deleted_tasks, deleted_runs = _purge_runs(conn, [run_id])
        db.log(
            conn,
            f"실행 기록 삭제: run #{run_id} '{row['schedule_name']}' (발신 작업 {deleted_tasks}건)",
            "warning",
        )
    return {"ok": True, "deleted": deleted_runs, "deleted_tasks": deleted_tasks}


@api.post("/runs/delete")
def bulk_delete_runs(payload: RunBulkDelete, conn: sqlite3.Connection = Depends(get_conn)):
    """실행 기록 여러 개 삭제 / 이미 끝난 실행 기록 전체 삭제(all_finished)."""
    if payload.all_finished:
        placeholders = ",".join("?" for _ in FINISHED_RUN_STATUSES)
        ids = [
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM runs WHERE status IN ({placeholders}) ORDER BY id",
                list(FINISHED_RUN_STATUSES),
            )
        ]
    else:
        ids = list(payload.ids)
    if not ids:
        raise HTTPException(status_code=404, detail="삭제할 실행 기록이 없습니다.")
    placeholders = ",".join("?" for _ in ids)
    found = {
        row["id"]
        for row in conn.execute(f"SELECT id FROM runs WHERE id IN ({placeholders})", ids)
    }
    missing = [item for item in ids if item not in found]
    target = [item for item in ids if item in found]
    if not target:
        raise HTTPException(status_code=404, detail="삭제할 실행 기록이 없습니다.")
    with db.transaction(conn):
        deleted_tasks, deleted_runs = _purge_runs(conn, target)
        db.log(
            conn,
            f"실행 기록 삭제: {deleted_runs}개 (발신 작업 {deleted_tasks}건)",
            "warning",
        )
    return {
        "ok": True,
        "deleted": deleted_runs,
        "deleted_tasks": deleted_tasks,
        "missing_ids": missing,
    }


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


# 대기열 삭제에서 실제로 없애는 상태 / 취소로 처리하는 상태
DELETABLE_STATUSES = ("pending", "assigned", "done", "failed", "skipped")
ACTIVE_STATUSES = ("pending", "assigned", "running")


def _recount_runs_after_delete(conn: sqlite3.Connection, run_ids: set[int]) -> list[dict]:
    """작업이 사라진 실행 묶음(run)의 진행 집계와 상태를 다시 맞춘다.

    남은 작업이 하나도 없거나 완료·실패 없이 취소만 남으면 'canceled' 로 표시해
    "다 지운 묶음"이 완료(done)처럼 보이지 않게 한다.
    """
    updated: list[dict] = []
    for run_id in sorted(run_ids):
        counts = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                      SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                      SUM(CASE WHEN status IN ('pending','assigned','running') THEN 1 ELSE 0 END) AS open
               FROM tasks WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
        total = counts["total"] or 0
        done = counts["done"] or 0
        failed = counts["failed"] or 0
        skipped = counts["skipped"] or 0
        open_count = counts["open"] or 0

        if total == 0 or (open_count == 0 and done == 0 and failed == 0 and skipped > 0):
            status = "canceled"
            conn.execute(
                """UPDATE runs SET status = 'canceled', total_tasks = ?, done_tasks = ?, failed_tasks = ?,
                          finished_at = COALESCE(finished_at, ?) WHERE id = ?""",
                (total - skipped, done, failed, iso(now()), run_id),
            )
        elif open_count == 0:
            status = "done" if failed == 0 else ("failed" if done == 0 else "partial")
            conn.execute(
                """UPDATE runs SET status = ?, total_tasks = ?, done_tasks = ?, failed_tasks = ?,
                          finished_at = COALESCE(finished_at, ?) WHERE id = ?""",
                (status, total - skipped, done, failed, iso(now()), run_id),
            )
        else:
            status = "running" if (done + failed) > 0 else "pending"
            conn.execute(
                """UPDATE runs SET status = ?, total_tasks = ?, done_tasks = ?, failed_tasks = ?
                   WHERE id = ?""",
                (status, total, done, failed, run_id),
            )

        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is not None:
            updated.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "total_tasks": row["total_tasks"],
                    "done_tasks": row["done_tasks"],
                    "failed_tasks": row["failed_tasks"],
                }
            )
    return updated


def _delete_queue_tasks(
    conn: sqlite3.Connection,
    task_ids: list[int] | None = None,
    all_pending: bool = False,
    all_tasks: bool = False,
    run_id: int | None = None,
) -> dict:
    """대기열에서 작업을 지운다.

    - pending/assigned/done/failed/skipped 는 tasks 행을 실제로 삭제해 목록에서도 지운다.
    - running(발신 중) 은 폰이 이미 걸고 있어 되돌릴 수 없으므로 취소(skipped)로 표시한다.
    """
    if all_tasks:
        clauses: list[str] = []
        values: list = []
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(f"SELECT id, run_id, status FROM tasks{where}", values).fetchall()
        requested: list[int] = []
    elif all_pending:
        clauses = ["status IN ('pending','assigned','running')"]
        values: list = []
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        rows = conn.execute(
            f"SELECT id, run_id, status FROM tasks WHERE {' AND '.join(clauses)}", values
        ).fetchall()
        requested: list[int] = []
    else:
        requested = [int(item) for item in dict.fromkeys(task_ids or [])]
        if not requested:
            raise HTTPException(status_code=400, detail="삭제할 작업을 선택하세요.")
        rows = conn.execute(
            f"SELECT id, run_id, status FROM tasks WHERE id IN ({','.join('?' for _ in requested)})",
            requested,
        ).fetchall()

    found_ids = {row["id"] for row in rows}
    removed = [row["id"] for row in rows if row["status"] in DELETABLE_STATUSES]
    canceled = [row["id"] for row in rows if row["status"] == "running"]
    kept: list[int] = []
    missing = [task_id for task_id in requested if task_id not in found_ids]

    if not removed and not canceled:
        if requested and not found_ids:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        raise HTTPException(
            status_code=409,
            detail="삭제할 작업이 없습니다.",
        )

    affected = set(removed) | set(canceled)
    run_ids = {row["run_id"] for row in rows if row["id"] in affected}
    finished = iso(now())
    with db.transaction(conn):
        if removed:
            conn.execute(
                f"DELETE FROM tasks WHERE id IN ({','.join('?' for _ in removed)})", removed
            )
        if canceled:
            conn.execute(
                f"""UPDATE tasks SET status = 'skipped', device_id = NULL, claimed_at = NULL,
                          finished_at = ?,
                          error = COALESCE(error, '대시보드에서 대기열 삭제(진행 중 취소)')
                   WHERE id IN ({','.join('?' for _ in canceled)})""",
                [finished] + canceled,
            )
        runs = _recount_runs_after_delete(conn, run_ids)
        label = "전체" if (all_tasks or all_pending) else "선택"
        detail = []
        if removed:
            detail.append(f"{len(removed)}건 삭제")
        if canceled:
            detail.append(f"진행 중 {len(canceled)}건 취소")
        if kept:
            detail.append(f"완료·취소 {len(kept)}건 유지")
        db.log(conn, f"대기열 {label} 삭제: " + " · ".join(detail), "warning")

    return {
        "ok": True,
        "deleted_count": len(removed),
        "deleted_ids": removed,
        "canceled_count": len(canceled),
        "canceled_ids": canceled,
        "protected_ids": kept,
        "missing_ids": missing,
        "runs": runs,
    }


@api.delete("/tasks/{task_id}")
def delete_task(task_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """대기열 작업 1건 삭제(부분 삭제)."""
    return _delete_queue_tasks(conn, task_ids=[task_id])


@api.post("/tasks/bulk-delete")
def bulk_delete_tasks(payload: TaskBulkDelete, conn: sqlite3.Connection = Depends(get_conn)):
    """대기열 부분 삭제(task_ids) / 전체 삭제(all_pending 또는 all_tasks)."""
    return _delete_queue_tasks(
        conn,
        task_ids=payload.task_ids,
        all_pending=payload.all_pending,
        all_tasks=payload.all_tasks,
        run_id=payload.run_id,
    )


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

    # 수신 번호: 앱이 보낸 값(receiver_number/recipient) → 없으면 기기에 등록된 번호.
    receiver_number = (payload.receiver_number or "").strip()
    device_phone = (_row_value(device, "phone_number") or "").strip()
    if not receiver_number:
        receiver_number = device_phone
    elif not device_phone:
        # 기기에 번호가 비어 있는데 앱이 실제 수신 번호를 알려 줬다면 기기 정보도 채워 둔다.
        conn.execute("UPDATE devices SET phone_number = ? WHERE id = ?", (receiver_number[:30], payload.device_id))
        device_phone = receiver_number
        db.log(conn, f"기기 수신 번호 자동 등록: {payload.device_id} → {receiver_number}")

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
        """INSERT INTO sms_inbox(device_id, sender, receiver_number, body, received_at, created_at, telegram_status)
           VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
        (payload.device_id, payload.sender, receiver_number, payload.body, received_at, iso(now())),
    )
    sms_id = int(cursor.lastrowid)
    saved = save_sms_attachments(sms_id, payload.attachments)
    if saved:
        conn.execute(
            "UPDATE sms_inbox SET attachment_count = ?, attachment_paths = ? WHERE id = ?",
            (len(saved), json.dumps(saved, ensure_ascii=False), sms_id),
        )
    attachment_note = f", 사진·첨부 {len(saved)}건" if saved else ""
    db.log(conn, f"수신 문자 저장: {payload.sender} → {device['name']} (id={sms_id}{attachment_note})")

    settings = db.get_settings(conn)
    if settings.get("sms_forward_enabled", "1") == "1" and settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        background.add_task(_forward_sms, sms_id)
    else:
        conn.execute(
            "UPDATE sms_inbox SET telegram_status = 'skipped', telegram_error = ? WHERE id = ?",
            ("텔레그램 설정 또는 문자 전달이 꺼져 있습니다.", sms_id),
        )

    row = conn.execute("SELECT * FROM sms_inbox WHERE id = ?", (sms_id,)).fetchone()
    return {"ok": True, "id": sms_id, "sms": sms_dict(row, {payload.device_id: device_phone})}


def _attachment_extension(content_type: str, filename: str) -> str:
    """첨부 저장에 쓸 확장자. content-type 우선, 없으면 파일명에서."""
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/bmp": ".bmp",
        "video/mp4": ".mp4",
        "video/3gpp": ".3gp",
        "audio/mpeg": ".mp3",
        "audio/amr": ".amr",
        "text/vcard": ".vcf",
        "text/x-vcard": ".vcf",
    }
    base = (content_type or "").split(";")[0].strip().lower()
    if base in mapping:
        return mapping[base]
    suffix = Path(filename or "").suffix.lower()
    if suffix and len(suffix) <= 6 and suffix.isascii():
        return suffix
    return ".bin"


def save_sms_attachments(sms_id: int, attachments) -> list[dict]:
    """앱이 올린 Base64 첨부를 서버 파일로 저장한다(실패해도 문자 저장은 계속)."""
    if not attachments:
        return []
    saved: list[dict] = []
    target = config.MEDIA_DIR / "sms"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("첨부 저장 폴더를 만들지 못했습니다: %s", exc)
        return []
    for index, item in enumerate(attachments[: config.MAX_SMS_ATTACHMENTS]):
        try:
            raw = base64.b64decode(item.data_base64, validate=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("첨부 %s 디코딩 실패 (sms=%s): %s", index, sms_id, exc)
            continue
        if not raw:
            continue
        if len(raw) > config.MAX_ATTACHMENT_BYTES:
            logger.warning("첨부가 너무 큼 (sms=%s, %s bytes)", sms_id, len(raw))
            continue
        content_type = (item.content_type or "application/octet-stream").strip()
        extension = _attachment_extension(content_type, item.filename)
        path = target / f"{sms_id}_{index}{extension}"
        try:
            path.write_bytes(raw)
        except OSError as exc:
            logger.warning("첨부 파일 저장 실패 (sms=%s): %s", sms_id, exc)
            continue
        saved.append(
            {
                "path": str(path),
                "filename": (item.filename or f"attachment-{index}{extension}").strip()[:200],
                "content_type": content_type,
                "size": len(raw),
            }
        )
    return saved


def _forward_sms(sms_id: int) -> None:
    conn = db.connect()
    try:
        ok, error = telegram_client.deliver_sms_notification(conn, sms_id)
        if not ok:
            logger.warning("텔레그램 전달 실패 (sms=%s): %s", sms_id, error)
    finally:
        conn.close()


@api.get("/sms")
def list_sms(
    limit: int = Query(default=50, ge=1, le=500),
    device_id: Optional[str] = Query(default=None, max_length=64),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """수신 문자 목록(최신 순). MMS 사진은 attachments[].url 로 바로 볼 수 있다."""
    where = ""
    values: list = []
    if device_id:
        where = " WHERE device_id = ?"
        values.append(device_id)
    rows = conn.execute(
        f"SELECT * FROM sms_inbox{where} ORDER BY id DESC LIMIT ?", values + [limit]
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) AS c FROM sms_inbox{where}", values).fetchone()["c"]
    names = {row["id"]: (row["name"] or row["id"]) for row in conn.execute("SELECT id, name FROM devices")}
    phones = {row["id"]: (_row_value(row, "phone_number") or "") for row in conn.execute("SELECT * FROM devices")}
    items = []
    for row in rows:
        item = sms_dict(row, phones)
        item["device_name"] = names.get(row["device_id"], row["device_id"])
        items.append(item)
    return {"messages": items, "count": len(items), "total": int(total or 0)}


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


@api.get("/sms/{sms_id}/attachments/{index}")
def get_sms_attachment(sms_id: int, index: int, conn: sqlite3.Connection = Depends(get_conn)):
    """MMS 첨부 파일 보기/내려받기(대시보드에서 확인용)."""
    row = conn.execute("SELECT * FROM sms_inbox WHERE id = ?", (sms_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="수신 문자를 찾을 수 없습니다.")
    attachments = sms_attachment_list(row)
    if index < 0 or index >= len(attachments):
        raise HTTPException(status_code=404, detail="첨부를 찾을 수 없습니다.")
    items = _sms_attachment_items(row)
    item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
    path = Path(str(item.get("path") or ""))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="첨부 파일이 서버에 남아 있지 않습니다.")
    if not _inside_sms_media(path):
        # DB 값이 임의로 바뀌었더라도 미디어 폴더 밖 파일은 내보내지 않는다.
        raise HTTPException(status_code=403, detail="허용되지 않은 첨부 경로입니다.")
    return FileResponse(
        path,
        media_type=str(item.get("content_type") or "application/octet-stream"),
        filename=_safe_download_name(str(item.get("filename") or path.name)),
    )


# --------------------------------------------------------------------------
# 수신 문자 삭제
# --------------------------------------------------------------------------
def _sms_attachment_items(row: sqlite3.Row) -> list:
    try:
        items = json.loads(_row_value(row, "attachment_paths") or "[]")
    except (TypeError, ValueError):
        items = []
    return items if isinstance(items, list) else []


def _sms_media_root() -> Path:
    try:
        return (Path(config.MEDIA_DIR) / "sms").resolve()
    except OSError:
        return Path(config.MEDIA_DIR) / "sms"


def _inside_sms_media(path: Path) -> bool:
    """첨부 파일이 미디어 폴더(media/sms) 안에 있는지 확인한다(경로 탈출 차단)."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    root = _sms_media_root()
    return resolved == root or root in resolved.parents


def _safe_download_name(name: str) -> str:
    """내려받기 파일명에서 경로 구분자·제어문자를 없앤다."""
    cleaned = Path(name.replace("\\", "/")).name
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return cleaned[:200] or "attachment"


def _delete_sms_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict:
    """수신 문자 행과 서버에 저장된 MMS 첨부 파일을 함께 지운다.

    먼저 DB 행을 지우고(실패하면 파일도 그대로), 그 다음 미디어 폴더 안의 첨부 파일을 정리한다.
    """
    removed_ids: list[int] = []
    files_removed = 0
    files_kept = 0
    targets: list[Path] = []
    for row in rows:
        removed_ids.append(int(row["id"]))
        for item in _sms_attachment_items(row):
            if not isinstance(item, dict):
                continue
            path = Path(str(item.get("path") or ""))
            if path.is_file() and _inside_sms_media(path):
                targets.append(path)
            elif path.is_file():
                # 미디어 폴더 밖 파일은 안전을 위해 지우지 않는다.
                files_kept += 1

    if removed_ids:
        with db.transaction(conn):
            conn.execute(
                f"DELETE FROM sms_inbox WHERE id IN ({','.join('?' for _ in removed_ids)})",
                removed_ids,
            )
            db.log(conn, f"수신 문자 삭제: {len(removed_ids)}건", "warning")

    for path in targets:
        try:
            path.unlink()
            files_removed += 1
        except OSError as exc:  # noqa: BLE001
            logger.warning("첨부 파일을 지우지 못했습니다 (%s): %s", path.name, exc)
            files_kept += 1

    return {
        "ok": True,
        "deleted_count": len(removed_ids),
        "deleted_ids": removed_ids,
        "files_removed": files_removed,
        "files_kept": files_kept,
    }


@api.delete("/sms/{sms_id}")
def delete_sms(sms_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """수신 문자 1건 삭제(첨부 파일도 함께 정리)."""
    row = conn.execute("SELECT * FROM sms_inbox WHERE id = ?", (sms_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="수신 문자를 찾을 수 없습니다.")
    return _delete_sms_rows(conn, [row])


@api.post("/sms/delete")
def bulk_delete_sms(payload: SmsBulkDelete, conn: sqlite3.Connection = Depends(get_conn)):
    """수신 문자 부분 삭제(ids) / 전체 삭제(all_sms)."""
    if payload.all_sms:
        rows = conn.execute("SELECT * FROM sms_inbox").fetchall()
        requested: list[int] = []
    else:
        requested = list(payload.ids)
        rows = conn.execute(
            f"SELECT * FROM sms_inbox WHERE id IN ({','.join('?' for _ in requested)})",
            requested,
        ).fetchall()
    found_ids = {int(row["id"]) for row in rows}
    result = _delete_sms_rows(conn, rows)
    result["missing_ids"] = [sms_id for sms_id in requested if sms_id not in found_ids]
    return result


@api.delete("/sms")
def delete_all_sms(conn: sqlite3.Connection = Depends(get_conn)):
    """수신 문자 전체 삭제(POST /api/sms/delete + all_sms=true 와 같은 동작)."""
    rows = conn.execute("SELECT * FROM sms_inbox").fetchall()
    return _delete_sms_rows(conn, rows)


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
        "원격 기기 설정 저장: 확인 {}초 · 간격 {}초 · SMS 전달 {} · MMS 전달 {} · 통화 녹음 {} · 통화 볼륨 최소화 {} · 내 소리 차단 {}".format(
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
