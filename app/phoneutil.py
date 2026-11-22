"""전화번호 정규화.

기기 키(devices.id)와 수신 번호(devices.phone_number)는 같은 값이어야 하므로,
서버로 들어오는 모든 전화번호를 한 가지 표기로 맞춘다.

예) `+821056563939` / `01056563939` / `010 5656 3939` → `010-5656-3939`

앱(PhoneNumberHelper.normalize)과 같은 규칙을 쓴다. 읽을 수 없는 값(SIM 번호 없음,
"null", 글자 섞인 쓰레기)은 빈 문자열 `""` 로 돌려준다.
"""
from __future__ import annotations

import re

# 전화번호로 볼 수 있는 최소 자릿수(서비스 번호·쓰레기 값 방지).
MIN_DIGITS = 7

_NON_DIGIT = re.compile(r"[^0-9]")


def _format_korean(digits: str) -> str:
    """010-5656-3939 / 02-2123-4567 처럼 국내 표기로 맞춘다. 모르는 길이는 숫자만 돌려준다."""
    length = len(digits)
    if digits.startswith("02"):
        if length == 9:
            return f"02-{digits[2:5]}-{digits[5:]}"
        if length == 10:
            return f"02-{digits[2:6]}-{digits[6:]}"
        return digits
    if digits.startswith("050") and length == 11:
        return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"
    if digits.startswith("050") and length == 12:
        return f"{digits[:4]}-{digits[4:8]}-{digits[8:]}"
    if length == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if length == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return digits


def normalize_phone(raw: str | None) -> str:
    """전화번호 문자열을 기기 키로 쓸 수 있는 표기로 정리한다. 못 읽으면 `""`."""
    value = (raw or "").strip()
    if not value:
        return ""
    lower = value.lower()
    if lower in {"null", "unknown", "none"}:
        return ""
    if any(char.isalpha() for char in value):
        return ""

    has_plus = value.startswith("+")
    digits = _NON_DIGIT.sub("", value)
    if not digits:
        return ""

    # 국제표기(+82, 0082)는 국내 표기(0…)로 되돌린다.
    if digits.startswith("0082"):
        digits = "0" + digits[4:]
    elif digits.startswith("82") and 11 <= len(digits) <= 13:
        digits = "0" + digits[2:]

    if len(digits) < MIN_DIGITS:
        return ""

    if digits.startswith("0"):
        return _format_korean(digits)
    return f"+{digits}" if has_plus else digits


def is_phone_key(value: str | None) -> bool:
    """기기 키로 쓸 수 있는 전화번호인지(정규화하면 값이 나오는지)."""
    return bool(normalize_phone(value))
