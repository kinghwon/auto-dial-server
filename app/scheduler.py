"""발신 스케줄 확장과 백그라운드 스케줄러."""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta
from typing import Iterable

from . import config, db
from .timeutil import end_date_deadline, iso, next_occurrence, now, parse_dt

logger = logging.getLogger(__name__)

_scheduler = None


# --------------------------------------------------------------------------
# 작업 확장
# --------------------------------------------------------------------------
def expand_tasks(
    numbers: Iterable[str],
    repeat_count: int = 1,
    order_mode: str = "sequence",
    total_calls: int | None = None,
) -> list[tuple[str, int]]:
    """(번호, 회차) 목록을 만든다.

    total_calls 가 지정되면 번호 개수 × repeat_count 대신 정확히 total_calls 건을 만든다.

    order_mode:
      - sequence : 번호 순서대로 한 바퀴씩 반복  → A,B,C,A,B,C
      - random   : 한 바퀴(라운드)마다 번호를 무작위로 섞어서 발신
      - grouped  : 번호마다 연속 반복            → A,A,A,B,B,B
    """
    cleaned = [str(n).strip() for n in numbers if str(n).strip()]
    repeat = max(1, int(repeat_count or 1))
    if not cleaned:
        return []

    per_round = len(cleaned)
    try:
        target = int(total_calls) if total_calls is not None else per_round * repeat
    except (TypeError, ValueError):
        target = per_round * repeat
    target = max(1, min(target, config.MAX_TOTAL_CALLS))

    if order_mode == "grouped":
        base: list[tuple[str, int]] = [(number, index) for number in cleaned for index in range(1, repeat + 1)]
        if target <= len(base):
            return base[:target]
        result: list[tuple[str, int]] = []
        round_no = 0
        while len(result) < target:
            for number, index in base:
                if len(result) >= target:
                    break
                result.append((number, round_no * repeat + index))
            round_no += 1
        return result

    if order_mode == "random":
        pool: list[str] = []
        previous: str | None = None
        result = []
        while len(result) < target:
            if not pool:
                pool = list(cleaned)
                random.shuffle(pool)
                # 라운드 경계에서 바로 앞 번호가 또 나오지 않도록 자리를 바꾼다.
                if previous is not None and len(pool) > 1 and pool[0] == previous:
                    swap = random.randrange(1, len(pool))
                    pool[0], pool[swap] = pool[swap], pool[0]
            number = pool.pop(0)
            result.append(number)
            previous = number
        return [(number, index // per_round + 1) for index, number in enumerate(result)]

    # sequence (기본): 번호 목록을 처음부터 다시 돌면서 채운다.
    return [(cleaned[index % per_round], index // per_round + 1) for index in range(target)]


def create_run(conn, schedule_row, scheduled_for: datetime, source: str = "scheduled") -> int | None:
    """스케줄 하나를 실행(run)으로 확장해 tasks 를 만든다.

    발신 기기를 여러 대 지정했으면 **각 기기마다 같은 발신량**을 따로 만든다.
    예) 기기 2대 × 총 10회 → 기기당 10건, 합쳐 20건 (랜덤 순서는 기기마다 따로 섞는다).
    기기 지정이 없으면 예전처럼 한 묶음을 공유 대기열(device_id = NULL)에 넣는다.
    """
    # 삭제 보관함으로 옮긴 스케줄은 절대 실행하지 않는다.
    if "deleted_at" in schedule_row.keys() and schedule_row["deleted_at"]:
        return None
    numbers = db.json_list(schedule_row["numbers"])
    total_calls = schedule_row["total_calls"] if "total_calls" in schedule_row.keys() else None
    order_mode = schedule_row["order_mode"] or "sequence"
    raw_targets = schedule_row["target_device_ids"] if "target_device_ids" in schedule_row.keys() else None
    target_ids = db.json_list(raw_targets)
    if not target_ids and schedule_row["device_id"]:
        target_ids = [schedule_row["device_id"]]

    batches: list[tuple[str | None, list[tuple[str, int]]]] = []
    if target_ids:
        for device_id in target_ids:
            batches.append(
                (
                    device_id,
                    expand_tasks(
                        numbers,
                        schedule_row["repeat_count"],
                        order_mode,
                        total_calls=total_calls,
                    ),
                )
            )
    else:
        batches.append(
            (
                schedule_row["device_id"] or None,
                expand_tasks(
                    numbers,
                    schedule_row["repeat_count"],
                    order_mode,
                    total_calls=total_calls,
                ),
            )
        )

    total_tasks = sum(len(pairs) for _, pairs in batches)
    if total_tasks == 0:
        db.log(conn, f"스케줄 #{schedule_row['id']} 에 발신할 번호가 없습니다.", "warning")
        return None
    if total_tasks > config.MAX_TOTAL_CALLS:
        # API 에서 이미 막지만, 예전에 등록된 스케줄·직접 DB 수정에도 대비해 한 번 더 지킨다.
        db.log(
            conn,
            f"스케줄 #{schedule_row['id']} 발신량 {total_tasks}건이 상한({config.MAX_TOTAL_CALLS}건)을 넘어 실행하지 않았습니다.",
            "warning",
        )
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
                total_tasks,
                created,
            ),
        )
        run_id = int(cursor.lastrowid)

        task_rows = []
        seq = 0
        for device_id, pairs in batches:
            for number, repeat_index in pairs:
                seq += 1
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
                        device_id,
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
        device_text = f"기기 {len(target_ids)}대 × {total_tasks // len(target_ids)}건" if target_ids else "공유 대기열"
        db.log(
            conn,
            f"스케줄 '{schedule_row['name']}' 실행 생성: {total_tasks}건 ({device_text}, {source})",
        )
    return run_id


def run_schedule_now(conn, schedule_id: int) -> int | None:
    row = conn.execute(
        "SELECT * FROM schedules WHERE id = ? AND deleted_at IS NULL", (schedule_id,)
    ).fetchone()
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
    return _clamp_to_end_date(schedule_row, candidate)


def _clamp_to_end_date(schedule_row, candidate: datetime | None) -> datetime | None:
    """다음 실행 시각이 종료일(그날 23:59:59)보다 뒤면 실행하지 않는다(None)."""
    if candidate is None:
        return None
    end_date = _row_end_date(schedule_row)
    deadline = end_date_deadline(end_date)
    if deadline is not None and candidate > deadline:
        return None
    return candidate


def _row_end_date(schedule_row) -> str:
    """예전 DB(컬럼 없음)와도 안전하게 종료일을 읽는다."""
    try:
        keys = schedule_row.keys()
    except AttributeError:
        return ""
    if "end_date" not in keys:
        return ""
    return (schedule_row["end_date"] or "").strip()


def is_end_date_expired(schedule_row, current: datetime | None = None) -> bool:
    """종료일이 지났는지(그날 23:59:59 초과) 확인한다."""
    current = current or now()
    deadline = end_date_deadline(_row_end_date(schedule_row))
    return deadline is not None and current > deadline


def finish_expired_schedules(conn, current: datetime | None = None) -> list[int]:
    """종료일이 지난 활성 스케줄을 종료 처리한다.

    예약 시각이 종료일 뒤로 잠겨 있어 저절로 만기가 오지 않는 스케줄까지
    목록에서 분명히 '종료'로 보이도록 스케줄러 1주기마다 확인한다.
    """
    current = current or now()
    rows = conn.execute(
        """SELECT * FROM schedules
           WHERE status = 'active' AND deleted_at IS NULL AND COALESCE(end_date, '') <> ''"""
    ).fetchall()
    finished: list[int] = []
    for row in rows:
        deadline = end_date_deadline(_row_end_date(row))
        if deadline is None:
            continue
        next_at = parse_dt(row["next_run_at"]) if row["next_run_at"] else None
        if current > deadline or (next_at is not None and next_at > deadline):
            conn.execute(
                "UPDATE schedules SET next_run_at = NULL, status = 'finished' WHERE id = ?",
                (row["id"],),
            )
            db.log(
                conn,
                f"스케줄 '{row['name']}' 종료일({row['end_date']}) 도달 → 더 이상 실행하지 않습니다.",
                category="schedule",
            )
            finished.append(int(row["id"]))
    return finished


def run_due_schedules(conn, current: datetime | None = None) -> list[int]:
    """next_run_at 이 지난 활성 스케줄을 모두 실행한다. 생성된 run id 목록 반환."""
    current = current or now()
    rows = conn.execute(
        """SELECT * FROM schedules
           WHERE status = 'active' AND deleted_at IS NULL
             AND next_run_at IS NOT NULL AND next_run_at <= ?
           ORDER BY next_run_at ASC""",
        (iso(current),),
    ).fetchall()

    created_runs: list[int] = []
    for row in rows:
        # 종료일이 지난 스케줄은 실행을 만들지 않고 종료 처리한다.
        if is_end_date_expired(row, current):
            conn.execute(
                "UPDATE schedules SET next_run_at = NULL, status = 'finished' WHERE id = ?",
                (row["id"],),
            )
            db.log(
                conn,
                f"스케줄 '{row['name']}' 종료일({row['end_date']})이 지나 실행하지 않았습니다.",
                category="schedule",
            )
            continue
        run_id = create_run(conn, row, parse_dt(row["next_run_at"]) or current)
        next_at = _advance_next_run(row, current)
        status = "finished" if next_at is None else "active"
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, status = ? WHERE id = ?",
            (iso(next_at), status, row["id"]),
        )
        if next_at is None and _row_end_date(row) and status == "finished":
            db.log(
                conn,
                f"스케줄 '{row['name']}' 마지막 실행 후 종료일({row['end_date']})에 도달해 종료되었습니다.",
                category="schedule",
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
           WHERE deleted_at IS NULL AND status IN ('assigned', 'running')
             AND claimed_at IS NOT NULL AND claimed_at <= ?""",
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
           FROM tasks WHERE run_id = ? AND deleted_at IS NULL""",
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
            "UPDATE runs SET status = ?, done_tasks = ?, failed_tasks = ?, finished_at = COALESCE(finished_at, ?) "
            "WHERE id = ? AND deleted_at IS NULL",
            (status, done, failed, iso(now()), run_id),
        )
    else:
        status = "running" if (done + failed) > 0 else "pending"
        conn.execute(
            "UPDATE runs SET status = ?, done_tasks = ?, failed_tasks = ?, total_tasks = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (status, done, failed, total, run_id),
        )


def refresh_run(conn, run_id: int) -> None:
    _refresh_run(conn, run_id)


def retry_pending_telegram(conn, limit: int = 10) -> int:
    """전송 실패한 수신 문자를 재시도한다."""
    from .telegram_client import deliver_sms_notification

    rows = conn.execute(
        "SELECT id FROM sms_inbox WHERE deleted_at IS NULL AND telegram_status = 'pending' ORDER BY id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    sent = 0
    for row in rows:
        ok, _ = deliver_sms_notification(conn, int(row["id"]))
        if ok:
            sent += 1
    return sent


def tick(conn, current: datetime | None = None) -> dict:
    """스케줄러 1회 주기: 만기 실행 → 종료일 지난 스케줄 정리 → 응답 없는 작업 회수 → 텔레그램 재시도."""
    current = current or now()
    created = run_due_schedules(conn, current)
    expired = finish_expired_schedules(conn, current)
    reaped = reap_stale_tasks(conn, current)
    retried = retry_pending_telegram(conn)
    return {
        "runs_created": len(created),
        "schedules_finished": len(expired),
        "tasks_reaped": reaped,
        "telegram_retried": retried,
    }


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
