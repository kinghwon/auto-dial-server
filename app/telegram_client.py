"""텔레그램 봇 전송 클라이언트와 메시지 포맷."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import config
from .timeutil import local_str, now

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# 사진 설명(caption)으로 쓸 수 있는 최대 글자 수(텔레그램 제한)
CAPTION_LIMIT = 1024


def in_telegram_window(dt: datetime | None = None) -> bool:
    """(참고용) 알림을 보낼 시간대인지 확인. 현재는 하루 종일 발송."""
    return True


def format_incoming_sms(
    device_id: str,
    device_name: str | None,
    sender: str,
    body: str,
    received_at: str | None,
) -> str:
    """수신 문자를 텔레그램 메시지로 만든다. (기기/발신번호/시각/내용 포함)"""
    device_label = device_name.strip() if device_name else ""
    if device_label and device_label != device_id:
        device_line = f"{device_label} ({device_id})"
    else:
        device_line = device_id or "알 수 없는 기기"

    when = local_str(received_at, "%Y-%m-%d %H:%M:%S") if received_at else local_str(now())
    text = (body or "").strip() or "(내용 없음)"

    return (
        "📩 수신 문자\n"
        f"기기: {device_line}\n"
        f"발신: {sender or '알 수 없음'}\n"
        f"수신: {when}\n"
        "내용:\n"
        f"{text}"
    )


def format_task_alert(title: str, lines: list[str]) -> str:
    return "\n".join([f"🔔 {title}", *lines])


def send_message(token: str, chat_id: str, text: str, timeout: float = 10.0) -> tuple[bool, str | None]:
    """텔레그램 sendMessage 호출. (성공여부, 오류메시지) 반환."""
    if not token or not chat_id:
        return False, "텔레그램 봇 토큰 또는 채팅 ID가 설정되지 않았습니다."
    url = TELEGRAM_API.format(token=token.strip(), method="sendMessage")
    payload: dict[str, Any] = {
        "chat_id": str(chat_id).strip(),
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                return True, None
            return False, str(data.get("description") or "텔레그램이 메시지를 거부했습니다.")
        # 429 등: 응답 본문에서 원인을 뽑아 남긴다.
        detail = ""
        try:
            detail = response.json().get("description", "")
        except Exception:  # noqa: BLE001
            detail = response.text[:300]
        return False, f"HTTP {response.status_code}: {detail}".strip()
    except httpx.HTTPError as exc:
        return False, f"텔레그램 연결 실패: {exc.__class__.__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("텔레그램 전송 중 알 수 없는 오류")
        return False, f"{exc.__class__.__name__}: {exc}"


def deliver_sms_notification(conn, sms_id: int) -> tuple[bool, str | None]:
    """sms_inbox 한 건을 텔레그램으로 보내고 결과를 DB 에 기록한다.

    MMS 사진 첨부가 있으면 글과 사진을 함께 보낸다. 글이 1024자인 짧으면
    첫 사진의 설명(caption)으로 붙이고, 길면 글을 먼저 보낸 뒤 사진을 따로 보낸다.
    첨부가 없는 일반 문자는 예전과 똑같이 sendMessage 한 번으로 보낸다.
    """
    from .db import get_settings
    from .timeutil import now_iso

    row = conn.execute("SELECT * FROM sms_inbox WHERE id = ?", (sms_id,)).fetchone()
    if row is None:
        return False, "수신 문자를 찾을 수 없습니다."

    settings = get_settings(conn)
    if settings.get("sms_forward_enabled", "1") != "1":
        conn.execute(
            "UPDATE sms_inbox SET telegram_status = 'disabled', telegram_error = ? WHERE id = ?",
            ("문자 전달이 꺼져 있습니다.", sms_id),
        )
        return False, "문자 전달이 꺼져 있습니다."

    device = conn.execute("SELECT name FROM devices WHERE id = ?", (row["device_id"],)).fetchone()
    device_name = device["name"] if device else None
    text = format_incoming_sms(
        device_id=row["device_id"] or "",
        device_name=device_name,
        sender=row["sender"] or "",
        body=row["body"] or "",
        received_at=row["received_at"],
    )

    token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    media = _row_media(row)

    if media:
        ok, error = _send_with_media(token, chat_id, text, media)
    else:
        ok, error = send_message(token, chat_id, text)

    attempts = (row["telegram_attempts"] or 0) + 1
    if ok:
        conn.execute(
            """UPDATE sms_inbox
               SET telegram_status = 'sent', telegram_error = ?, telegram_attempts = ?, telegram_sent_at = ?
               WHERE id = ?""",
            ((error or None), attempts, now_iso(), sms_id),
        )
    else:
        status = "pending" if attempts < config.TELEGRAM_MAX_ATTEMPTS else "failed"
        conn.execute(
            "UPDATE sms_inbox SET telegram_status = ?, telegram_error = ?, telegram_attempts = ? WHERE id = ?",
            (status, (error or "")[:500], attempts, sms_id),
        )
    return ok, error


def send_photo(
    token: str,
    chat_id: str,
    image_bytes: bytes,
    filename: str,
    caption: str | None = None,
    content_type: str = "image/jpeg",
    timeout: float = 30.0,
) -> tuple[bool, str | None]:
    """텔레그램 sendPhoto(multipart/form-data) 호출."""
    return _send_file(token, chat_id, "sendPhoto", "photo", image_bytes, filename, caption, content_type, timeout)


def send_document(
    token: str,
    chat_id: str,
    file_bytes: bytes,
    filename: str,
    caption: str | None = None,
    content_type: str = "application/octet-stream",
    timeout: float = 30.0,
) -> tuple[bool, str | None]:
    """텔레그램 sendDocument(multipart/form-data) 호출. 사진이 아닌 첨부용."""
    return _send_file(token, chat_id, "sendDocument", "document", file_bytes, filename, caption, content_type, timeout)


def _send_file(
    token: str,
    chat_id: str,
    method: str,
    field: str,
    file_bytes: bytes,
    filename: str,
    caption: str | None,
    content_type: str,
    timeout: float,
) -> tuple[bool, str | None]:
    if not token or not chat_id:
        return False, "텔레그램 봇 토큰 또는 채팅 ID가 설정되지 않았습니다."
    if not file_bytes:
        return False, "보낼 파일이 비어 있습니다."
    url = TELEGRAM_API.format(token=token.strip(), method=method)
    data: dict[str, Any] = {"chat_id": str(chat_id).strip()}
    if caption:
        data["caption"] = caption[:CAPTION_LIMIT]
    files = {field: (filename or "file", file_bytes, content_type or "application/octet-stream")}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, data=data, files=files)
        if response.status_code == 200:
            payload = response.json()
            if payload.get("ok"):
                return True, None
            return False, str(payload.get("description") or "텔레그램이 파일을 거부했습니다.")
        detail = ""
        try:
            detail = response.json().get("description", "")
        except Exception:  # noqa: BLE001
            detail = response.text[:300]
        return False, f"HTTP {response.status_code}: {detail}".strip()
    except httpx.HTTPError as exc:
        return False, f"텔레그램 연결 실패: {exc.__class__.__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("텔레그램 파일 전송 중 알 수 없는 오류")
        return False, f"{exc.__class__.__name__}: {exc}"


def _row_media(row) -> list[dict[str, str]]:
    """DB 행에 기록된 첨부 파일 중 실제로 남아 있는 것만 돌려준다."""
    try:
        keys = row.keys()
    except AttributeError:
        return []
    if "attachment_paths" not in keys:
        return []
    raw = row["attachment_paths"]
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    found: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path or not Path(path).is_file():
            continue
        found.append(
            {
                "path": path,
                "filename": str(item.get("filename") or Path(path).name),
                "content_type": str(item.get("content_type") or "application/octet-stream"),
            }
        )
    return found


def _send_with_media(
    token: str,
    chat_id: str,
    text: str,
    media: list[dict[str, str]],
) -> tuple[bool, str | None]:
    """글 + 첨부를 텔레그램으로 보낸다. (성공여부, 오류/경고메시지)"""
    photos = [item for item in media if str(item["content_type"]).startswith("image/")]
    others = [item for item in media if not str(item["content_type"]).startswith("image/")]
    errors: list[str] = []
    text_sent = False

    def _load(item: dict[str, str]) -> bytes | None:
        try:
            return Path(item["path"]).read_bytes()
        except OSError as exc:
            errors.append(f"{item['filename']} 읽기 실패: {exc.__class__.__name__}")
            return None

    # 1) 짧은 글은 첫 사진 설명으로 붙여 한 번에 보낸다.
    retry_photo: dict[str, str] | None = None
    if photos and len(text) <= CAPTION_LIMIT:
        first, *rest = photos
        payload = _load(first)
        if payload is not None:
            ok, error = send_photo(
                token,
                chat_id,
                payload,
                first["filename"],
                caption=text,
                content_type=first["content_type"],
            )
            if ok:
                text_sent = True
            else:
                # 사진 설명으로는 못 보냈으므로 글을 먼저 보내고 이 사진은 따로 다시 보낸다.
                retry_photo = first
            photos = rest

    # 2) 글을 먼저 보낸다(사진 설명으로 못 붙인 경우).
    if not text_sent:
        ok, error = send_message(token, chat_id, text)
        if not ok:
            detail = "; ".join([error or "", *errors]).strip("; ")
            return False, detail or "텔레그램 전송 실패"
        text_sent = True

    # 3) 남은 사진과 사진이 아닌 첨부를 차례로 보낸다.
    if retry_photo is not None:
        photos = [retry_photo, *photos]
    for item in photos:
        payload = _load(item)
        if payload is None:
            continue
        ok, error = send_photo(token, chat_id, payload, item["filename"], content_type=item["content_type"])
        if not ok:
            errors.append(f"사진 전송 실패: {error}")
    for item in others:
        payload = _load(item)
        if payload is None:
            continue
        ok, error = send_document(token, chat_id, payload, item["filename"], content_type=item["content_type"])
        if not ok:
            errors.append(f"첨부 전송 실패: {error}")

    if errors:
        return True, "일부 첨부 전송 실패: " + "; ".join(errors)[:400]
    return True, None
