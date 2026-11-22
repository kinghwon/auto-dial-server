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


def clean_phone_number(value: str | None) -> str:
    """사용자가 입력한 전화번호(선택 항목). 빈 값은 "" 로, 형식이 틀리면 오류."""
    number = (value or "").strip()
    if not number:
        return ""
    if not PHONE_RE.match(number):
        raise ValueError(f"전화번호 형식이 올바르지 않습니다: {number}")
    return number


class DeviceRegister(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=80)
    android_version: str = Field(default="", max_length=40)
    app_version: str = Field(default="", max_length=40)
    # 기기가 문자를 받는 번호(선택). 앱이 보내지 않으면 대시보드에서 직접 입력한다.
    phone_number: str = Field(default="", max_length=30)
    note: str = Field(default="", max_length=400)

    @field_validator("phone_number")
    @classmethod
    def _phone_number(cls, value: str) -> str:
        return clean_phone_number(value)

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
    # 기기가 문자를 받는 번호. 빈 문자열을 보내면 지운다(None 이면 그대로 둔다).
    phone_number: Optional[str] = Field(default=None, max_length=30)
    enabled: Optional[bool] = None

    @field_validator("phone_number")
    @classmethod
    def _phone_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return clean_phone_number(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        """이름은 공백만 보내면 비운 것으로 보고 기기 ID 로 되돌린다."""
        if value is None:
            return None
        return value.strip()


class ArchiveRestore(BaseModel):
    """삭제 보관함 복원. kind 는 schedule/run/task/sms."""

    kind: Literal["schedule", "run", "task", "sms"]
    id: int = Field(ge=1)


ORDER_MODES = ("sequence", "grouped", "random")

# 한 스케줄에 동시에 지정할 수 있는 발신 기기 수
MAX_TARGET_DEVICES = 50


def clean_device_ids(values: list[str] | None) -> list[str]:
    """발신 기기 ID 목록. 빈 값은 버리고, 중복은 한 번만 남긴다."""
    cleaned: list[str] = []
    for item in values or []:
        value = str(item).strip()
        if not value:
            continue
        if "/" in value or "\\" in value:
            raise ValueError("기기 ID 를 올바르게 입력하세요. (/ 또는 \\ 제외)")
        if len(value) > 64:
            raise ValueError("기기 ID 는 64자까지입니다.")
        if value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > MAX_TARGET_DEVICES:
        raise ValueError(f"한 스케줄에 지정할 수 있는 기기는 {MAX_TARGET_DEVICES}대까지입니다.")
    return cleaned


class ScheduleBase(BaseModel):
    """스케줄 생성/수정에서 같이 쓰는 번호·시각 검증기."""

    @field_validator("numbers", check_fields=False)
    @classmethod
    def _numbers(cls, value):
        return clean_numbers(value)

    @field_validator("sms_body", check_fields=False)
    @classmethod
    def _sms_body(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("device_id", "target_device_id", check_fields=False)
    @classmethod
    def _device(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ScheduleCreate(ScheduleBase):
    name: str = Field(default="", max_length=120)
    type: Literal["call", "sms"]
    numbers: list[str] | str
    repeat_count: int = Field(default=1, ge=1, le=config.MAX_REPEAT_COUNT)
    # 총 발신 횟수. 지정하면 번호 개수 × repeat_count 대신 이 건수만큼 만든다.
    total_calls: Optional[int] = Field(default=None, ge=1, le=config.MAX_TOTAL_CALLS)
    order_mode: Literal["sequence", "grouped", "random"] = "sequence"
    max_call_duration_sec: Optional[int] = Field(default=None, ge=1, le=config.MAX_CALL_MAX_SEC)
    sms_body: Optional[str] = Field(default=None, max_length=2000)
    start_at: str
    recurrence: Literal["once", "daily", "weekly", "monthly"] = "once"
    # 발신 기기 한 대(예전 형식). 여러 대를 고를 때는 target_device_ids 를 쓴다.
    device_id: Optional[str] = Field(default=None, max_length=64)
    # 예전 이름 호환용 별칭(device_id 와 같은 뜻).
    target_device_id: Optional[str] = Field(default=None, max_length=64)
    # 발신 기기 여러 대. 각 기기에서 같은 횟수만큼 발신한다. 비우면 전체 기기 자동 배분.
    target_device_ids: Optional[list[str]] = None

    @field_validator("start_at")
    @classmethod
    def _start_at(cls, value: str) -> str:
        if parse_dt(value) is None:
            raise ValueError("실행 시각을 입력하세요.")
        return value.strip()

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
        # 발신 기기: 목록이 있으면 그 목록을, 없으면 device_id 한 대를 목록으로 옮긴다.
        ids = clean_device_ids(self.target_device_ids)
        if not ids:
            single = (self.device_id or self.target_device_id or "").strip()
            ids = clean_device_ids([single]) if single else []
        self.target_device_ids = ids
        self.device_id = ids[0] if len(ids) == 1 else None
        self.target_device_id = self.device_id
        per_device = self.total_calls if self.total_calls is not None else len(self.numbers) * max(1, self.repeat_count)
        if len(ids) > 1 and per_device * len(ids) > config.MAX_TOTAL_CALLS:
            raise ValueError(
                f"기기 {len(ids)}대 × {per_device}회 = {per_device * len(ids)}건은 한 번에 발신할 수 있는 "
                f"최대 {config.MAX_TOTAL_CALLS}건을 넘습니다. 기기 수나 총 발신 횟수를 줄이세요."
            )
        return self


class ScheduleUpdate(ScheduleBase):
    """스케줄 수정(PATCH). 보낸 항목만 바뀐다."""

    name: Optional[str] = Field(default=None, max_length=120)
    type: Optional[Literal["call", "sms"]] = None
    numbers: Optional[list[str] | str] = None
    repeat_count: Optional[int] = Field(default=None, ge=1, le=config.MAX_REPEAT_COUNT)
    total_calls: Optional[int] = Field(default=None, ge=1, le=config.MAX_TOTAL_CALLS)
    order_mode: Optional[Literal["sequence", "grouped", "random"]] = None
    max_call_duration_sec: Optional[int] = Field(default=None, ge=1, le=config.MAX_CALL_MAX_SEC)
    sms_body: Optional[str] = Field(default=None, max_length=2000)
    start_at: Optional[str] = None
    recurrence: Optional[Literal["once", "daily", "weekly", "monthly"]] = None
    device_id: Optional[str] = Field(default=None, max_length=64)
    target_device_id: Optional[str] = Field(default=None, max_length=64)
    target_device_ids: Optional[list[str]] = None
    status: Optional[Literal["active", "paused", "finished"]] = None

    @field_validator("start_at")
    @classmethod
    def _start_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if parse_dt(value) is None:
            raise ValueError("실행 시각을 입력하세요.")
        return value.strip()

    @model_validator(mode="after")
    def _require_change(self):
        if not self.model_fields_set:
            raise ValueError("수정할 항목을 하나 이상 보내야 합니다.")
        return self

    @model_validator(mode="after")
    def _sync_target_devices(self):
        """기기 지정은 target_device_ids(목록) 로 통일한다. device_id 는 한 대일 때만 채운다."""
        provided = self.model_fields_set
        if not ({"target_device_ids", "target_device_id", "device_id"} & provided):
            return self
        if "target_device_ids" in provided:
            ids = clean_device_ids(self.target_device_ids)
        else:
            single = (self.device_id or self.target_device_id or "").strip()
            ids = clean_device_ids([single]) if single else []
        self.target_device_ids = ids
        self.device_id = ids[0] if len(ids) == 1 else None
        self.target_device_id = self.device_id
        return self


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


class ScheduleCopy(BaseModel):
    """스케줄 복사. 기본은 일시정지 상태로 만들어 예약이 저절로 나가지 않게 한다."""

    name: Optional[str] = Field(default=None, max_length=120)
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ScheduleBulkDelete(BaseModel):
    """스케줄 부분 삭제(ids) / 실행했던 스케줄 삭제(executed_only) / 전체 삭제(all_schedules)."""

    ids: list[int] = Field(default_factory=list, max_length=1000)
    executed_only: bool = False
    all_schedules: bool = False

    @field_validator("ids")
    @classmethod
    def _ids(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        for item in value:
            number = int(item)
            if number not in cleaned:
                cleaned.append(number)
        return cleaned

    @model_validator(mode="after")
    def _require_target(self):
        if not self.all_schedules and not self.executed_only and not self.ids:
            raise ValueError(
                "삭제할 스케줄(ids)을 지정하거나 executed_only=true 또는 all_schedules=true 를 보내야 합니다."
            )
        return self


class RunBulkDelete(BaseModel):
    """실행 묶음 부분 삭제(ids) / 이미 끝난 실행 기록 전체 삭제(all_finished)."""

    ids: list[int] = Field(default_factory=list, max_length=1000)
    all_finished: bool = False

    @field_validator("ids")
    @classmethod
    def _ids(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        for item in value:
            number = int(item)
            if number not in cleaned:
                cleaned.append(number)
        return cleaned

    @model_validator(mode="after")
    def _require_target(self):
        if not self.all_finished and not self.ids:
            raise ValueError("삭제할 실행 기록(ids)을 지정하거나 all_finished=true 를 보내야 합니다.")
        return self


class SmsBulkDelete(BaseModel):
    """수신 문자 부분 삭제(ids) / 전체 삭제(all_sms)."""

    ids: list[int] = Field(default_factory=list, max_length=1000)
    all_sms: bool = False

    @field_validator("ids")
    @classmethod
    def _ids(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        for item in value:
            number = int(item)
            if number not in cleaned:
                cleaned.append(number)
        return cleaned

    @model_validator(mode="after")
    def _require_target(self):
        if not self.all_sms and not self.ids:
            raise ValueError("삭제할 문자(ids)를 지정하거나 all_sms=true 를 보내야 합니다.")
        return self


class TaskResult(BaseModel):
    device_id: Optional[str] = None
    status: Literal["done", "failed", "skipped"]
    result: Optional[str] = Field(default=None, max_length=1000)
    error: Optional[str] = Field(default=None, max_length=1000)
    duration_sec: Optional[float] = Field(default=None, ge=0, le=86400)


class SmsAttachment(BaseModel):
    """MMS 첨부(사진 등). 앱이 Base64 로 인코딩해 올린다."""

    filename: str = Field(default="attachment", max_length=200)
    content_type: str = Field(default="application/octet-stream", max_length=100)
    # Base64 문자열 길이 상한(약 6MB 실제 데이터). 텔레그램 사진 한 장 한도(10MB) 안쪽.
    data_base64: str = Field(min_length=1, max_length=8_000_000)


class SmsIncoming(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    sender: str = Field(default="", max_length=64)
    body: str = Field(default="", max_length=4000)
    # 이 문자를 받은 번호(그 기기의 번호). 앱이 보내면 그대로 저장하고,
    # 없으면 devices.phone_number 로 채운다. recipient 는 앱 쪽 호환용 이름.
    receiver_number: Optional[str] = Field(default=None, max_length=40)
    recipient: Optional[str] = Field(default=None, max_length=40)
    received_at: Optional[str] = None
    received_ts: Optional[int] = None  # epoch millis (앱에서 보내는 경우)
    # MMS 사진·파일 첨부(최대 10건). 비어 있으면 기존 문자 전용 경로 그대로 동작한다.
    attachments: list[SmsAttachment] = Field(default_factory=list, max_length=10)

    @field_validator("sender")
    @classmethod
    def _sender(cls, value: str) -> str:
        return value.strip()

    @field_validator("receiver_number", "recipient")
    @classmethod
    def _receiver_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @model_validator(mode="after")
    def _merge_receiver(self):
        if not (self.receiver_number or "").strip() and (self.recipient or "").strip():
            self.receiver_number = self.recipient.strip()
        return self


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
