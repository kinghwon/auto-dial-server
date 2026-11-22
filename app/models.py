"""API 요청/응답 스키마."""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from . import config
from .timeutil import RECURRENCES, parse_dt

PHONE_RE = re.compile(r"^[0-9+\-()#*\s]{3,30}$")


def clean_numbers(values: list[str] | str) -> list[str]:
    if isinstance(values, str):
        raw = re.split(r"[\n,;]+", values)
    else:
        raw = []
        for item in values:
            raw.extend(re.split(r"[\n,;]+", str(item)))
    cleaned: list[str] = []
    for item in raw:
        number = item.strip()
        if not number:
            continue
        if not PHONE_RE.match(number):
            raise ValueError(f"전화번호 형식이 올바르지 않습니다: {number}")
        cleaned.append(number)
    if not cleaned:
        raise ValueError("발신할 전화번호를 1개 이상 입력하세요.")
    if len(cleaned) > 500:
        raise ValueError("한 스케줄에 등록할 수 있는 번호는 500개까지입니다.")
    return cleaned


class DeviceRegister(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=80)
    android_version: str = Field(default="", max_length=40)
    app_version: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=400)

    @field_validator("device_id")
    @classmethod
    def _trim_id(cls, value: str) -> str:
        value = value.strip()
        if not value or "/" in value or "\\" in value:
            raise ValueError("기기 ID 를 올바르게 입력하세요. (/ 또는 \\ 제외)")
        return value


class Heartbeat(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    battery: Optional[int] = Field(default=None, ge=0, le=100)
    network: str = Field(default="", max_length=40)


class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    note: Optional[str] = Field(default=None, max_length=400)
    enabled: Optional[bool] = None


class ScheduleCreate(BaseModel):
    name: str = Field(default="", max_length=120)
    type: Literal["call", "sms"]
    numbers: list[str] | str
    repeat_count: int = Field(default=1, ge=1, le=config.MAX_REPEAT_COUNT)
    order_mode: Literal["sequence", "grouped"] = "sequence"
    max_call_duration_sec: Optional[int] = Field(default=None, ge=1, le=config.MAX_CALL_MAX_SEC)
    sms_body: Optional[str] = Field(default=None, max_length=2000)
    start_at: str
    recurrence: Literal["once", "daily", "weekly", "monthly"] = "once"
    device_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("numbers")
    @classmethod
    def _numbers(cls, value):
        return clean_numbers(value)

    @field_validator("start_at")
    @classmethod
    def _start_at(cls, value: str) -> str:
        if parse_dt(value) is None:
            raise ValueError("실행 시각을 입력하세요.")
        return value.strip()

    @field_validator("sms_body")
    @classmethod
    def _sms_body(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("device_id")
    @classmethod
    def _device(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _check_type_fields(self):
        if self.type == "call":
            if self.max_call_duration_sec is None:
                self.max_call_duration_sec = config.DEFAULT_CALL_MAX_SEC
            self.sms_body = None
        else:
            if not (self.sms_body or "").strip():
                raise ValueError("문자 발신에는 문자 내용(sms_body)이 필요합니다.")
            self.max_call_duration_sec = None
        if self.recurrence not in RECURRENCES:
            raise ValueError("반복 주기는 once/daily/weekly/monthly 중 하나여야 합니다.")
        if not self.name.strip():
            label = "전화" if self.type == "call" else "문자"
            self.name = f"{label} 발신 {self.start_at[5:16].replace('T', ' ')}"
        return self


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    status: Optional[Literal["active", "paused", "finished"]] = None


class TaskBulkDelete(BaseModel):
    """대기열 부분 삭제(task_ids) / 전체 삭제(all_pending 또는 all_tasks)."""

    task_ids: list[int] = Field(default_factory=list, max_length=1000)
    all_pending: bool = False
    all_tasks: bool = False
    # 특정 실행 묶음(run) 안에서만 지우고 싶을 때
    run_id: Optional[int] = Field(default=None, ge=1)

    @field_validator("task_ids")
    @classmethod
    def _task_ids(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        for item in value:
            if int(item) not in cleaned:
                cleaned.append(int(item))
        return cleaned

    @model_validator(mode="after")
    def _require_target(self):
        if not self.all_pending and not self.all_tasks and not self.task_ids:
            raise ValueError("삭제할 작업(task_ids)을 지정하거나 all_pending=true 또는 all_tasks=true 를 보내야 합니다.")
        return self


class TaskResult(BaseModel):
    device_id: Optional[str] = None
    status: Literal["done", "failed", "skipped"]
    result: Optional[str] = Field(default=None, max_length=1000)
    error: Optional[str] = Field(default=None, max_length=1000)
    duration_sec: Optional[float] = Field(default=None, ge=0, le=86400)


class SmsIncoming(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    sender: str = Field(default="", max_length=64)
    body: str = Field(default="", max_length=4000)
    received_at: Optional[str] = None
    received_ts: Optional[int] = None  # epoch millis (앱에서 보내는 경우)

    @field_validator("sender")
    @classmethod
    def _sender(cls, value: str) -> str:
        return value.strip()


class TelegramSettings(BaseModel):
    bot_token: Optional[str] = Field(default=None, max_length=200)
    chat_id: Optional[str] = Field(default=None, max_length=80)
    enabled: Optional[bool] = None
    sms_forward_enabled: Optional[bool] = None


class TelegramTest(BaseModel):
    text: str = Field(default="✅ 텔레그램 연동 테스트 메시지입니다.", max_length=1000)


class SettingsUpdate(BaseModel):
    """기타 운영 설정(선택)."""
    online_timeout_sec: Optional[int] = Field(default=None, ge=10, le=3600)
    default_poll_interval_sec: Optional[int] = Field(default=None, ge=1, le=300)


class RemoteSettings(BaseModel):
    """폰에 내려보내는 원격 동작 설정(APK 재설치 없이 대시보드에서 변경)."""

    poll_interval_sec: Optional[int] = Field(default=None, ge=2, le=300)
    call_gap_sec: Optional[int] = Field(default=None, ge=0, le=600)
    sms_forward_enabled: Optional[bool] = None
    mms_forward_enabled: Optional[bool] = None
    # 통화 중 동작(발신할 때 폰이 적용)
    call_recording_enabled: Optional[bool] = None  # 통화 녹음(기본 끔)
    call_mute_volume: Optional[bool] = None  # 통화 볼륨 0(기본 켬)
    call_mute_mic: Optional[bool] = None  # 내 소리 차단 = 마이크 음소거(기본 켬)
