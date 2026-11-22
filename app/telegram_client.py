"""텔레그램 봇 전송 클라이언트와 메시지 포맷."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from . import config
from .timeutil import local_str, now

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


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
    """sms_inbox 한 건을 텔레그램으로 보내고 결과를 DB 에 기록한다."""
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

    ok, error = send_message(
        settings.get("telegram_bot_token", ""),
        settings.get("telegram_chat_id", ""),
        text,
    )
    attempts = (row["telegram_attempts"] or 0) + 1
    if ok:
        conn.execute(
            """UPDATE sms_inbox
               SET telegram_status = 'sent', telegram_error = NULL, telegram_attempts = ?, telegram_sent_at = ?
               WHERE id = ?""",
            (attempts, now_iso(), sms_id),
        )
    else:
        status = "pending" if attempts < config.TELEGRAM_MAX_ATTEMPTS else "failed"
        conn.execute(
            "UPDATE sms_inbox SET telegram_status = ?, telegram_error = ?, telegram_attempts = ? WHERE id = ?",
            (status, (error or "")[:500], attempts, sms_id),
        )
    return ok, error
