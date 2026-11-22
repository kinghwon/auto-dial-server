"""시간 처리 유틸리티.

DB 에는 오프셋이 포함된 ISO8601 문자열(예: 2026-11-22T16:00:00+09:00)로 저장합니다.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

from .config import TZ

RECURRENCES = ("once", "daily", "weekly", "monthly")


def now() -> datetime:
    return datetime.now(TZ)


def parse_dt(value: str | datetime | None) -> datetime | None:
    """문자열/날짜를 서버 시간대(datetime)로 변환한다. 오프셋이 없으면 서버 TZ 로 해석."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time(0, 0))
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # '2026-11-22 16:00' 처럼 공백 구분/초 생략 등 관대하게 처리
            text = text.replace(" ", "T")
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"시간 형식을 해석할 수 없습니다: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(now())  # type: ignore[return-value]


def local_str(value: str | datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    dt = parse_dt(value) if not isinstance(value, datetime) else value
    if dt is None:
        return "-"
    return dt.astimezone(TZ).strftime(fmt)


def add_months(dt: datetime, months: int) -> datetime:
    """말일 보정 포함 월 단위 더하기 (1/31 + 1개월 = 2/28)."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_occurrence(dt: datetime, recurrence: str) -> datetime | None:
    """주어진 실행 시각의 '다음' 실행 시각. once 는 None."""
    recurrence = (recurrence or "once").lower()
    if recurrence == "once":
        return None
    if recurrence == "daily":
        return dt + timedelta(days=1)
    if recurrence == "weekly":
        return dt + timedelta(days=7)
    if recurrence == "monthly":
        return add_months(dt, 1)
    raise ValueError(f"알 수 없는 반복 주기: {recurrence}")


def first_run_at(start_at: datetime, current: datetime) -> datetime:
    """start_at 이 이미 지났다면 반복 주기 단위로 앞당겨 다음 실행 시각을 구한다."""
    return start_at
