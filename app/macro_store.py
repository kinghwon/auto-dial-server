"""'자동 터치' 앱의 매크로 백업 보관소.

자동 발신 DB(autodialer.db)와는 완전히 분리된 JSON 파일에 보관한다.
(자동 발신 쪽 스케줄·기기·문자 데이터에는 어떤 변경도 하지 않는다.)

보관 위치: ``{MACRO_BACKUP_DIR}/{app_id}/backups.json``

파일 형식::

    {"app_id": "auto-touch", "backups": [
        {"id": "...", "name": "...", "device_name": "...",
         "macro_count": 3, "step_count": 42,
         "created_at": "...", "updated_at": "...",
         "macros": [ ...앱이 보낸 매크로 JSON 그대로... ]}
    ]}

쓰기는 임시 파일에 쓴 뒤 교체(원자적)하고, 한 프로세스 안에서는 락으로 직렬화한다.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from .timeutil import TZ, now_iso

# 백업 ID: 소문자·숫자 8~32자 (UUID4 hex 앞 12자)
BACKUP_ID_RE = re.compile(r"^[a-z0-9]{8,32}$")

MAX_NAME_LEN = 60
MAX_DEVICE_NAME_LEN = 60
MAX_MACROS = 200
MAX_STEPS = 5000

_lock = threading.Lock()


def _root() -> Path:
    return Path(getattr(config, "MACRO_BACKUP_DIR", Path(config.DATA_DIR) / "macro_backups"))


def _file(app_id: str) -> Path:
    return _root() / app_id / "backups.json"


def _read(app_id: str) -> list[dict]:
    path = _file(app_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("backups") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id")]


def _write(app_id: str, backups: list[dict]) -> None:
    path = _file(app_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"app_id": app_id, "updated_at": now_iso(), "backups": backups}
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _meta(item: dict) -> dict:
    """목록에 보여 줄 요약(매크로 본문 제외)."""
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "device_name": str(item.get("device_name") or ""),
        "macro_count": int(item.get("macro_count") or 0),
        "step_count": int(item.get("step_count") or 0),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
    }


def count_steps(macros: list[dict]) -> int:
    total = 0
    for macro in macros:
        actions = macro.get("actions") if isinstance(macro, dict) else None
        if isinstance(actions, list):
            total += len(actions)
    return total


def list_backups(app_id: str) -> list[dict]:
    """최신 저장 순으로 백업 요약 목록."""
    with _lock:
        backups = _read(app_id)
    metas = [_meta(item) for item in backups]
    order = {item["id"]: int(item.get("seq") or 0) for item in backups}
    # 저장 시각이 같은 초에 겹쳐도 나중에 저장한 것이 위로 오도록 seq 를 함께 본다.
    metas.sort(key=lambda item: (item["updated_at"], order.get(item["id"], 0)), reverse=True)
    return metas


def get_backup(app_id: str, backup_id: str) -> Optional[dict]:
    """백업 하나의 전체 내용(매크로 본문 포함)."""
    with _lock:
        backups = _read(app_id)
    for item in backups:
        if item.get("id") == backup_id:
            payload = _meta(item)
            payload["macros"] = item.get("macros") or []
            return payload
    return None


def delete_backup(app_id: str, backup_id: str) -> bool:
    with _lock:
        backups = _read(app_id)
        remaining = [item for item in backups if item.get("id") != backup_id]
        if len(remaining) == len(backups):
            return False
        _write(app_id, remaining)
    return True


def save_backup(
    app_id: str,
    name: str,
    device_name: Optional[str],
    macros: list[dict],
) -> tuple[dict, bool]:
    """백업을 저장한다. 같은 이름이 있으면 그 백업을 덮어쓴다.

    @return (백업 요약, 새로 만들었는지 여부)
    """
    cleaned_name = (name or "").strip()[:MAX_NAME_LEN]
    if not cleaned_name:
        cleaned_name = datetime.now(TZ).strftime("백업 %Y-%m-%d %H:%M")
    cleaned_device = (device_name or "").strip()[:MAX_DEVICE_NAME_LEN]
    stamp = now_iso()
    step_count = count_steps(macros)
    with _lock:
        backups = _read(app_id)
        existing = next((item for item in backups if str(item.get("name") or "") == cleaned_name), None)
        created = existing is None
        if existing is None:
            if len(backups) >= int(getattr(config, "MAX_MACRO_BACKUPS", 200)):
                raise ValueError(
                    f"서버에 저장할 수 있는 백업은 {getattr(config, 'MAX_MACRO_BACKUPS', 200)}개까지입니다. "
                    "필요 없는 백업을 먼저 지워 주세요."
                )
            existing = {
                "id": uuid.uuid4().hex[:12],
                "name": cleaned_name,
                "created_at": stamp,
            }
            backups.append(existing)
        existing["seq"] = max([int(item.get("seq") or 0) for item in backups] + [0]) + 1
        existing["name"] = cleaned_name
        existing["device_name"] = cleaned_device
        existing["macro_count"] = len(macros)
        existing["step_count"] = step_count
        existing["updated_at"] = stamp
        existing["macros"] = macros
        _write(app_id, backups)
        return _meta(existing), created
