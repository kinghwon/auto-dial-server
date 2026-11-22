"""발신 스케줄 확장과 백그라운드 스케줄러."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Iterable

from . import config, db
from .timeutil import iso, next_occurrence, now, parse_dt

logger = logging.getLogger(__name__)

_scheduler = None


# --------------------------------------------------------------------------
# 작업 확장
# --------------------------------------------------------------------------
def expand_tasks(
    numbers: Iterable[str],
    repeat_count: int,
    order_mode: str = "sequence",
) -> list[tuple[str, int]]:
    """(번호, 회차) 목록을 만든다.

    order_mode:
      - sequence : 번호 순서대로 한 바퀴씩 반복  → A,B,C,A,B,C
      - grouped  : 번호마다 연속 반복            → A,A,A,B,B,B
    """
    cleaned = [str(n).strip() for n in numbers if str(n).strip()]
    repeat = max(1, int(repeat_count or 1))
    if not cleaned:
        return []

    result: list[tuple[str, int]] = []
    if order_mode == "grouped":
        for number in cleaned:
            for index in range(1, repeat + 1):
                result.append((number, index))
    else:
        for index in range(1, repeat + 1):
            for number in cleaned:
                result.append((number, index))
    return result


def create_run(conn, schedule_row, scheduled_for: datetime, source: str = "scheduled") -> int | None:
    """스케줄 하나를 실행(run)으로 확장해 tasks 를 만든다."""
    numbers = db.json_list(schedule_row["numbers"])
    pairs = expand_tasks(numbers, schedule_row["repeat_count"], schedule_row["order_mode"] or "sequence")
    if not pairs:
        db.log(conn, f"스케줄 #{schedule_row['id']} 에 발신할 번호가 없습니다.", "warning")
        return None

    created = iso(now())
    with db.transaction(conn):
        cursor = conn.execute(
            """INSERT INTO runs(schedule_id, schedule_name, type, scheduled_for, status, total_tasks, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (
                schedule_row["id"],
                schedule_row["name"] or f"스케줄 {schedule_row['id']}",
                schedule_row["type"],
                iso(scheduled_for),
                len(pairs),
                created,
            ),
        )
        run_id = int(cursor.lastrowid)

        task_rows = []
        for seq, (number, repeat_index) in enumerate(pairs, start=1):
            task_rows.append(
                (
                    run_id,
                    schedule_row["id"],
                    schedule_row["name"] or f"스케줄 {schedule_row['id']}",
                    schedule_row["type"],
                    number,
                    seq,
                    repeat_index,
                    schedule_row["max_call_duration_sec"],
                    schedule_row["sms_body"],
                    schedule_row["device_id"] or None,
                    created,
                )
            )
        conn.executemany(
            """INSERT INTO tasks(run_id, schedule_id, schedule_name, type, phone_number, seq, repeat_index,
                                 max_call_duration_sec, sms_body, device_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            task_rows,
        )
        conn.execute(
            "UPDATE schedules SET run_count = run_count + 1, last_run_at = ? WHERE id = ?",
            (created, schedule_row["id"]),
        )
        db.log(
            conn,
            f"스케줄 '{schedule_row['name']}' 실행 생성: {len(pairs)}건 ({source})",
        )
    return run_id


def run_schedule_now(conn, schedule_id: int) -> int | None:
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        return None
    return create_run(conn, row, now(), source="manual")


def _advance_next_run(schedule_row, current: datetime) -> datetime | None:
    """만기 처리 후의 다음 실행 시각을 계산한다(서버가 꺼져 있던 시간은 건너뛴다)."""
    recurrence = schedule_row["recurrence"] or "once"
    next_at = parse_dt(schedule_row["next_run_at"]) or parse_dt(schedule_row["start_at"]) or current
    if recurrence == "once":
        return None
    guard = 0
    candidate = next_occurrence(next_at, recurrence)
    while candidate is not None and candidate <= current and guard < 1000:
        candidate = next_occurrence(candidate, recurrence)
        guard += 1
    return candidate


def run_due_schedules(conn, current: datetime | None = None) -> list[int]:
    """next_run_at 이 지난 활성 스케줄을 모두 실행한다. 생성된 run id 목록 반환."""
    current = current or now()
    rows = conn.execute(
        """SELECT * FROM schedules
           WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?
           ORDER BY next_run_at ASC""",
        (iso(current),),
    ).fetchall()

    created_runs: list[int] = []
    for row in rows:
        run_id = create_run(conn, row, parse_dt(row["next_run_at"]) or current)
        next_at = _advance_next_run(row, current)
        status = "finished" if next_at is None else "active"
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, status = ? WHERE id = ?",
            (iso(next_at), status, row["id"]),
        )
        if run_id:
            created_runs.append(run_id)
    return created_runs


def reap_stale_tasks(conn, current: datetime | None = None) -> int:
    """응답이 끊긴(가져간 뒤 오래된) 작업을 대기열로 되돌리거나 실패 처리한다."""
    current = current or now()
    limit = iso(current - timedelta(seconds=config.TASK_TIMEOUT_SEC))
    rows = conn.execute(
        """SELECT * FROM tasks
           WHERE status IN ('assigned', 'running') AND claimed_at IS NOT NULL AND claimed_at <= ?""",
        (limit,),
    ).fetchall()
    recovered = 0
    for row in rows:
        if (row["attempts"] or 0) >= 2:
            conn.execute(
                """UPDATE tasks SET status = 'failed', finished_at = ?,
                          error = COALESCE(error, '기기 응답 시간 초과') WHERE id = ?""",
                (iso(current), row["id"]),
            )
            _refresh_run(conn, row["run_id"])
        else:
            conn.execute(
                """UPDATE tasks SET status = 'pending', device_id = CASE WHEN device_id = ? THEN NULL ELSE device_id END,
                          claimed_at = NULL, attempts = attempts + 1 WHERE id = ?""",
                (row["device_id"], row["id"]),
            )
            recovered += 1
    if rows:
        db.log(conn, f"응답 없는 작업 {len(rows)}건 정리(복구 {recovered}건)", "warning")
    return len(rows)


def _refresh_run(conn, run_id: int) -> None:
    counts = conn.execute(
        """SELECT
             SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
             SUM(CASE WHEN status IN ('pending', 'assigned', 'running') THEN 1 ELSE 0 END) AS open,
             COUNT(*) AS total
           FROM tasks WHERE run_id = ?""",
        (run_id,),
    ).fetchone()
    total = counts["total"] or 0
    done = counts["done"] or 0
    failed = counts["failed"] or 0
    open_count = counts["open"] or 0

    if open_count == 0:
        if failed == 0:
            status = "done"
        elif done == 0:
            status = "failed"
        else:
            status = "partial"
        conn.execute(
            "UPDATE runs SET status = ?, done_tasks = ?, failed_tasks = ?, finished_at = COALESCE(finished_at, ?) WHERE id = ?",
            (status, done, failed, iso(now()), run_id),
        )
    else:
        status = "running" if (done + failed) > 0 else "pending"
        conn.execute(
            "UPDATE runs SET status = ?, done_tasks = ?, failed_tasks = ?, total_tasks = ? WHERE id = ?",
            (status, done, failed, total, run_id),
        )


def refresh_run(conn, run_id: int) -> None:
    _refresh_run(conn, run_id)


def retry_pending_telegram(conn, limit: int = 10) -> int:
    """전송 실패한 수신 문자를 재시도한다."""
    from .telegram_client import deliver_sms_notification

    rows = conn.execute(
        "SELECT id FROM sms_inbox WHERE telegram_status = 'pending' ORDER BY id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    sent = 0
    for row in rows:
        ok, _ = deliver_sms_notification(conn, int(row["id"]))
        if ok:
            sent += 1
    return sent


def tick(conn, current: datetime | None = None) -> dict:
    """스케줄러 1회 주기: 만기 실행 → 응답 없는 작업 회수 → 텔레그램 재시도."""
    current = current or now()
    created = run_due_schedules(conn, current)
    reaped = reap_stale_tasks(conn, current)
    retried = retry_pending_telegram(conn)
    return {"runs_created": len(created), "tasks_reaped": reaped, "telegram_retried": retried}


# --------------------------------------------------------------------------
# APScheduler 연결
# --------------------------------------------------------------------------
def _job() -> None:
    conn = db.connect()
    try:
        result = tick(conn)
        if any(result.values()):
            logger.info("scheduler tick: %s", result)
    except Exception:  # noqa: BLE001
        logger.exception("스케줄러 실행 중 오류")
    finally:
        conn.close()


def start_scheduler(interval_sec: int | None = None):
    """FastAPI 시작 시 호출. 이미 떠 있으면 그대로 반환한다."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BackgroundScheduler(timezone=str(config.TZ))
    scheduler.add_job(
        _job,
        trigger=IntervalTrigger(seconds=interval_sec or config.SCHEDULER_INTERVAL_SEC),
        id="auto-dialer-tick",
        max_instances=1,
        coalesce=True,
        next_run_time=now(),
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("스케줄러 시작 (%s초 주기)", interval_sec or config.SCHEDULER_INTERVAL_SEC)
    return scheduler


def is_running() -> bool:
    return _scheduler is not None and _scheduler.running


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
