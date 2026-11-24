"""FastAPI 애플리케이션 (중앙 서버)."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import apps_page, config, db, macro_store, scheduler
from .db import json_list
from .models import (
    ArchiveRestore,
    DeviceRegister,
    DeviceUpdate,
    Heartbeat,
    MacroBackupIn,
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
from .phoneutil import normalize_phone
from .timeutil import end_date_deadline, iso, local_str, next_occurrence, now, now_iso, parse_dt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("auto-dialer")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# '전체 보기'에서 한 번에 돌려주는 최대 행 수(서버 보호용 안전 상한).
LOG_ALL_LIMIT = 10000
ARCHIVE_ALL_LIMIT = 5000


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


def _is_date_only(text: str) -> bool:
    """'2026-11-22' 처럼 날짜만 입력됐는지 확인한다(종료 시간은 그날 끝까지로 해석)."""
    cleaned = (text or "").strip()
    return len(cleaned) <= 10 and "T" not in cleaned and ":" not in cleaned


def _time_param(value: str | None, label: str = "시간", end_of_day: bool = False) -> str | None:
    """시간 필터 입력(ISO8601 · KST · 날짜만)을 DB 비교용 ISO 문자열로 바꾼다."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        dt = parse_dt(text)
    except ValueError as exc:  # noqa: F841
        raise HTTPException(
            status_code=400, detail=f"{label} 형식을 해석할 수 없습니다: {text} (예: 2026-11-22T09:00 또는 2026-11-22)"
        ) from None
    if dt is None:
        return None
    if end_of_day and _is_date_only(text):
        dt = dt.replace(hour=23, minute=59, second=59)
    return iso(dt)


def _time_range_clauses(column: str, start: str | None, end: str | None) -> tuple[list[str], list]:
    """시작/종료 시간 조건. 저장값이 오프셋을 포함해도 맞도록 datetime() 으로 비교한다."""
    clauses: list[str] = []
    values: list = []
    start_iso = _time_param(start, "시작 시간")
    end_iso = _time_param(end, "종료 시간", end_of_day=True)
    if start_iso and end_iso and start_iso > end_iso:
        raise HTTPException(status_code=400, detail="시작 시간이 종료 시간보다 늦습니다.")
    if start_iso:
        clauses.append(f"datetime({column}) >= datetime(?)")
        values.append(start_iso)
    if end_iso:
        clauses.append(f"datetime({column}) <= datetime(?)")
        values.append(end_iso)
    return clauses, values


def _search_clause(columns: list[str], q: str | None) -> tuple[str | None, list]:
    """여러 컬럼을 한 번에 대소문자 구분 없이 부분 일치 검색한다."""
    term = (q or "").strip()
    if not term:
        return None, []
    like = f"%{term.lower()}%"
    clause = "(" + " OR ".join(f"LOWER(COALESCE({column}, '')) LIKE ?" for column in columns) + ")"
    return clause, [like] * len(columns)


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
        "name_customized": bool(_row_value(row, "name_customized", 0) or 0),
        "phone_customized": bool(_row_value(row, "phone_customized", 0) or 0),
        "model_customized": bool(_row_value(row, "model_customized", 0) or 0),
        "android_customized": bool(_row_value(row, "android_customized", 0) or 0),
        "enabled": bool(row["enabled"]),
        "online": device_is_online(row, current),
        "last_seen": row["last_seen"],
        "last_seen_text": local_str(row["last_seen"], "%m-%d %H:%M:%S") if row["last_seen"] else "-",
        "seconds_since_seen": int((current - last_seen).total_seconds()) if last_seen else None,
        "created_at": row["created_at"],
    }


def resolve_device_key(conn: sqlite3.Connection, device_id: str | None) -> str:
    """요청이 보낸 기기 ID 를 지금의 기기 키(정규화된 수신번호)로 푼다.

    - 이미 전화번호면 정규화한 값(010-5656-3939)을 쓴다.
    - 옛 앱이 보내는 예전 기기 ID(예: 5656)는 device_aliases 를 거쳐 수신번호로 바꾼다.
      (폰에 새 APK 를 설치하기 전에도 같은 기기로 이어진다)
    - 그 밖의 값은 그대로 돌려준다(빈 값은 "").
    """
    raw = (device_id or "").strip()
    if not raw:
        return ""
    phone = normalize_phone(raw)
    if phone:
        return phone
    aliased = db.device_alias_target(conn, raw)
    return aliased or raw


def ensure_device(conn: sqlite3.Connection, device_id: str, name: str | None = None) -> sqlite3.Row:
    device_id = resolve_device_key(conn, device_id)
    if not device_id:
        raise HTTPException(status_code=400, detail="기기 키(수신번호)가 필요합니다.")
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        phone = normalize_phone(device_id)
        if not phone:
            raise HTTPException(
                status_code=400,
                detail="기기 키는 수신번호입니다. 폰 앱(v1.3.7 이상)에서 전화번호 읽기 권한을 허용하고 다시 연결하세요.",
            )
        conn.execute(
            "INSERT INTO devices(id, name, phone_number, created_at) VALUES (?, ?, ?, ?)",
            (phone, (name or phone)[:80], phone, iso(now())),
        )
        db.log(conn, f"기기 자동 등록: {phone}", "info")
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (phone,)).fetchone()
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


def _schedule_task_counts(conn: sqlite3.Connection) -> dict[int, dict]:
    """스케줄별 발신 작업 집계(보낸 회수 관리). 한 번의 질의로 모든 스케줄을 모은다."""
    rows = conn.execute(
        """SELECT schedule_id,
                  SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                  SUM(CASE WHEN status IN ('pending','assigned','running') THEN 1 ELSE 0 END) AS pending_count,
                  SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                  COUNT(*) AS total_tasks,
                  MAX(CASE WHEN status = 'done' THEN finished_at END) AS last_sent_at
           FROM tasks WHERE deleted_at IS NULL AND schedule_id IS NOT NULL
           GROUP BY schedule_id"""
    ).fetchall()
    return {
        int(row["schedule_id"]): {
            "done_count": int(row["done_count"] or 0),
            "failed_count": int(row["failed_count"] or 0),
            "pending_count": int(row["pending_count"] or 0),
            "skipped_count": int(row["skipped_count"] or 0),
            "total_tasks": int(row["total_tasks"] or 0),
            "last_sent_at": row["last_sent_at"],
        }
        for row in rows
    }


def _empty_task_counts() -> dict:
    return {
        "done_count": 0,
        "failed_count": 0,
        "pending_count": 0,
        "skipped_count": 0,
        "total_tasks": 0,
        "last_sent_at": None,
    }


def schedule_dict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    devices: dict[str, dict] | None = None,
    task_counts: dict[int, dict] | None = None,
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
        "SELECT * FROM runs WHERE schedule_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (row["id"],)
    ).fetchone()
    opens = conn.execute(
        """SELECT COUNT(*) AS c FROM tasks
           WHERE schedule_id = ? AND deleted_at IS NULL AND status IN ('pending','assigned','running')""",
        (row["id"],),
    ).fetchone()["c"]
    end_date = (_row_value(row, "end_date") or "").strip()
    deadline = end_date_deadline(end_date)
    if task_counts is not None:
        counts = dict(task_counts.get(int(row["id"])) or _empty_task_counts())
    else:
        counts = _schedule_task_counts(conn).get(int(row["id"])) or _empty_task_counts()
    counts["run_count"] = row["run_count"] or 0
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
        "end_date": end_date,
        "end_date_text": end_date or "",
        "end_date_expired": bool(deadline is not None and now() > deadline),
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
        "stats": counts,
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


@app.get("/apps", include_in_schema=False)
def apps_download_page(request: Request):
    """폰 브라우저용 앱 다운로드 페이지(앱별 최신 APK 카드)."""
    items = _list_app_apks(request)
    return HTMLResponse(
        apps_page.render_apps_page(items),
        headers={"Cache-Control": "no-store"},
    )


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

    active_schedules = conn.execute(
        "SELECT COUNT(*) AS c FROM schedules WHERE status = 'active' AND deleted_at IS NULL"
    ).fetchone()["c"]
    pending_tasks = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NULL AND status IN ('pending','assigned','running')"
    ).fetchone()["c"]
    done_today = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NULL AND status = 'done' AND finished_at >= ?",
        (today_start,),
    ).fetchone()["c"]
    failed_today = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NULL AND status = 'failed' AND finished_at >= ?",
        (today_start,),
    ).fetchone()["c"]
    calls_today = conn.execute(
        """SELECT COUNT(*) AS c FROM tasks
           WHERE deleted_at IS NULL AND type = 'call' AND status IN ('done','failed') AND finished_at >= ?""",
        (today_start,),
    ).fetchone()["c"]
    sms_today = conn.execute(
        "SELECT COUNT(*) AS c FROM sms_inbox WHERE deleted_at IS NULL AND created_at >= ?", (today_start,)
    ).fetchone()["c"]
    # 보낸(발신) 문자·전화 건수: 수신 문자(sms_inbox)와 구분해 발신 작업(tasks)에서 센다.
    sms_sent_today = conn.execute(
        """SELECT COUNT(*) AS c FROM tasks
           WHERE deleted_at IS NULL AND type = 'sms' AND status = 'done' AND finished_at >= ?""",
        (today_start,),
    ).fetchone()["c"]
    call_done_today = conn.execute(
        """SELECT COUNT(*) AS c FROM tasks
           WHERE deleted_at IS NULL AND type = 'call' AND status = 'done' AND finished_at >= ?""",
        (today_start,),
    ).fetchone()["c"]
    sent_totals = conn.execute(
        """SELECT
             SUM(CASE WHEN type = 'sms' AND status = 'done' THEN 1 ELSE 0 END) AS sms_done,
             SUM(CASE WHEN type = 'sms' AND status = 'failed' THEN 1 ELSE 0 END) AS sms_failed,
             SUM(CASE WHEN type = 'call' AND status = 'done' THEN 1 ELSE 0 END) AS call_done,
             SUM(CASE WHEN type = 'call' AND status = 'failed' THEN 1 ELSE 0 END) AS call_failed
           FROM tasks WHERE deleted_at IS NULL"""
    ).fetchone()
    telegram_failed = conn.execute(
        """SELECT COUNT(*) AS c FROM sms_inbox
           WHERE deleted_at IS NULL AND telegram_status IN ('failed','pending')"""
    ).fetchone()["c"]
    upcoming = conn.execute(
        """SELECT * FROM schedules WHERE status = 'active' AND deleted_at IS NULL AND next_run_at IS NOT NULL
           ORDER BY next_run_at ASC LIMIT 5"""
    ).fetchall()
    recent_runs = conn.execute("SELECT * FROM runs WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 5").fetchall()

    return {
        "devices": {"total": len(devices), "online": online},
        "schedules": {"active": active_schedules},
        "tasks": {
            "pending": pending_tasks,
            "done_today": done_today,
            "failed_today": failed_today,
            "calls_today": calls_today,
        },
        "sent": {
            "sms_today": sms_sent_today,
            "call_today": call_done_today,
            "sms_done": int(sent_totals["sms_done"] or 0),
            "sms_failed": int(sent_totals["sms_failed"] or 0),
            "call_done": int(sent_totals["call_done"] or 0),
            "call_failed": int(sent_totals["call_failed"] or 0),
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
    """기기 등록/갱신. 기기 키는 정규화된 수신번호 하나뿐이다(devices.id == devices.phone_number).

    앱이 유심에서 읽은 번호를 phone_number 로 보내면 그 값이 키가 된다. 옛 앱처럼
    device_id 에 번호를 담아 보내도 같은 규칙으로 읽는다. 번호를 읽지 못했으면
    422 로 분명히 알린다(번호 없는 기기는 만들지 않는다).
    """
    current = iso(now())
    phone = normalize_phone(payload.phone_number) or normalize_phone(payload.device_id)
    if not phone:
        raise HTTPException(
            status_code=422,
            detail=(
                "수신번호(기기 키)가 필요합니다. 폰에서 유심 전화번호를 읽지 못했습니다. "
                "앱의 ‘권한 허용’을 눌러 전화번호 읽기 권한을 허용한 뒤 다시 시작하세요."
            ),
        )
    # 옛 앱이 예전 기기 ID(예: 5656)로 등록하면서 번호를 알려주면, 번호가 비어 있던
    # 그 기기 줄을 그대로 수신번호 키로 옮긴다(기록·메모 유지).
    legacy_id = (payload.device_id or "").strip()
    if legacy_id and not normalize_phone(legacy_id) and legacy_id != phone:
        legacy_row = conn.execute(
            "SELECT phone_number FROM devices WHERE id = ?", (legacy_id,)
        ).fetchone()
        if legacy_row is not None and not (legacy_row["phone_number"] or "").strip():
            db.move_device_records(conn, legacy_id, phone)
    # 옛 기기 ID 로 들어와도 같은 기기로 잇는다.
    phone = resolve_device_key(conn, phone)
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (phone,)).fetchone()
    # 앱이 보낸 기기 이름(공백 제거). 비어 있으면 지금 이름을 그대로 둔다.
    name_value = (payload.name or "").strip()
    if row is None:
        conn.execute(
            """INSERT INTO devices(id, name, model, android_version, app_version, phone_number, note, enabled, last_seen, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                phone,
                name_value or phone,
                payload.model,
                payload.android_version,
                payload.app_version,
                phone,
                payload.note,
                current,
                current,
            ),
        )
        db.log(conn, f"기기 등록: {phone} ({payload.name})")
    else:
        # 앱은 30초마다 같은 등록 요청을 보낸다. 대시보드에서 직접 고친 이름·모델·안드로이드 버전은 덮어쓰지 않는다.
        # 수신번호는 기기 키이므로 앱이 무엇을 보내든 서버 값(=키)을 그대로 둔다.
        #
        # 예외: 사용자가 앱 화면에서 기기 이름을 직접 고쳐 force_name=true 로 보낸 경우에는
        # 그 이름이 마지막 의도된 값이므로 대시보드에서 고친 이름도 이 이름으로 바꾼다
        # (웹·앱 기기 이름을 어느 쪽에서 고쳐도 같게 유지).
        force_name = bool(payload.force_name) and bool(name_value)
        name_sql = (
            "name = ?, name_customized = 1"
            if force_name
            else "name = CASE WHEN COALESCE(name_customized, 0) = 1 THEN name\n"
            "ELSE COALESCE(NULLIF(?, ''), name) END"
        )
        conn.execute(
            f"""UPDATE devices SET
                      {name_sql},
                      model = CASE WHEN COALESCE(model_customized, 0) = 1 THEN model
                                   ELSE COALESCE(NULLIF(?, ''), model) END,
                      android_version = CASE WHEN COALESCE(android_customized, 0) = 1 THEN android_version
                                             ELSE COALESCE(NULLIF(?, ''), android_version) END,
                      app_version = ?,
                      phone_number = ?,
                      note = COALESCE(NULLIF(?, ''), note), last_seen = ? WHERE id = ?""",
            (
                name_value,
                payload.model,
                payload.android_version,
                payload.app_version or row["app_version"],
                phone,
                payload.note,
                current,
                phone,
            ),
        )
        if force_name:
            db.log(conn, f"기기 이름을 앱에서 반영: {phone} → {name_value}", "info")
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (phone,)).fetchone()
    return {
        "ok": True,
        "device": device_dict(row),
        # 앱 화면이 서버의 기기 이름과 바로 같아지도록 현재 이름을 함께 돌려준다.
        "device_name": row["name"] or phone,
    }


@api.post("/devices/{device_id}/heartbeat")
def device_heartbeat(device_id: str, payload: Heartbeat | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    body = payload or Heartbeat(device_id=device_id)
    row = ensure_device(conn, device_id)
    device_key = row["id"]
    cfg = remote_config(conn, device_key)
    conn.execute(
        "UPDATE devices SET last_seen = ?, battery = COALESCE(?, battery), network = COALESCE(NULLIF(?, ''), network) WHERE id = ?",
        (iso(now()), body.battery, body.network, device_key),
    )
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NULL AND status = 'pending' AND (device_id IS NULL OR device_id = ?)",
        (device_key,),
    ).fetchone()["c"]
    return {
        "ok": True,
        "server_time": iso(now()),
        "poll_interval_sec": cfg["poll_interval_sec"],
        "pending_tasks": pending,
        # 앱 화면의 기기 이름이 대시보드와 같아지도록 현재 이름을 함께 내려준다.
        "device_name": row["name"] or device_key,
        # 대시보드에서 바꾼 원격 설정(확인 주기·발신 간격·문자 전달·기기 사용 여부)
        "config": cfg,
    }


@api.patch("/devices/{device_id}")
def update_device(device_id: str, payload: DeviceUpdate, conn: sqlite3.Connection = Depends(get_conn)):
    """기기 정보 편집. 기기 이름·모델·안드로이드 버전·메모·사용 여부만 바꿀 수 있다.

    기기 키(수신번호)는 바꿀 수 없다 — 스케줄·대기열·수신 문자가 모두 이 키를 가리키므로
    바뀌면 기록이 어긋난다. new_device_id · phone_number 로 다른 값을 보내면 422.
    """
    device_key = resolve_device_key(conn, device_id)
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_key,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="기기를 찾을 수 없습니다.")

    if payload.new_device_id is not None and payload.new_device_id != row["id"]:
        raise HTTPException(status_code=422, detail="수신번호(기기 키)는 수정할 수 없습니다.")
    if payload.phone_number is not None:
        given = normalize_phone(payload.phone_number) or payload.phone_number.strip()
        if given != row["id"]:
            raise HTTPException(status_code=422, detail="수신번호(기기 키)는 수정할 수 없습니다.")

    updates, values = [], []
    if payload.name is not None:
        updates.append("name = ?")
        values.append(payload.name[:80] or row["id"])
        # 대시보드에서 직접 고친 이름은 앱 등록 정보가 덮어쓰지 않도록 표시한다.
        updates.append("name_customized = 1")
    if payload.note is not None:
        updates.append("note = ?")
        values.append(payload.note[:400])
    if payload.model is not None:
        updates.append("model = ?")
        values.append(payload.model[:80])
        # 비우면 앱이 알려주는 모델명을 다시 받아들이고, 값을 넣으면 그 값으로 고정한다.
        updates.append(f"model_customized = {1 if payload.model else 0}")
    if payload.android_version is not None:
        updates.append("android_version = ?")
        values.append(payload.android_version[:40])
        updates.append(f"android_customized = {1 if payload.android_version else 0}")
    if payload.enabled is not None:
        updates.append("enabled = ?")
        values.append(1 if payload.enabled else 0)

    if not updates:
        return {"ok": True, "device": device_dict(row), "renamed_from": None}

    try:
        conn.execute(f"UPDATE devices SET {', '.join(updates)} WHERE id = ?", values + [row["id"]])
        db.log(conn, f"기기 정보 수정: {row['id']}")
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"기기 정보를 저장하지 못했습니다: {exc}") from exc

    updated = conn.execute("SELECT * FROM devices WHERE id = ?", (row["id"],)).fetchone()
    return {"ok": True, "device": device_dict(updated), "renamed_from": None}


@api.delete("/devices/{device_id}")
def delete_device(device_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    device_id = resolve_device_key(conn, device_id)
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
    rows = conn.execute("SELECT * FROM schedules WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()
    devices = device_lookup(conn)
    task_counts = _schedule_task_counts(conn)
    return {"schedules": [schedule_dict(conn, row, devices, task_counts) for row in rows]}


@api.post("/schedules")
def create_schedule(payload: ScheduleCreate, conn: sqlite3.Connection = Depends(get_conn)):
    start_at = parse_dt(payload.start_at)
    assert start_at is not None
    created = iso(now())
    with db.transaction(conn):
        cursor = conn.execute(
            """INSERT INTO schedules(name, type, numbers, repeat_count, total_calls, order_mode, max_call_duration_sec,
                                     sms_body, start_at, end_date, recurrence, device_id, target_device_ids, status, next_run_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
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
                payload.end_date or "",
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
        end_text = f", 종료일 {payload.end_date}" if payload.end_date else ""
        db.log(
            conn,
            f"스케줄 등록: '{payload.name}' {payload.type} {amount} "
            f"({device_text}, {payload.order_mode}, {payload.recurrence}{end_text}, {iso(start_at)})",
        )
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return {"ok": True, "schedule": schedule_dict(conn, row)}


@api.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    return {"schedule": schedule_dict(conn, row)}


# 보낸 회수 집계용 공통 SELECT. 번호별·기기별 조회는 GROUP BY 를 붙여 쓴다.
_TASK_COUNTS_SELECT = """
    SELECT {extra}
      SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count,
      SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
      SUM(CASE WHEN status IN ('pending','assigned','running') THEN 1 ELSE 0 END) AS pending_count,
      SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
      COUNT(*) AS total_tasks,
      MAX(CASE WHEN status = 'done' THEN finished_at END) AS last_sent_at
    FROM tasks WHERE deleted_at IS NULL AND schedule_id = ? {group}
"""

_EMPTY_COUNT_ROW = {
    "done_count": 0,
    "failed_count": 0,
    "pending_count": 0,
    "skipped_count": 0,
    "total_tasks": 0,
    "last_sent_at": None,
}


def _count_bundle(target) -> dict:
    """집계 행(dict/sqlite3.Row/None)을 응답용 건수 묶음으로 바꾼다."""
    target = target or _EMPTY_COUNT_ROW
    last_sent = target["last_sent_at"]
    return {
        "done_count": int(target["done_count"] or 0),
        "failed_count": int(target["failed_count"] or 0),
        "pending_count": int(target["pending_count"] or 0),
        "skipped_count": int(target["skipped_count"] or 0),
        "total_tasks": int(target["total_tasks"] or 0),
        "last_sent_at": last_sent,
        "last_sent_text": local_str(last_sent, "%Y-%m-%d %H:%M:%S") if last_sent else "-",
    }


@api.get("/schedules/{schedule_id}/stats")
def schedule_sent_stats(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """스케줄 하나의 보낸 회수 상세: 전체·번호별·기기별 성공/실패/대기 건수."""
    row = conn.execute("SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")

    totals = _count_bundle(
        conn.execute(_TASK_COUNTS_SELECT.format(extra="", group=""), (schedule_id,)).fetchone()
    )
    totals["run_count"] = row["run_count"] or 0

    # 번호별: 스케줄에 등록된 번호 순서를 그대로 유지하고, 기록만 있는 번호는 뒤에 붙인다.
    number_rows = conn.execute(
        _TASK_COUNTS_SELECT.format(
            extra="phone_number,",
            group="GROUP BY phone_number ORDER BY MIN(seq) ASC, MIN(id) ASC",
        ),
        (schedule_id,),
    ).fetchall()
    by_number = {entry["phone_number"]: entry for entry in number_rows}
    per_number: list[dict] = []
    seen_numbers: set[str] = set()
    for number in json_list(row["numbers"]):
        per_number.append({"number": number, **_count_bundle(by_number.get(number))})
        seen_numbers.add(number)
    for entry in number_rows:
        if entry["phone_number"] in seen_numbers:
            continue
        per_number.append({"number": entry["phone_number"], **_count_bundle(entry)})

    # 기기별: 기기 이름·번호를 붙여 준다.
    devices = device_lookup(conn)
    device_rows = conn.execute(
        _TASK_COUNTS_SELECT.format(
            extra="device_id,",
            group="GROUP BY device_id ORDER BY MIN(seq) ASC, MIN(id) ASC",
        ),
        (schedule_id,),
    ).fetchall()
    target_ids = db.json_list(_row_value(row, "target_device_ids"))
    if not target_ids and row["device_id"]:
        target_ids = [row["device_id"]]
    per_device: list[dict] = []
    seen_devices: set[str] = set()

    def _device_item(device_id: str, counts: dict) -> dict:
        info = devices.get(device_id) if device_id else None
        return {
            "device_id": device_id,
            "device_name": (info.get("name") if info else "") or device_id or "자동 배분",
            "phone_number": (info.get("phone_number") if info else "") or "",
            **counts,
        }

    for entry in device_rows:
        device_id = entry["device_id"] or ""
        per_device.append(_device_item(device_id, _count_bundle(entry)))
        seen_devices.add(device_id)
    for device_id in target_ids:
        if device_id in seen_devices:
            continue
        per_device.append(_device_item(device_id, _count_bundle(None)))

    end_date = (_row_value(row, "end_date") or "").strip()
    deadline = end_date_deadline(end_date)
    return {
        "schedule_id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "status": row["status"],
        "sms_body": row["sms_body"],
        "start_at": row["start_at"],
        "start_at_text": local_str(row["start_at"], "%Y-%m-%d %H:%M"),
        "end_date": end_date,
        "end_date_expired": bool(deadline is not None and now() > deadline),
        "next_run_at": row["next_run_at"],
        "next_run_text": local_str(row["next_run_at"], "%Y-%m-%d %H:%M") if row["next_run_at"] else "-",
        "numbers": per_number,
        "devices": per_device,
        **totals,
    }


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
    row = conn.execute("SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)).fetchone()
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
        "end_date": (_row_value(row, "end_date") or "").strip(),
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
    if "end_date" in provided and payload.end_date is not None:
        new["end_date"] = payload.end_date
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
    deadline = end_date_deadline(new["end_date"])
    if deadline is not None and start_dt > deadline:
        raise HTTPException(
            status_code=422,
            detail=(
                f"실행 시각({start_dt.strftime('%Y-%m-%d %H:%M')})이 실행 종료일({new['end_date']})보다 뒤입니다. "
                "시작 시각이나 종료일을 다시 정하세요."
            ),
        )
    if not new["name"]:
        label = "전화" if new["type"] == "call" else "문자"
        new["name"] = f"{label} 발신 {start_dt.strftime('%m-%d %H:%M')}"

    # 다음 실행 시각 · 상태
    current = now()
    schedule_changed = bool({"start_at", "recurrence", "end_date"} & provided)
    explicitly_status = "status" in provided and payload.status is not None
    if schedule_changed and not explicitly_status and new["status"] == "finished" and start_dt > current:
        # 지난 1회성 일정의 실행 시각을 미래로 옮기면 다시 예약한다.
        new["status"] = "active"
    if schedule_changed and not explicitly_status and new["status"] == "finished" and "end_date" in provided:
        # 종료일을 늘렸을 때 아직 남은 반복이 있으면 다시 예약한다.
        candidate = _next_run_after_edit(
            start_dt, new["recurrence"], current, run_count=row["run_count"] or 0
        )
        candidate_dt = parse_dt(candidate) if candidate else None
        if candidate_dt is not None and (deadline is None or candidate_dt <= deadline):
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

    # 종료일을 넘긴 예약은 실행하지 않는다.
    next_run_dt = parse_dt(next_run) if next_run else None
    if deadline is not None and next_run_dt is not None and next_run_dt > deadline:
        next_run = None
        if new["status"] == "active":
            new["status"] = "finished"

    with db.transaction(conn):
        conn.execute(
            """UPDATE schedules SET name = ?, type = ?, numbers = ?, repeat_count = ?, total_calls = ?,
                                  order_mode = ?, max_call_duration_sec = ?, sms_body = ?, start_at = ?, end_date = ?,
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
                new["end_date"] or "",
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
        if before["end_date"] != new["end_date"]:
            changes.append(f"종료일 {before['end_date'] or '없음'}→{new['end_date'] or '없음'}")
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
    row = conn.execute("SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)).fetchone()
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
    deadline = end_date_deadline((_row_value(row, "end_date") or "").strip())
    if deadline is not None and next_run > deadline:
        # 종료일을 넘겨 재개하면 바로 다시 종료되므로, 그 뜻을 분명히 알려 준다.
        conn.execute(
            "UPDATE schedules SET status = 'finished', next_run_at = NULL WHERE id = ?", (schedule_id,)
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"이 스케줄은 실행 종료일({row['end_date']})이 지나 재개할 수 없습니다. "
                "종료일을 늘리거나 지운 뒤 다시 재개하세요."
            ),
        )
    conn.execute(
        "UPDATE schedules SET status = 'active', next_run_at = ? WHERE id = ?",
        (iso(next_run), schedule_id),
    )
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return {"ok": True, "schedule": schedule_dict(conn, row)}


@api.post("/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    run_id = scheduler.run_schedule_now(conn, schedule_id)
    if run_id is None:
        raise HTTPException(status_code=400, detail="발신할 번호가 없습니다.")
    return {"ok": True, "run_id": run_id}


def _archive_schedules(conn: sqlite3.Connection, schedule_ids: list[int]) -> dict:
    """스케줄을 소프트 삭제해 삭제 보관함으로 옮긴다.

    - 스케줄은 status='paused' + deleted_at 으로 바꿔 예약이 저절로 나가지 않게 한다.
    - 대기·배정·진행 중 작업은 취소(skipped)해서 폰이 절대 발신하지 않게 한다.
    - 실행 기록(runs)과 발신 작업(tasks)도 deleted_at 을 채워 함께 보관한다.
      (행을 지우지 않으므로 나중에 삭제 보관함에서 찾아보거나 복원할 수 있다)
    """
    empty = {
        "deleted_ids": [],
        "deleted": 0,
        "deleted_runs": 0,
        "deleted_tasks": 0,
        "canceled_tasks": 0,
        "missing_ids": [],
        "soft_deleted": True,
    }
    ids = [int(item) for item in dict.fromkeys(schedule_ids or [])]
    if not ids:
        return empty
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, name FROM schedules WHERE id IN ({placeholders}) AND deleted_at IS NULL", ids
    ).fetchall()
    found = [row["id"] for row in rows]
    found_set = set(found)
    missing = [item for item in ids if item not in found_set]
    if not found:
        return {**empty, "missing_ids": missing}

    found_placeholders = ",".join("?" for _ in found)
    run_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM runs WHERE schedule_id IN ({found_placeholders}) AND deleted_at IS NULL", found
        )
    ]
    task_scope = f"schedule_id IN ({found_placeholders})"
    task_values: list = list(found)
    if run_ids:
        task_scope += f" OR run_id IN ({','.join('?' for _ in run_ids)})"
        task_values += run_ids

    deleted_at = now_iso()
    canceled = conn.execute(
        f"""UPDATE tasks SET status = 'skipped', device_id = NULL, claimed_at = NULL, finished_at = ?,
                          error = COALESCE(error, '스케줄 삭제로 취소(삭제 보관함)')
           WHERE deleted_at IS NULL AND status IN ('pending','assigned','running') AND ({task_scope})""",
        [deleted_at] + task_values,
    ).rowcount
    deleted_tasks = conn.execute(
        f"UPDATE tasks SET deleted_at = ? WHERE ({task_scope}) AND deleted_at IS NULL",
        [deleted_at] + task_values,
    ).rowcount
    deleted_runs = conn.execute(
        f"UPDATE runs SET deleted_at = ? WHERE schedule_id IN ({found_placeholders}) AND deleted_at IS NULL",
        [deleted_at] + found,
    ).rowcount
    deleted_schedules = conn.execute(
        f"UPDATE schedules SET deleted_at = ?, status = 'paused' WHERE id IN ({found_placeholders}) AND deleted_at IS NULL",
        [deleted_at] + found,
    ).rowcount
    return {
        "deleted_ids": found,
        "deleted": int(deleted_schedules or 0),
        "deleted_runs": int(deleted_runs or 0),
        "deleted_tasks": int(deleted_tasks or 0),
        "canceled_tasks": int(canceled or 0),
        "missing_ids": missing,
        "soft_deleted": True,
    }


@api.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """스케줄 1개 삭제(소프트 삭제). 삭제 보관함에서 다시 찾아 복원할 수 있다."""
    row = conn.execute(
        "SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없습니다.")
    with db.transaction(conn):
        result = _archive_schedules(conn, [schedule_id])
        db.log(
            conn,
            f"스케줄 삭제(보관함): #{schedule_id} '{row['name']}' "
            f"(실행 기록 {result['deleted_runs']}개 · 발신 작업 {result['deleted_tasks']}건 함께 보관)",
            "warning",
        )
    return {"ok": True, "deleted": schedule_id, **result}


@api.post("/schedules/delete")
def bulk_delete_schedules(payload: ScheduleBulkDelete, conn: sqlite3.Connection = Depends(get_conn)):
    """스케줄 여러 개 삭제(소프트 삭제).

    - ids           : 선택한 스케줄
    - executed_only : 이미 실행된 적이 있고 다시 예약되지 않는 스케줄
                      (실행 기록이 있으면서 status='finished' 이거나 next_run_at 이 없는 것)
    - all_schedules : 등록된 모든 스케줄
    실행 기록(runs)과 발신 작업(tasks)도 함께 보관함으로 옮긴다.
    """
    if payload.all_schedules:
        ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM schedules WHERE deleted_at IS NULL ORDER BY id")
        ]
        label = "전체"
    elif payload.executed_only:
        # 되풀이되어 다시 실행될 스케줄(매일·매주 등 활성/일시정지)은 건드리지 않는다.
        ids = [
            row["id"]
            for row in conn.execute(
                """SELECT s.id FROM schedules s
                   WHERE s.deleted_at IS NULL
                     AND (s.run_count > 0 OR s.last_run_at IS NOT NULL
                          OR EXISTS (SELECT 1 FROM runs r WHERE r.schedule_id = s.id AND r.deleted_at IS NULL))
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
        result = _archive_schedules(conn, ids)
        db.log(
            conn,
            f"스케줄 {label} 삭제(보관함): {result['deleted']}개 "
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
    row = conn.execute("SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)).fetchone()
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
                                     max_call_duration_sec, sms_body, start_at, end_date, recurrence, device_id, target_device_ids,
                                     status, next_run_at, run_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
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
                (_row_value(row, "end_date") or ""),
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


def _archive_runs(conn: sqlite3.Connection, run_ids: list[int]) -> dict:
    """실행 묶음과 그 안의 발신 작업을 소프트 삭제해 보관함으로 옮긴다.

    대기 중이던 작업은 취소(skipped)해 폰이 발신하지 않게 한다.
    """
    ids = [int(item) for item in dict.fromkeys(run_ids or [])]
    result = {"deleted": 0, "deleted_tasks": 0, "canceled_tasks": 0, "deleted_ids": [], "missing_ids": []}
    if not ids:
        return result
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM runs WHERE id IN ({placeholders}) AND deleted_at IS NULL", ids
    ).fetchall()
    found = [row["id"] for row in rows]
    if not found:
        return {**result, "missing_ids": ids}
    found_set = set(found)
    found_placeholders = ",".join("?" for _ in found)
    deleted_at = now_iso()
    canceled = conn.execute(
        f"""UPDATE tasks SET status = 'skipped', device_id = NULL, claimed_at = NULL, finished_at = ?,
                          error = COALESCE(error, '실행 기록 삭제로 취소(삭제 보관함)')
           WHERE deleted_at IS NULL AND status IN ('pending','assigned','running')
             AND run_id IN ({found_placeholders})""",
        [deleted_at] + found,
    ).rowcount
    deleted_tasks = conn.execute(
        f"UPDATE tasks SET deleted_at = ? WHERE run_id IN ({found_placeholders}) AND deleted_at IS NULL",
        [deleted_at] + found,
    ).rowcount
    deleted_runs = conn.execute(
        f"UPDATE runs SET deleted_at = ? WHERE id IN ({found_placeholders}) AND deleted_at IS NULL",
        [deleted_at] + found,
    ).rowcount
    return {
        "deleted": int(deleted_runs or 0),
        "deleted_tasks": int(deleted_tasks or 0),
        "canceled_tasks": int(canceled or 0),
        "deleted_ids": found,
        "missing_ids": [item for item in ids if item not in found_set],
    }


@api.delete("/runs/{run_id}")
def delete_run(run_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """실행 묶음 1개 삭제(소프트 삭제, 그 안의 발신 작업 포함)."""
    row = conn.execute("SELECT * FROM runs WHERE id = ? AND deleted_at IS NULL", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다.")
    with db.transaction(conn):
        result = _archive_runs(conn, [run_id])
        db.log(
            conn,
            f"실행 기록 삭제(보관함): run #{run_id} '{row['schedule_name']}' (발신 작업 {result['deleted_tasks']}건)",
            "warning",
        )
    return {"ok": True, "deleted": result["deleted"], "deleted_tasks": result["deleted_tasks"],
            "canceled_tasks": result["canceled_tasks"], "soft_deleted": True}


@api.post("/runs/delete")
def bulk_delete_runs(payload: RunBulkDelete, conn: sqlite3.Connection = Depends(get_conn)):
    """실행 기록 여러 개 삭제 / 이미 끝난 실행 기록 전체 삭제(all_finished). 소프트 삭제."""
    if payload.all_finished:
        placeholders = ",".join("?" for _ in FINISHED_RUN_STATUSES)
        ids = [
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM runs WHERE deleted_at IS NULL AND status IN ({placeholders}) ORDER BY id",
                list(FINISHED_RUN_STATUSES),
            )
        ]
    else:
        ids = list(payload.ids)
    if not ids:
        raise HTTPException(status_code=404, detail="삭제할 실행 기록이 없습니다.")
    with db.transaction(conn):
        result = _archive_runs(conn, ids)
        if not result["deleted_ids"]:
            raise HTTPException(status_code=404, detail="삭제할 실행 기록이 없습니다.")
        db.log(
            conn,
            f"실행 기록 삭제(보관함): {result['deleted']}개 (발신 작업 {result['deleted_tasks']}건)",
            "warning",
        )
    return {
        "ok": True,
        "deleted": result["deleted"],
        "deleted_tasks": result["deleted_tasks"],
        "canceled_tasks": result["canceled_tasks"],
        "missing_ids": result["missing_ids"],
        "soft_deleted": True,
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
                 AND t.deleted_at IS NULL
                 AND r.deleted_at IS NULL
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
            "UPDATE runs SET status = 'running' WHERE id = ? AND status = 'pending' AND deleted_at IS NULL",
            (row["run_id"],),
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
               FROM tasks WHERE run_id = ? AND deleted_at IS NULL""",
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
                          finished_at = COALESCE(finished_at, ?) WHERE id = ? AND deleted_at IS NULL""",
                (total - skipped, done, failed, iso(now()), run_id),
            )
        elif open_count == 0:
            status = "done" if failed == 0 else ("failed" if done == 0 else "partial")
            conn.execute(
                """UPDATE runs SET status = ?, total_tasks = ?, done_tasks = ?, failed_tasks = ?,
                          finished_at = COALESCE(finished_at, ?) WHERE id = ? AND deleted_at IS NULL""",
                (status, total - skipped, done, failed, iso(now()), run_id),
            )
        else:
            status = "running" if (done + failed) > 0 else "pending"
            conn.execute(
                """UPDATE runs SET status = ?, total_tasks = ?, done_tasks = ?, failed_tasks = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (status, total, done, failed, run_id),
            )

        row = conn.execute("SELECT * FROM runs WHERE id = ? AND deleted_at IS NULL", (run_id,)).fetchone()
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
    """대기열에서 작업을 소프트 삭제(보관함으로 이동)한다.

    - pending/assigned/done/failed/skipped 는 deleted_at 을 채워 목록에서 숨긴다(보관함에서 복원 가능).
    - running(발신 중) 은 폰이 이미 걸고 있어 되돌릴 수 없으므로 취소(skipped)로 표시하고 목록에는 남긴다.
    """
    if all_tasks:
        clauses: list[str] = ["deleted_at IS NULL"]
        values: list = []
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}"
        rows = conn.execute(f"SELECT id, run_id, status FROM tasks{where}", values).fetchall()
        requested: list[int] = []
    elif all_pending:
        clauses = ["deleted_at IS NULL", "status IN ('pending','assigned','running')"]
        values = []
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        rows = conn.execute(
            f"SELECT id, run_id, status FROM tasks WHERE {' AND '.join(clauses)}", values
        ).fetchall()
        requested = []
    else:
        requested = [int(item) for item in dict.fromkeys(task_ids or [])]
        if not requested:
            raise HTTPException(status_code=400, detail="삭제할 작업을 선택하세요.")
        rows = conn.execute(
            f"SELECT id, run_id, status FROM tasks WHERE deleted_at IS NULL AND id IN ({','.join('?' for _ in requested)})",
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
                f"UPDATE tasks SET deleted_at = ? WHERE id IN ({','.join('?' for _ in removed)})",
                [finished] + removed,
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
            detail.append(f"{len(removed)}건 보관함으로 이동")
        if canceled:
            detail.append(f"진행 중 {len(canceled)}건 취소")
        if kept:
            detail.append(f"완료·취소 {len(kept)}건 유지")
        db.log(conn, f"대기열 {label} 삭제(보관함): " + " · ".join(detail), "warning")

    return {
        "ok": True,
        "deleted_count": len(removed),
        "deleted_ids": removed,
        "canceled_count": len(canceled),
        "canceled_ids": canceled,
        "protected_ids": kept,
        "missing_ids": missing,
        "soft_deleted": True,
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
    device_row = ensure_device(conn, device_id)
    device_key = device_row["id"]
    cfg = remote_config(conn, device_key)
    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (iso(now()), device_key))
    # 대시보드에서 '사용 중지'된 기기에는 작업을 배정하지 않는다.
    task = claim_next_task(conn, device_key) if cfg["device_enabled"] else None
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NULL AND status = 'pending' AND (device_id IS NULL OR device_id = ?)",
        (device_key,),
    ).fetchone()["c"]
    return {
        "task": task,
        "pending_tasks": pending,
        "poll_interval_sec": cfg["poll_interval_sec"],
        "server_time": iso(now()),
        # 앱 화면의 기기 이름이 대시보드와 같아지도록 현재 이름을 함께 내려준다.
        "device_name": device_row["name"] or device_key,
        "config": cfg,
    }


@api.post("/tasks/{task_id}/start")
def start_task(task_id: int, payload: dict | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    conn.execute(
        "UPDATE tasks SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
        (iso(now()), task_id),
    )
    return {"ok": True}


@api.post("/tasks/{task_id}/result")
def report_task_result(task_id: int, payload: TaskResult, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    if row["device_id"] and payload.device_id and row["device_id"] != resolve_device_key(conn, payload.device_id):
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
    # 삭제 보관함으로 옮긴 작업은 일반 목록에서 보이지 않는다.
    clauses.append("deleted_at IS NULL")
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
    rows = conn.execute(
        "SELECT * FROM runs WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
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
    device_key = device["id"]
    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (iso(now()), device_key))

    # 수신 번호: 앱이 보낸 값(receiver_number/recipient) → 없으면 기기의 번호(=기기 키).
    receiver_number = (payload.receiver_number or "").strip()
    device_phone = (_row_value(device, "phone_number") or "").strip() or device_key
    if not receiver_number:
        receiver_number = device_phone

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
        (device_key, payload.sender, receiver_number, payload.body, received_at, iso(now())),
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
    return {"ok": True, "id": sms_id, "sms": sms_dict(row, {device_key: device_phone})}


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
    where = " WHERE deleted_at IS NULL"
    values: list = []
    if device_id:
        where += " AND device_id = ?"
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
    row = conn.execute(
        "SELECT * FROM sms_inbox WHERE id = ? AND deleted_at IS NULL", (sms_id,)
    ).fetchone()
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


def _archive_sms_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict:
    """수신 문자를 소프트 삭제해 삭제 보관함으로 옮긴다.

    DB 행을 지우지 않고 deleted_at 만 채우므로, 보관함에서 다시 찾아보거나 복원할 수 있다.
    MMS 사진·첨부 파일은 서버 디스크에 그대로 둔다(보관함에서 계속 볼 수 있음).
    """
    removed_ids: list[int] = []
    files_kept = 0
    for row in rows:
        removed_ids.append(int(row["id"]))
        for item in _sms_attachment_items(row):
            if isinstance(item, dict) and Path(str(item.get("path") or "")).is_file():
                files_kept += 1

    if removed_ids:
        with db.transaction(conn):
            conn.execute(
                f"""UPDATE sms_inbox SET deleted_at = ?
                    WHERE id IN ({','.join('?' for _ in removed_ids)}) AND deleted_at IS NULL""",
                [now_iso()] + removed_ids,
            )
            db.log(
                conn,
                f"수신 문자 삭제(보관함): {len(removed_ids)}건 · 사진·첨부 {files_kept}개는 서버에 보관",
                "warning",
            )

    return {
        "ok": True,
        "deleted_count": len(removed_ids),
        "deleted_ids": removed_ids,
        "files_removed": 0,
        "files_kept": files_kept,
        "soft_deleted": True,
    }


@api.delete("/sms/{sms_id}")
def delete_sms(sms_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """수신 문자 1건 삭제(소프트 삭제). MMS 사진은 서버 보관함에 그대로 남는다."""
    row = conn.execute(
        "SELECT * FROM sms_inbox WHERE id = ? AND deleted_at IS NULL", (sms_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="수신 문자를 찾을 수 없습니다.")
    return _archive_sms_rows(conn, [row])


@api.post("/sms/delete")
def bulk_delete_sms(payload: SmsBulkDelete, conn: sqlite3.Connection = Depends(get_conn)):
    """수신 문자 부분 삭제(ids) / 전체 삭제(all_sms). 소프트 삭제."""
    if payload.all_sms:
        rows = conn.execute("SELECT * FROM sms_inbox WHERE deleted_at IS NULL").fetchall()
        requested: list[int] = []
    else:
        requested = list(payload.ids)
        rows = conn.execute(
            f"""SELECT * FROM sms_inbox
                WHERE deleted_at IS NULL AND id IN ({','.join('?' for _ in requested)})""",
            requested,
        ).fetchall()
    found_ids = {int(row["id"]) for row in rows}
    result = _archive_sms_rows(conn, rows)
    result["missing_ids"] = [sms_id for sms_id in requested if sms_id not in found_ids]
    return result


@api.delete("/sms")
def delete_all_sms(conn: sqlite3.Connection = Depends(get_conn)):
    """수신 문자 전체 삭제(POST /api/sms/delete + all_sms=true 와 같은 동작). 소프트 삭제."""
    rows = conn.execute("SELECT * FROM sms_inbox WHERE deleted_at IS NULL").fetchall()
    return _archive_sms_rows(conn, rows)


# --------------------------------------------------------------------------
# 삭제 보관함 (소프트 삭제된 스케줄·실행 기록·발신 작업·수신 문자)
# --------------------------------------------------------------------------
ARCHIVE_KINDS = ("schedules", "runs", "tasks", "sms")
ARCHIVE_KIND_LABELS = {"schedules": "스케줄", "runs": "실행 기록", "tasks": "발신 작업", "sms": "수신 문자"}
ARCHIVE_TABLES = {"schedules": "schedules", "runs": "runs", "tasks": "tasks", "sms": "sms_inbox"}
ARCHIVE_RESTORE_TABLES = {"schedule": "schedules", "run": "runs", "task": "tasks", "sms": "sms_inbox"}
ARCHIVE_RESTORE_LABELS = {"schedule": "스케줄", "run": "실행 기록", "task": "발신 작업", "sms": "수신 문자"}


def _archive_schedule_items(conn, q, start, end) -> list[dict]:
    clauses = ["deleted_at IS NOT NULL"]
    values: list = []
    search, search_values = _search_clause(["name", "numbers"], q)
    if search:
        clauses.append(search)
        values.extend(search_values)
    time_clauses, time_values = _time_range_clauses("created_at", start, end)
    clauses.extend(time_clauses)
    values.extend(time_values)
    rows = conn.execute(
        f"SELECT * FROM schedules WHERE {' AND '.join(clauses)} ORDER BY deleted_at DESC LIMIT ?",
        values + [ARCHIVE_ALL_LIMIT],
    ).fetchall()
    items = []
    for row in rows:
        numbers = json_list(row["numbers"])
        start_at = parse_dt(row["start_at"])
        items.append(
            {
                "kind": "schedule",
                "kind_label": ARCHIVE_KIND_LABELS["schedules"],
                "id": row["id"],
                "title": row["name"] or f"스케줄 #{row['id']}",
                "type": row["type"],
                "detail": f"번호 {len(numbers)}개 · {row['recurrence']} · 실행 {local_str(row['start_at'], '%Y-%m-%d %H:%M')}",
                "status": row["status"],
                "numbers": numbers,
                "number_count": len(numbers),
                "recurrence": row["recurrence"],
                "start_at": row["start_at"],
                "start_at_text": local_str(row["start_at"], "%Y-%m-%d %H:%M") if start_at else "-",
                "run_count": row["run_count"],
                "created_at": row["created_at"],
                "item_time": row["created_at"],
                "item_time_text": local_str(row["created_at"], "%Y-%m-%d %H:%M"),
                "deleted_at": row["deleted_at"],
                "deleted_at_text": local_str(row["deleted_at"], "%Y-%m-%d %H:%M:%S"),
            }
        )
    return items


def _archive_run_items(conn, q, start, end) -> list[dict]:
    clauses = ["deleted_at IS NOT NULL"]
    values: list = []
    search, search_values = _search_clause(["schedule_name"], q)
    if search:
        clauses.append(search)
        values.extend(search_values)
    time_clauses, time_values = _time_range_clauses("created_at", start, end)
    clauses.extend(time_clauses)
    values.extend(time_values)
    rows = conn.execute(
        f"SELECT * FROM runs WHERE {' AND '.join(clauses)} ORDER BY deleted_at DESC LIMIT ?",
        values + [ARCHIVE_ALL_LIMIT],
    ).fetchall()
    return [
        {
            "kind": "run",
            "kind_label": ARCHIVE_KIND_LABELS["runs"],
            "id": row["id"],
            "title": row["schedule_name"] or f"실행 묶음 #{row['id']}",
            "type": row["type"],
            "detail": f"예정 {local_str(row['scheduled_for'], '%Y-%m-%d %H:%M')} · 완료 {row['done_tasks']}/{row['total_tasks']} · 실패 {row['failed_tasks']}",
            "status": row["status"],
            "schedule_id": row["schedule_id"],
            "total_tasks": row["total_tasks"],
            "done_tasks": row["done_tasks"],
            "failed_tasks": row["failed_tasks"],
            "created_at": row["created_at"],
            "item_time": row["created_at"],
            "item_time_text": local_str(row["created_at"], "%Y-%m-%d %H:%M"),
            "deleted_at": row["deleted_at"],
            "deleted_at_text": local_str(row["deleted_at"], "%Y-%m-%d %H:%M:%S"),
        }
        for row in rows
    ]


def _archive_task_items(conn, q, start, end) -> list[dict]:
    clauses = ["deleted_at IS NOT NULL"]
    values: list = []
    search, search_values = _search_clause(["schedule_name", "phone_number", "result", "error"], q)
    if search:
        clauses.append(search)
        values.extend(search_values)
    time_clauses, time_values = _time_range_clauses("created_at", start, end)
    clauses.extend(time_clauses)
    values.extend(time_values)
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY deleted_at DESC LIMIT ?",
        values + [ARCHIVE_ALL_LIMIT],
    ).fetchall()
    items = []
    for row in rows:
        note = row["error"] or row["result"] or ""
        items.append(
            {
                "kind": "task",
                "kind_label": ARCHIVE_KIND_LABELS["tasks"],
                "id": row["id"],
                "title": row["phone_number"],
                "type": row["type"],
                "detail": f"{row['schedule_name'] or '-'} · {row['seq']}번째 · {note[:80]}",
                "status": row["status"],
                "phone_number": row["phone_number"],
                "device_id": row["device_id"],
                "schedule_name": row["schedule_name"],
                "result": row["result"],
                "error": row["error"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "item_time": row["created_at"],
                "item_time_text": local_str(row["created_at"], "%Y-%m-%d %H:%M"),
                "deleted_at": row["deleted_at"],
                "deleted_at_text": local_str(row["deleted_at"], "%Y-%m-%d %H:%M:%S"),
            }
        )
    return items


def _archive_sms_items(conn, q, start, end) -> list[dict]:
    clauses = ["deleted_at IS NOT NULL"]
    values: list = []
    search, search_values = _search_clause(["sender", "body", "receiver_number"], q)
    if search:
        clauses.append(search)
        values.extend(search_values)
    time_clauses, time_values = _time_range_clauses("COALESCE(received_at, created_at)", start, end)
    clauses.extend(time_clauses)
    values.extend(time_values)
    rows = conn.execute(
        f"SELECT * FROM sms_inbox WHERE {' AND '.join(clauses)} ORDER BY deleted_at DESC LIMIT ?",
        values + [ARCHIVE_ALL_LIMIT],
    ).fetchall()
    names = {row["id"]: (row["name"] or row["id"]) for row in conn.execute("SELECT id, name FROM devices")}
    phones = {row["id"]: (_row_value(row, "phone_number") or "") for row in conn.execute("SELECT * FROM devices")}
    items = []
    for row in rows:
        item = sms_dict(row, phones)
        device_name = names.get(row["device_id"], row["device_id"])
        preview = " ".join((row["body"] or "").split())[:120]
        items.append(
            {
                "kind": "sms",
                "kind_label": ARCHIVE_KIND_LABELS["sms"],
                "id": row["id"],
                "title": f"{row['sender'] or '발신번호 없음'} → {item['receiver_number'] or '-'}",
                "detail": preview or ("📷 MMS 사진" if item["attachments"] else "(내용 없음)"),
                "sender": row["sender"],
                "receiver_number": item["receiver_number"],
                "body": row["body"],
                "device_id": row["device_id"],
                "device_name": device_name,
                "telegram_status": row["telegram_status"],
                "attachment_count": item["attachment_count"],
                "attachments": item["attachments"],
                "has_mms": item["has_mms"],
                "item_time": row["received_at"] or row["created_at"],
                "item_time_text": local_str(row["received_at"] or row["created_at"], "%Y-%m-%d %H:%M:%S"),
                "deleted_at": row["deleted_at"],
                "deleted_at_text": local_str(row["deleted_at"], "%Y-%m-%d %H:%M:%S"),
            }
        )
    return items


ARCHIVE_BUILDERS = {
    "schedules": _archive_schedule_items,
    "runs": _archive_run_items,
    "tasks": _archive_task_items,
    "sms": _archive_sms_items,
}


@api.get("/archive")
def list_archive(
    kind: str = Query(default="all"),
    q: Optional[str] = Query(default=None, max_length=200),
    start: Optional[str] = Query(default=None, max_length=40),
    end: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """삭제 보관함 목록. 웹에서 삭제한 스케줄·실행 기록·발신 작업·수신 문자(MMS 사진 포함)를 보여 준다."""
    if kind not in ("all", *ARCHIVE_KINDS):
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 종류입니다: {kind} (all, {', '.join(ARCHIVE_KINDS)} 중 하나)",
        )

    counts = {key: int(conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE deleted_at IS NOT NULL").fetchone()["c"] or 0)
              for key, table in ARCHIVE_TABLES.items()}
    counts["total"] = sum(counts.values())

    targets = ARCHIVE_KINDS if kind == "all" else (kind,)
    items: list[dict] = []
    filtered_total = 0
    for key in targets:
        found = ARCHIVE_BUILDERS[key](conn, q, start, end)
        filtered_total += len(found)
        items.extend(found)
    items.sort(key=lambda item: (item.get("deleted_at") or "", item["kind"], item["id"]), reverse=True)
    page = items[offset : offset + limit]
    return {
        "items": page,
        "count": len(page),
        "total": filtered_total,
        "counts": counts,
        "kind": kind,
        "kinds": ARCHIVE_KIND_LABELS,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < filtered_total,
    }


@api.post("/archive/restore")
def restore_archive(payload: ArchiveRestore, conn: sqlite3.Connection = Depends(get_conn)):
    """삭제 보관함에서 항목을 복원한다.

    스케줄은 일시정지 상태로 돌아와 복원하자마자 저절로 발신되지 않는다.
    함께 삭제된 실행 기록·발신 작업(같은 삭제 시점)도 같이 되살린다.
    """
    table = ARCHIVE_RESTORE_TABLES[payload.kind]
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id = ? AND deleted_at IS NOT NULL", (payload.id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="삭제 보관함에서 항목을 찾을 수 없습니다.")
    deleted_at = row["deleted_at"]
    restored_runs = 0
    restored_tasks = 0
    with db.transaction(conn):
        if payload.kind == "schedule":
            conn.execute(
                "UPDATE schedules SET deleted_at = NULL, status = 'paused' WHERE id = ?", (payload.id,)
            )
            restored_runs = conn.execute(
                "UPDATE runs SET deleted_at = NULL WHERE schedule_id = ? AND deleted_at = ?",
                (payload.id, deleted_at),
            ).rowcount
            restored_tasks = conn.execute(
                "UPDATE tasks SET deleted_at = NULL WHERE schedule_id = ? AND deleted_at = ?",
                (payload.id, deleted_at),
            ).rowcount
        elif payload.kind == "run":
            conn.execute("UPDATE runs SET deleted_at = NULL WHERE id = ?", (payload.id,))
            restored_tasks = conn.execute(
                "UPDATE tasks SET deleted_at = NULL WHERE run_id = ? AND deleted_at = ?",
                (payload.id, deleted_at),
            ).rowcount
        else:
            conn.execute(f"UPDATE {table} SET deleted_at = NULL WHERE id = ?", (payload.id,))
        db.log(
            conn,
            f"삭제 보관함 복원: {ARCHIVE_RESTORE_LABELS[payload.kind]} #{payload.id}"
            + (f" (실행 기록 {restored_runs}개 · 발신 작업 {restored_tasks}건)" if restored_runs or restored_tasks else ""),
        )
    return {
        "ok": True,
        "kind": payload.kind,
        "id": payload.id,
        "restored": 1,
        "restored_runs": restored_runs,
        "restored_tasks": restored_tasks,
    }


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
def list_logs(
    q: Optional[str] = Query(default=None, max_length=200),
    level: Optional[str] = Query(default=None, max_length=20),
    category: Optional[str] = Query(default=None, max_length=20),
    start: Optional[str] = Query(default=None, max_length=40),
    end: Optional[str] = Query(default=None, max_length=40),
    all: bool = False,
    limit: int = Query(default=200, ge=0, le=5000),
    offset: int = Query(default=0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """서버 활동 로그. 검색어·분류·레벨·시간 범위로 걸러 보고 전체/나눠서 볼 수 있다.

    - q        : 메시지·분류·레벨에서 대소문자 구분 없이 부분 일치 검색
    - level    : info / warning / error
    - category : device / schedule / run / queue / sms / telegram / settings / system
    - start/end: ISO8601 · KST · 날짜만(종료는 그날 23:59:59까지)
    - all=true 또는 limit=0 : 조건에 맞는 로그를 한 번에(안전 상한 LOG_ALL_LIMIT)
    """
    if category and category not in db.LOG_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 분류입니다: {category} ({', '.join(db.LOG_CATEGORIES)} 중 하나)",
        )

    clauses: list[str] = []
    values: list = []
    search, search_values = _search_clause(["message", "category", "level"], q)
    if search:
        clauses.append(search)
        values.extend(search_values)
    if level:
        clauses.append("LOWER(level) = ?")
        values.append(level.strip().lower())
    if category:
        clauses.append("category = ?")
        values.append(category)
    time_clauses, time_values = _time_range_clauses("created_at", start, end)
    clauses.extend(time_clauses)
    values.extend(time_values)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    total = int(conn.execute(f"SELECT COUNT(*) AS c FROM logs{where}", values).fetchone()["c"] or 0)
    capped = bool(all or limit == 0)
    truncated = False
    if capped:
        rows = conn.execute(
            f"SELECT * FROM logs{where} ORDER BY id DESC LIMIT ? OFFSET ?", values + [LOG_ALL_LIMIT, offset]
        ).fetchall()
        truncated = total - offset > len(rows)
    else:
        rows = conn.execute(
            f"SELECT * FROM logs{where} ORDER BY id DESC LIMIT ? OFFSET ?", values + [limit, offset]
        ).fetchall()

    items = [
        {
            "id": row["id"],
            "level": row["level"],
            "category": _row_value(row, "category", "") or "",
            "category_label": db.LOG_CATEGORIES.get(_row_value(row, "category", "") or "", "기타"),
            "message": row["message"],
            "created_at": row["created_at"],
            "created_at_text": local_str(row["created_at"], "%m-%d %H:%M:%S"),
            "created_at_full": local_str(row["created_at"], "%Y-%m-%d %H:%M:%S"),
        }
        for row in rows
    ]
    return {
        "logs": items,
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": None if capped else limit,
        "all": capped,
        "has_more": offset + len(items) < total,
        "truncated": truncated,
        "categories": db.LOG_CATEGORIES,
        "server_time": iso(now()),
    }


@api.post("/scheduler/tick")
def manual_tick(conn: sqlite3.Connection = Depends(get_conn)):
    """스케줄러를 즉시 한 번 돌린다(운영/점검용)."""
    return {"ok": True, **scheduler.tick(conn)}


# --------------------------------------------------------------------------
# 앱 APK 배포(자동 터치 등): /data/apks/{app_id}/latest.json + APK 파일
#
# · 조회(latest/download)는 인증 없이 공개한다 — 폰 앱이 업데이트를 확인하려면
#   키 없이도 받아야 하고, APK 자체는 비밀 정보가 아니다.
# · 업로드(upload)는 위 api 라우터에 붙여 SERVER_API_KEY 가 설정된 경우 그 키를 요구한다.
# --------------------------------------------------------------------------
APK_MEDIA_TYPE = "application/vnd.android.package-archive"
APK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
APK_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z._-]{1,32}$")

apps = APIRouter(prefix="/api/apps")


def _checked_app_id(app_id: str) -> str:
    """앱 ID 를 검사해 돌려준다(경로 탈출·잘못된 값 차단)."""
    cleaned = (app_id or "").strip()
    if not APK_ID_PATTERN.match(cleaned):
        raise HTTPException(status_code=404, detail="앱 ID가 올바르지 않습니다.")
    return cleaned


def _app_apk_dir(app_id: str) -> Path:
    """앱 ID 를 검사하고 그 앱의 APK 폴더 경로를 돌려준다(경로 탈출 차단)."""
    return Path(config.APK_DIR) / _checked_app_id(app_id)


def _read_apk_latest(app_id: str) -> Optional[dict]:
    meta_path = _app_apk_dir(app_id) / "latest.json"
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _public_base_url(request: Optional[Request]) -> str:
    configured = (getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if configured:
        return configured
    if request is None:
        return ""
    return str(request.base_url).rstrip("/")


def _app_apk_meta(app_id: str, meta: dict, request: Optional[Request]) -> dict:
    """latest.json 의 내부 정보를 앱이 쓰는 응답 형식으로 바꾼다."""
    base = _public_base_url(request)
    return {
        "app_id": app_id,
        "version_code": int(meta.get("version_code") or 0),
        "version_name": str(meta.get("version_name") or ""),
        "download_url": f"{base}/api/apps/{app_id}/download",
        "sha256": str(meta.get("sha256") or ""),
        "size_bytes": int(meta.get("size_bytes") or 0),
        "release_notes": str(meta.get("release_notes") or ""),
        "updated_at": str(meta.get("updated_at") or ""),
        "file_name": str(meta.get("file_name") or ""),
    }


@apps.get("/{app_id}/latest")
def app_apk_latest(app_id: str, request: Request):
    """앱 업데이트 확인용 최신 버전 정보."""
    meta = _read_apk_latest(app_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="등록된 APK가 없습니다.")
    return _app_apk_meta(app_id.strip(), meta, request)


# --------------------------------------------------------------------------
# 앱별 최신 APK 목록: 폰 브라우저(/apps)와 대시보드 '앱 다운로드' 탭이 함께 쓴다
# --------------------------------------------------------------------------
# 앱 표시 이름·설명(모르는 앱은 app_id 를 그대로 보여 준다)
APP_DISPLAY_NAMES = {
    "auto-dialer": "자동 발신",
    "auto-touch": "자동 터치",
}
APP_DESCRIPTIONS = {
    "auto-dialer": "서버가 예약한 전화·문자를 이 폰에서 자동으로 실행합니다.",
    "auto-touch": "화면 터치를 기록해 반복 실행하는 매크로 앱입니다.",
}
# 목록에서 먼저 보여 줄 순서(그 밖의 앱은 app_id 알파벳순으로 뒤에 붙는다)
APP_LIST_ORDER = ("auto-dialer", "auto-touch")


def _app_apk_available(folder: Path, meta: dict) -> bool:
    """latest.json 이 가리키는 APK 파일이 실제로 있는지(경로 탈출 포함 검사)."""
    file_name = str(meta.get("file_name") or "").strip()
    if not file_name or "/" in file_name or "\\" in file_name or ".." in file_name:
        return False
    try:
        return (folder / file_name).is_file()
    except OSError:
        return False


def _app_sort_key(app_id: str) -> tuple:
    if app_id in APP_LIST_ORDER:
        return (0, APP_LIST_ORDER.index(app_id), app_id)
    return (1, 0, app_id)


def _list_app_apks(request: Optional[Request]) -> list[dict]:
    """서버에 APK 를 올린 앱들의 최신 버전 정보를 돌려준다."""
    root = Path(config.APK_DIR)
    items: list[dict] = []
    try:
        folders = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return items
    base = _public_base_url(request)
    for folder in folders:
        app_id = folder.name
        if not APK_ID_PATTERN.match(app_id):
            continue
        meta = _read_apk_latest(app_id)
        if not meta:
            continue
        updated_at = str(meta.get("updated_at") or "")
        size_bytes = int(meta.get("size_bytes") or 0)
        items.append(
            {
                "app_id": app_id,
                "display_name": APP_DISPLAY_NAMES.get(app_id, app_id),
                "description": APP_DESCRIPTIONS.get(app_id, ""),
                "version_code": int(meta.get("version_code") or 0),
                "version_name": str(meta.get("version_name") or ""),
                "file_name": str(meta.get("file_name") or ""),
                "sha256": str(meta.get("sha256") or ""),
                "size_bytes": size_bytes,
                "size_text": apps_page.format_size(size_bytes),
                "updated_at": updated_at,
                "updated_at_text": local_str(updated_at, "%Y-%m-%d %H:%M") if updated_at else "",
                "release_notes": str(meta.get("release_notes") or ""),
                "download_url": f"{base}/api/apps/{app_id}/download",
                "available": _app_apk_available(folder, meta),
            }
        )
    items.sort(key=lambda item: _app_sort_key(item["app_id"]))
    return items


@apps.get("")
@apps.get("/")
def app_apk_list(request: Request):
    """서버에 올린 앱별 최신 APK 목록(폰 브라우저·대시보드 공용)."""
    items = _list_app_apks(request)
    return {"ok": True, "count": len(items), "apps": items}


@apps.get("/{app_id}/download")
def app_apk_download(app_id: str):
    """최신 APK 파일 스트리밍."""
    meta = _read_apk_latest(app_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="등록된 APK가 없습니다.")
    file_name = str(meta.get("file_name") or "")
    if not file_name or "/" in file_name or "\\" in file_name or ".." in file_name:
        raise HTTPException(status_code=404, detail="APK 파일 정보가 올바르지 않습니다.")
    path = _app_apk_dir(app_id) / file_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="APK 파일이 서버에 없습니다.")
    version_name = str(meta.get("version_name") or "latest").strip() or "latest"
    return FileResponse(
        path,
        media_type=APK_MEDIA_TYPE,
        filename=f"{app_id.strip()}-{version_name}.apk",
        headers={
            "Cache-Control": "no-store",
            "X-APK-SHA256": str(meta.get("sha256") or ""),
            "X-APK-Version-Code": str(meta.get("version_code") or ""),
        },
    )


@api.post("/apps/{app_id}/upload")
async def upload_app_apk(
    request: Request,
    app_id: str,
    file: UploadFile = File(...),
    version_code: int = Form(...),
    version_name: str = Form(...),
    release_notes: str = Form(""),
    force: bool = Form(False),
):
    """새 버전 APK 를 올리고 latest.json 을 갱신한다(multipart)."""
    target_dir = _app_apk_dir(app_id)
    app_id = app_id.strip()
    if version_code < 1:
        raise HTTPException(status_code=400, detail="version_code 는 1 이상이어야 합니다.")
    version_name = (version_name or "").strip()
    if not APK_VERSION_PATTERN.match(version_name):
        raise HTTPException(status_code=400, detail="version_name 이 올바르지 않습니다(영문·숫자·._- 32자 이내).")
    upload_name = (file.filename or "").strip()
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if not upload_name.lower().endswith(".apk") and content_type not in (APK_MEDIA_TYPE, "application/octet-stream", ""):
        raise HTTPException(status_code=400, detail="APK 파일만 올릴 수 있습니다.")

    current = _read_apk_latest(app_id)
    if current and not force:
        current_code = int(current.get("version_code") or 0)
        if version_code < current_code:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"이미 더 높은 버전({current.get('version_name')}, code {current_code})이 있습니다. "
                    "강제로 올리려면 force=true 로 보내세요."
                ),
            )

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("APK 폴더를 만들지 못했습니다: %s", exc)
        raise HTTPException(status_code=500, detail="APK 저장 폴더를 만들지 못했습니다.")

    file_name = f"{app_id}-{version_name}.apk"
    dest = target_dir / file_name
    part = target_dir / f"{file_name}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with part.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_APK_BYTES:
                    raise ValueError("too large")
                digest.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise ValueError("empty")
        part.replace(dest)
    except ValueError as exc:
        part.unlink(missing_ok=True)
        if str(exc) == "too large":
            raise HTTPException(status_code=413, detail="APK 파일이 너무 큽니다.")
        raise HTTPException(status_code=400, detail="빈 파일은 올릴 수 없습니다.")
    except OSError as exc:
        part.unlink(missing_ok=True)
        logger.error("APK 저장 실패: %s", exc)
        raise HTTPException(status_code=500, detail="APK 파일을 저장하지 못했습니다.")
    finally:
        await file.close()

    meta = {
        "app_id": app_id,
        "version_code": version_code,
        "version_name": version_name,
        "file_name": file_name,
        "sha256": digest.hexdigest(),
        "size_bytes": size,
        "release_notes": (release_notes or "").strip()[:2000],
        "updated_at": now_iso(),
    }
    meta_path = target_dir / "latest.json"
    tmp_meta = target_dir / "latest.json.part"
    try:
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_meta.replace(meta_path)
    except OSError as exc:
        logger.error("latest.json 저장 실패: %s", exc)
        raise HTTPException(status_code=500, detail="버전 정보를 저장하지 못했습니다.")

    logger.info("APK 업로드: %s v%s (code %s, %s bytes)", app_id, version_name, version_code, size)
    return {"ok": True, "app": _app_apk_meta(app_id, meta, request)}


# --------------------------------------------------------------------------
# '자동 터치' 매크로 백업: /data/macro_backups/auto-touch/backups.json
#
# · 폰 앱이 키 없이 저장·불러오기·삭제를 하므로 공개한다(자동 발신 DB 와 완전히 분리된 파일).
# --------------------------------------------------------------------------
@apps.get("/{app_id}/macros")
def app_macros_list(app_id: str):
    """저장된 매크로 백업 목록(최신순, 매크로 본문 제외)."""
    cleaned = _checked_app_id(app_id)
    backups = macro_store.list_backups(cleaned)
    return {"ok": True, "app_id": cleaned, "count": len(backups), "backups": backups}


@apps.post("/{app_id}/macros")
def app_macros_save(app_id: str, payload: MacroBackupIn):
    """매크로 묶음을 저장한다(같은 이름이면 덮어쓰기)."""
    cleaned = _checked_app_id(app_id)
    macros = payload.macros or []
    if len(macros) > macro_store.MAX_MACROS:
        raise HTTPException(
            status_code=413,
            detail=f"한 번에 저장할 수 있는 매크로는 {macro_store.MAX_MACROS}개까지입니다.",
        )
    steps = macro_store.count_steps([m for m in macros if isinstance(m, dict)])
    if steps > macro_store.MAX_STEPS:
        raise HTTPException(
            status_code=413,
            detail=f"한 번에 저장할 수 있는 동작은 {macro_store.MAX_STEPS}개까지입니다.",
        )
    folders = payload.folders or []
    if len(folders) > macro_store.MAX_FOLDERS:
        raise HTTPException(
            status_code=413,
            detail=f"한 번에 저장할 수 있는 폴더는 {macro_store.MAX_FOLDERS}개까지입니다.",
        )
    try:
        meta, created = macro_store.save_backup(
            cleaned,
            payload.name,
            payload.device_name,
            [m for m in macros if isinstance(m, dict)],
            [f for f in folders if isinstance(f, dict)],
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except OSError as exc:
        logger.error("매크로 백업 저장 실패: %s", exc)
        raise HTTPException(status_code=500, detail="매크로 백업을 저장하지 못했습니다.")
    logger.info("매크로 백업 저장: %s '%s' %s개", cleaned, meta["name"], meta["macro_count"])
    return {"ok": True, "app_id": cleaned, "created": created, "backup": meta}


@apps.get("/{app_id}/macros/{backup_id}")
def app_macros_detail(app_id: str, backup_id: str):
    """백업 하나의 전체 매크로 JSON."""
    cleaned = _checked_app_id(app_id)
    if not macro_store.BACKUP_ID_RE.match((backup_id or "").strip()):
        raise HTTPException(status_code=404, detail="백업 ID가 올바르지 않습니다.")
    backup = macro_store.get_backup(cleaned, backup_id.strip())
    if backup is None:
        raise HTTPException(status_code=404, detail="저장된 백업이 없습니다.")
    return {"ok": True, "app_id": cleaned, "backup": backup}


@apps.delete("/{app_id}/macros/{backup_id}")
def app_macros_delete(app_id: str, backup_id: str):
    """백업 삭제."""
    cleaned = _checked_app_id(app_id)
    if not macro_store.BACKUP_ID_RE.match((backup_id or "").strip()):
        raise HTTPException(status_code=404, detail="백업 ID가 올바르지 않습니다.")
    if not macro_store.delete_backup(cleaned, backup_id.strip()):
        raise HTTPException(status_code=404, detail="저장된 백업이 없습니다.")
    logger.info("매크로 백업 삭제: %s %s", cleaned, backup_id)
    return {"ok": True, "app_id": cleaned, "deleted": backup_id.strip()}


app.include_router(apps)
app.include_router(api)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
