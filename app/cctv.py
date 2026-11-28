"""초경량 CCTV 중계 모듈.

폰(안드로이드 4.4 카메라 앱)이 올려 주는 JPEG 한 장을 **메모리에만** 들고 있다가,
로그인한 뷰어가 보고 있을 때만 폰에 "지금 보내라"고 알려 주는 방식이다.

· 디스크에 영상을 저장하지 않는다(디스크 사용 0MB) — 최신 프레임 1장이 메모리에만 있다.
· 뷰어가 최근 CCTV_VIEWER_ACTIVE_SEC(기본 10초) 안에 프레임을 요청한 적이 없으면
  streaming_wanted=false 를 돌려줘 폰이 대기 중 트래픽을 쓰지 않게 한다.
· 뷰어 페이지·영상·제어 API 는 모두 로그인(쿠키 세션 또는 HTTP Basic)이 필요하다.
· 카메라 업로드·하트비트는 CCTV_CAMERA_TOKEN 헤더가 있어야 한다.

비밀번호·카메라 토큰·세션 서명 키는 /data/cctv_config.json 에 저장한다(파일 권한 600).
처음 실행할 때 추측하기 어려운 값을 자동으로 만들어 둔다.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import cctv_page, config

logger = logging.getLogger("auto-dialer.cctv")

COOKIE_NAME = "cctv_session"
PASSWORD_ITERATIONS = 120_000
ALLOWED_COMMANDS = {
    "front",
    "back",
    "flash_on",
    "flash_off",
    "quality_low",
    "quality_mid",
    "quality_high",
    "fps",
}
# 뷰어·앱에서 고를 수 있는 초당 프레임
FPS_CHOICES = (1, 2, 3, 5, 8, 10)
DEFAULT_FPS = 3
# 이 시간(초) 동안 하트비트가 없으면 그 기기는 오프라인으로 본다.
DEVICE_ONLINE_SEC = 20
DEVICE_KEY_MAX = 64
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SEC = 10 * 60
# 앱이 보내는 진단 로그: 디스크에는 쓰지 않고 메모리에 최근 1000줄만 들고 있는다.
LOG_MAX_LINES = 1000
# 한 번의 요청에서 받아 주는 로그 줄 수 상한(앱이 밀린 로그를 몰아 보낼 때 대비)
LOG_MAX_LINES_PER_POST = 200
# 로그 한 줄(스택트레이스는 여러 줄) 최대 길이
LOG_MAX_CHARS = 4000


def _now() -> float:
    """현재 시각(초). 테스트에서 monkeypatch 로 바꿔 쓴다."""
    return time.time()


def _now_text() -> str:
    """사람이 읽는 시각(서버 시간대, 기본 Asia/Seoul)."""
    return datetime.now(config.TZ).strftime("%Y-%m-%d %H:%M:%S")


def _config_path() -> Path:
    path = getattr(config, "CCTV_CONFIG_PATH", None)
    if path is None:
        path = Path(config.DATA_DIR) / "cctv_config.json"
    return Path(path)


# --------------------------------------------------------------------------
# 설정 파일 (비밀번호 해시·카메라 토큰·세션 서명 키)
# --------------------------------------------------------------------------
_READABLE = "abcdefghjkmnpqrstuvwxyz23456789"


def _readable_secret(groups: int = 4, size: int = 4) -> str:
    """눈으로 읽고 옮겨 적기 쉬운 임의 문자열(예: k7fm-2xqd-9wrt-h3np)."""
    parts = ["".join(secrets.choice(_READABLE) for _ in range(size)) for _ in range(groups)]
    return "-".join(parts)


def hash_password(password: str, salt: Optional[bytes] = None, iterations: int = PASSWORD_ITERATIONS) -> str:
    """PBKDF2-HMAC-SHA256 해시 문자열(pbkdf2_sha256$반복$salt_b64$hash_b64)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def _new_config(password: Optional[str] = None, camera_token: Optional[str] = None) -> dict:
    return {
        "username": "admin",
        "password_hash": hash_password(password or _readable_secret()),
        "camera_token": camera_token or secrets.token_hex(16),
        "session_secret": secrets.token_hex(32),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _save_config(data: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def load_config(create: bool = True) -> dict:
    """설정 파일을 읽고, 없으면 새로 만든다."""
    path = _config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("cctv_config.json 을 읽지 못했습니다: %s", exc)
            data = None
        if isinstance(data, dict) and data.get("password_hash") and data.get("camera_token"):
            data.setdefault("username", "admin")
            if not data.get("session_secret"):
                data["session_secret"] = secrets.token_hex(32)
                _save_config(data)
            return data
    if not create:
        raise HTTPException(status_code=503, detail="CCTV 설정이 없습니다.")
    data = _new_config()
    _save_config(data)
    logger.info("CCTV 설정을 새로 만들었습니다: %s", path)
    return data


def set_viewer_password(new_password: str) -> None:
    if len(new_password or "") < 6:
        raise HTTPException(status_code=400, detail="비밀번호는 6자 이상이어야 합니다.")
    data = load_config()
    data["password_hash"] = hash_password(new_password)
    _save_config(data)
    logger.info("CCTV 뷰어 비밀번호를 변경했습니다.")


def regenerate_camera_token() -> str:
    data = load_config()
    data["camera_token"] = secrets.token_hex(16)
    _save_config(data)
    logger.info("CCTV 카메라 토큰을 새로 만들었습니다.")
    return data["camera_token"]


# --------------------------------------------------------------------------
# 메모리 상태 -- 기기별로 독립된 카메라 슬롯
#   최신 프레임 1장 + 카메라 상태 + 제어 큐 + 뷰어 활동 시각을 기기마다 따로 둔다.
#   디스크에는 아무것도 쓰지 않는다.
# --------------------------------------------------------------------------
def normalize_device_key(raw) -> str:
    """기기 ID·이름을 딕셔너리 키와 URL 에 쓸 수 있는 짧은 문자열로 만든다.

    예: "LENOVO TB371FC" -> "lenovo-tb371fc", "갤럭시 S23+" -> "갤럭시-s23"
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    chars: list[str] = []
    for ch in text:
        if ch.isalnum():
            chars.append(ch)
        elif ch in " -_.:/|,":
            chars.append("-")
    key = "".join(chars)
    while "--" in key:
        key = key.replace("--", "-")
    return key.strip("-.")[:DEVICE_KEY_MAX]


def _camera_device_key(device_id: str = "", device_name: str = "") -> str:
    """폰이 보낸 고유 ID(없으면 기기 이름)로 슬롯 키를 만든다."""
    return normalize_device_key(device_id) or normalize_device_key(device_name) or "default"


class _Device:
    """카메라 한 대의 메모리 슬롯."""

    def __init__(self, key: str, device_id: str = "") -> None:
        self.key = key
        self.device_id = device_id or key
        self.frame: Optional[bytes] = None
        self.frame_seq = 0
        self.frame_at = 0.0
        self.camera: dict = {}
        self.camera_seen = 0.0
        self.commands: list[str] = []
        self.viewer_last = 0.0
        self.viewer_streams = 0
        self.frames_from_camera = 0

    @property
    def name(self) -> str:
        return str(self.camera.get("device_name") or self.key)

    def online(self) -> bool:
        return self.camera_seen > 0 and (_now() - self.camera_seen) < DEVICE_ONLINE_SEC

    def reset(self) -> None:
        self.frame = None
        self.frame_seq = 0
        self.frame_at = 0.0
        self.camera = {}
        self.camera_seen = 0.0
        self.commands = []
        self.viewer_last = 0.0
        self.viewer_streams = 0
        self.frames_from_camera = 0


class _Registry:
    """기기 슬롯 모음 + 앱 진단 로그(전체 공용)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.devices: dict[str, _Device] = {}
        self.logs: list[dict] = []

    def reset(self) -> None:
        with self.lock:
            self.devices = {}
            self.logs = []

    def get(self, key: Optional[str]) -> Optional[_Device]:
        if not key:
            return None
        with self.lock:
            return self.devices.get(key)

    def ensure(self, key: str, device_id: str = "", device_name: str = "") -> _Device:
        with self.lock:
            device = self.devices.get(key)
            if device is None:
                device = _Device(key, device_id)
                # 기기가 붙기 전에 뷰어가 보낸 명령은 'default' 자리표시자에 쌓여 있다.
                # 첫 실제 기기가 등록되면 그 명령을 넘겨 받는다(하위 호환).
                placeholder = self.devices.get("default")
                if key != "default" and placeholder is not None and not placeholder.camera_seen:
                    device.commands = list(placeholder.commands)
                    device.viewer_last = placeholder.viewer_last
                    self.devices.pop("default", None)
                # v1.0.3 처럼 기기 이름으로만 등록되던 옛 자리를 새 기기(device_id)로 합친다.
                # (업데이트 후 같은 폰이 온라인·오프라인 두 탭으로 보이지 않게)
                legacy_key = normalize_device_key(device_name)
                if legacy_key and legacy_key != key:
                    legacy = self.devices.get(legacy_key)
                    if legacy is not None and not str(legacy.camera.get("device_id") or ""):
                        device.frame = legacy.frame or device.frame
                        device.frame_seq = max(device.frame_seq, legacy.frame_seq)
                        device.frame_at = legacy.frame_at or device.frame_at
                        device.camera.update(legacy.camera)
                        device.camera_seen = legacy.camera_seen
                        device.commands = list(legacy.commands) + list(device.commands)
                        device.viewer_last = max(device.viewer_last, legacy.viewer_last)
                        device.viewer_streams = legacy.viewer_streams
                        device.frames_from_camera = legacy.frames_from_camera
                        self.devices.pop(legacy_key, None)
                        logger.info("CCTV 옛 기기 자리(%s)를 새 기기(%s)로 합쳤습니다.", legacy_key, key)
                self.devices[key] = device
                logger.info("CCTV 기기 등록: %s (%s)", key, device.name)
            elif device_id and not device.device_id:
                device.device_id = device_id
            return device

    def ordered(self) -> list[_Device]:
        """온라인 기기 먼저, 그다음 최근에 들은 기기 순."""
        with self.lock:
            items = list(self.devices.values())
        items.sort(key=lambda d: (0 if d.online() else 1, -(d.camera_seen or 0), -(d.frame_at or 0), d.key))
        return items

    def pick(self, key: Optional[str] = None) -> Optional[_Device]:
        """지정한 기기, 없으면 최근 온라인 기기(하위 호환: 기기 미지정 호출)."""
        if key and str(key).strip():
            wanted = normalize_device_key(key)
            device = self.get(wanted)
            if device is not None:
                return device
            # 키가 아니라 기기 ID 로 온 경우도 받아 준다
            with self.lock:
                for item in self.devices.values():
                    if normalize_device_key(item.device_id) == wanted:
                        return item
            return None
        items = self.ordered()
        return items[0] if items else None


_state = _Registry()
_login_fails: dict[str, list[float]] = {}


def reset_state() -> None:
    """테스트에서 메모리 상태를 비운다."""
    _state.reset()
    _login_fails.clear()


def _viewer_active(device: _Device) -> bool:
    """이 기기의 영상을 지금 뷰어가 보고 있는가."""
    window = float(getattr(config, "CCTV_VIEWER_ACTIVE_SEC", 10) or 10)
    with _state.lock:
        last = device.viewer_last
        streams = device.viewer_streams
    return streams > 0 or (_now() - last) <= window


def _touch_viewer(device: Optional[_Device]) -> None:
    if device is None:
        return
    with _state.lock:
        device.viewer_last = _now()


def _append_logs(
    lines: list,
    device: str = "",
    version: str = "",
    source: str = "",
    device_key: str = "",
) -> int:
    """폰이 보낸 로그 줄을 메모리에 쌓는다(최근 LOG_MAX_LINES 개만 유지).

    디스크에는 아무것도 쓰지 않는다. 각 항목은 서버가 받은 시각과 함께 남겨
    스택트레이스가 여러 줄이어도 한 덩어리로 읽을 수 있게 한다.
    """
    if not isinstance(lines, list):
        return 0
    accepted = 0
    with _state.lock:
        for raw in lines[:LOG_MAX_LINES_PER_POST]:
            text = str(raw if raw is not None else "")
            if not text.strip():
                continue
            if len(text) > LOG_MAX_CHARS:
                text = text[:LOG_MAX_CHARS] + " …(잘림)"
            _state.logs.append(
                {
                    "at": _now_text(),
                    "at_epoch": round(_now(), 3),
                    "device": str(device or "")[:80],
                    "device_key": str(device_key or "")[:DEVICE_KEY_MAX],
                    "version": str(version or "")[:32],
                    "source": str(source or "")[:16],
                    "line": text,
                }
            )
            accepted += 1
        if len(_state.logs) > LOG_MAX_LINES:
            del _state.logs[: len(_state.logs) - LOG_MAX_LINES]
    return accepted


# --------------------------------------------------------------------------
# 로그인 세션 (서명된 쿠키)
# --------------------------------------------------------------------------
def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(data: Optional[dict] = None) -> tuple[str, int]:
    data = data or load_config()
    hours = int(getattr(config, "CCTV_SESSION_HOURS", 72) or 72)
    exp = int(_now()) + hours * 3600
    sig = _sign(data["session_secret"], "v1:" + str(exp))
    return "v1.{}.{}".format(exp, sig), exp


def check_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        version, exp_text, sig = token.split(".", 2)
        if version != "v1":
            return False
        exp = int(exp_text)
    except (ValueError, TypeError):
        return False
    if exp < _now():
        return False
    try:
        data = load_config(create=False)
    except HTTPException:
        return False
    expected = _sign(data["session_secret"], "v1:" + str(exp))
    return hmac.compare_digest(sig, expected)


def _check_basic(header: Optional[str]) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
        username, _, password = raw.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    try:
        data = load_config(create=False)
    except HTTPException:
        return False
    return username == data.get("username", "admin") and verify_password(password, data["password_hash"])


def require_viewer(request: Request, device: Optional[_Device] = None) -> None:
    """뷰어 인증(쿠키 세션 또는 HTTP Basic). 없으면 401.

    device 를 주면 그 기기의 '봄' 시각을 지금으로 미룬다(그 기기만 전송).
    """
    if check_session(request.cookies.get(COOKIE_NAME)):
        _touch_viewer(device)
        return
    if _check_basic(request.headers.get("authorization")):
        _touch_viewer(device)
        return
    raise HTTPException(
        status_code=401,
        detail="CCTV 뷰어 비밀번호가 필요합니다.",
        headers={"WWW-Authenticate": 'Basic realm="cctv"'},
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _login_locked(ip: str) -> bool:
    now = _now()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_LOCK_SEC]
    _login_fails[ip] = fails
    return len(fails) >= LOGIN_MAX_FAILS


def _login_failed(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(_now())


def _login_ok(ip: str) -> None:
    _login_fails.pop(ip, None)


def _is_https(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def _set_cookie(response, request: Request, value: str, max_age: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )


def require_camera(*tokens: Optional[str]) -> None:
    """카메라 업로드·하트비트 인증.

    헤더 이름은 `X-CCTV-Token` 을 쓴다. Caddy(Go) 뒤에서는 밑줄이 들어간
    헤더(`CCTV_CAMERA_TOKEN`)가 전달 과정에서 사라지기 때문에, 예전 이름도
    함께 받아 준다(직접 8000 포트로 붙을 때 유용).
    """
    data = load_config()
    supplied = next((str(t).strip() for t in tokens if t and str(t).strip()), "")
    if not supplied or not hmac.compare_digest(supplied, data["camera_token"]):
        logger.warning("CCTV 카메라 토큰 불일치(받은 값 길이 %s)", len(supplied))
        raise HTTPException(status_code=401, detail="카메라 토큰이 올바르지 않습니다.")


# --------------------------------------------------------------------------
# 라우터
# --------------------------------------------------------------------------
router = APIRouter(tags=["cctv"])


@router.get("/cctv", include_in_schema=False)
def cctv_page_view(request: Request):
    """뷰어 페이지. 로그인하지 않았으면 로그인 화면만 돌려준다(401)."""
    if check_session(request.cookies.get(COOKIE_NAME)) or _check_basic(request.headers.get("authorization")):
        return HTMLResponse(cctv_page.render_viewer(), headers={"Cache-Control": "no-store"})
    return HTMLResponse(cctv_page.render_login(), status_code=401, headers={"Cache-Control": "no-store"})


@router.post("/cctv/login", include_in_schema=False)
async def cctv_login(request: Request):
    """뷰어 로그인(폼 또는 JSON). 성공하면 서명 쿠키를 준다."""
    ip = _client_ip(request)
    if _login_locked(ip):
        return HTMLResponse(
            cctv_page.render_login("로그인 시도가 너무 많습니다. 10분 뒤에 다시 시도해 주세요."),
            status_code=429,
            headers={"Cache-Control": "no-store"},
        )
    content_type = (request.headers.get("content-type") or "").lower()
    username = ""
    password = ""
    if "application/json" in content_type:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            username = str(body.get("username") or "")
            password = str(body.get("password") or "")
    else:
        form = await request.form()
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")

    data = load_config()
    username = username or data.get("username", "admin")
    if username == data.get("username", "admin") and verify_password(password, data["password_hash"]):
        _login_ok(ip)
        token, exp = make_session(data)
        if "application/json" in content_type:
            response = JSONResponse({"ok": True, "expires_at": exp})
        else:
            response = Response(status_code=303, headers={"Location": "/cctv"})
        _set_cookie(response, request, token, max_age=max(60, exp - int(_now())))
        logger.info("CCTV 뷰어 로그인 성공: %s", ip)
        return response

    _login_failed(ip)
    logger.warning("CCTV 뷰어 로그인 실패: %s", ip)
    if "application/json" in content_type:
        return JSONResponse({"ok": False, "detail": "아이디 또는 비밀번호가 올바르지 않습니다."}, status_code=401)
    return HTMLResponse(
        cctv_page.render_login("아이디 또는 비밀번호가 올바르지 않습니다."),
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/cctv/logout", include_in_schema=False)
def cctv_logout():
    response = Response(status_code=303, headers={"Location": "/cctv"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/api/cctv/status")
def cctv_status(request: Request, device: Optional[str] = None):
    """선택한 기기(?device=)의 상태. 기기를 지정하지 않으면 최근 온라인 기기."""
    require_viewer(request)
    data = load_config(create=False)
    with _state.lock:
        target = _state.pick(device)
    _touch_viewer(target)
    if target is None:
        # 아직 등록된 기기가 없어도 기존 형식(오프라인)으로 답한다.
        return {
            "ok": True,
            "device": None,
            "device_count": 0,
            "camera_online": False,
            "streaming_wanted": False,
            "viewer_streams": 0,
            "viewer_window_sec": float(getattr(config, "CCTV_VIEWER_ACTIVE_SEC", 10) or 10),
            "battery": None,
            "charging": None,
            "camera": None,
            "flash": None,
            "quality": None,
            "fps": None,
            "device_name": None,
            "app_version": None,
            "model": None,
            "android": None,
            "sdk": None,
            "engine": None,
            "screen_on": None,
            "last_seen_sec_ago": None,
            "last_frame_sec_ago": None,
            "frame_seq": 0,
            "log_lines": 0,
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "camera_token_hint": data["camera_token"][:4] + "…" + data["camera_token"][-4:],
            "username": data.get("username", "admin"),
        }
    summary = _device_summary(target)
    with _state.lock:
        log_lines = sum(1 for item in _state.logs if item.get("device_key") == target.key)
        device_count = len(_state.devices)
    result = {"ok": True, "device_count": device_count, "log_lines": log_lines}
    result.update(summary)
    result.update(
        {
            "viewer_window_sec": float(getattr(config, "CCTV_VIEWER_ACTIVE_SEC", 10) or 10),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "camera_token_hint": data["camera_token"][:4] + "…" + data["camera_token"][-4:],
            "username": data.get("username", "admin"),
        }
    )
    return result


@router.get("/api/cctv/devices")
def cctv_devices(request: Request, device: Optional[str] = None):
    """연결된 전체 기기 목록(온라인 먼저, 최근에 들은 순)."""
    require_viewer(request)
    with _state.lock:
        items = _state.ordered()
        selected = _state.pick(device)
        payload = [_device_summary(item) for item in items]
        selected_key = selected.key if selected else None
    return {
        "ok": True,
        "count": len(payload),
        "devices": payload,
        "selected": selected_key,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/api/cctv/frame")
def cctv_frame(request: Request, device: Optional[str] = None):
    """선택한 기기의 최신 JPEG 한 장(메모리)."""
    require_viewer(request)
    with _state.lock:
        target = _state.pick(device)
    if target is None:
        raise HTTPException(status_code=404, detail="등록된 기기가 없습니다.")
    _touch_viewer(target)
    with _state.lock:
        frame = target.frame
        seq = target.frame_seq
    if not frame:
        raise HTTPException(status_code=404, detail="아직 카메라 영상이 없습니다.")
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Frame-Seq": str(seq), "X-CCTV-Device": target.key},
    )


def _mjpeg_chunk(jpeg: bytes) -> bytes:
    return (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(jpeg)).encode("ascii")
        + b"\r\n\r\n"
        + jpeg
        + b"\r\n"
    )


@router.get("/api/cctv/stream")
async def cctv_stream(request: Request, device: Optional[str] = None):
    """MJPEG 실시간 스트림(이 기기를 보는 동안만 그 기기에 전송 요청)."""
    require_viewer(request)
    with _state.lock:
        target = _state.pick(device)
    if target is None:
        raise HTTPException(status_code=404, detail="등록된 기기가 없습니다.")
    with _state.lock:
        target.viewer_streams += 1

    async def gen():
        last_seq = -1
        try:
            while True:
                if await request.is_disconnected():
                    break
                _touch_viewer(target)
                with _state.lock:
                    frame = target.frame
                    seq = target.frame_seq
                if frame and seq != last_seq:
                    last_seq = seq
                    yield _mjpeg_chunk(frame)
                await asyncio.sleep(0.2)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            with _state.lock:
                target.viewer_streams = max(0, target.viewer_streams - 1)

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-CCTV-Device": target.key},
    )


def _parse_fps_command(command: str, body: dict) -> Optional[str]:
    """'fps' 명령을 'fps_5' 형태로 바꾸고, 고를 수 없는 값이면 None."""
    raw = body.get("fps", body.get("value"))
    if raw is None and command.startswith("fps_"):
        raw = command[len("fps_"):]
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if value not in FPS_CHOICES:
        return None
    return "fps_{}".format(value)


@router.post("/api/cctv/command")
async def cctv_command(request: Request, device: Optional[str] = None):
    """뷰어가 기기에 전/후면·플래시·화질·FPS 명령을 넣는다."""
    require_viewer(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    command = str(body.get("command") or "").strip().lower()
    if command == "fps" or command.startswith("fps_"):
        parsed = _parse_fps_command(command, body)
        if parsed is None:
            raise HTTPException(
                status_code=400,
                detail="FPS 는 " + ", ".join(str(v) for v in FPS_CHOICES) + " 중 하나여야 합니다.",
            )
        command = parsed
    elif command not in ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail="알 수 없는 명령입니다.")
    with _state.lock:
        target = _state.pick(device or body.get("device"))
        if target is None:
            # 아직 기기가 안 붙었어도 명령을 쌓아 둔다(첫 기기가 등록되면 넘겨 받는다).
            target = _state.ensure("default")
        target.commands.append(command)
        queued = len(target.commands)
        target_key = target.key
    logger.info("CCTV 명령 대기열 추가(%s): %s", target_key, command)
    return {"ok": True, "command": command, "queued": queued, "device": target_key}


def _device_summary(device: _Device) -> dict:
    """기기 한 대의 상태(목록·상태 응답 공용)."""
    with _state.lock:
        camera = dict(device.camera)
        camera_seen = device.camera_seen
        frame_at = device.frame_at
        frame_seq = device.frame_seq
        streams = device.viewer_streams
        frames = device.frames_from_camera
    return {
        "device": device.key,
        "device_id": device.device_id,
        "device_name": camera.get("device_name") or device.key,
        "camera_online": device.online(),
        "online": device.online(),
        "streaming_wanted": _viewer_active(device),
        "viewer_streams": streams,
        "battery": camera.get("battery"),
        "charging": camera.get("charging"),
        "camera": camera.get("camera"),
        "flash": camera.get("flash"),
        "quality": camera.get("quality"),
        "fps": camera.get("fps"),
        "app_version": camera.get("app_version"),
        "model": camera.get("model"),
        "android": camera.get("android"),
        "sdk": camera.get("sdk"),
        "engine": camera.get("engine"),
        "screen_on": camera.get("screen_on"),
        "last_seen_sec_ago": round(_now() - camera_seen) if camera_seen else None,
        "last_frame_sec_ago": round(_now() - frame_at) if frame_at else None,
        "frame_seq": frame_seq,
        "frames_from_camera": frames,
    }


@router.post("/api/cctv/settings")
async def cctv_settings(request: Request):
    """뷰어 비밀번호 변경 / 카메라 토큰 재발급(로그인 필요)."""
    require_viewer(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    new_password = str(body.get("new_password") or "")
    regenerate = bool(body.get("regenerate_camera_token"))
    if not new_password and not regenerate:
        raise HTTPException(status_code=400, detail="바꿀 항목이 없습니다.")
    result: dict = {"ok": True}
    if new_password:
        set_viewer_password(new_password)
        result["password_changed"] = True
    if regenerate:
        result["camera_token"] = regenerate_camera_token()
    response = JSONResponse(result)
    if new_password:
        data = load_config()
        token, exp = make_session(data)
        result["session_expires_at"] = exp
        response = JSONResponse(result)
        _set_cookie(response, request, token, max_age=max(60, exp - int(_now())))
    return response


# --- 카메라(폰) 쪽 -----------------------------------------------------------
def _body_device(body: dict, header_id: Optional[str], header_name: Optional[str]) -> tuple[str, str]:
    """폰이 보낸 본문·헤더에서 (device_id, device_name) 을 꺼낸다."""
    device_id = str(body.get("device_id") or header_id or "").strip()
    device_name = str(body.get("device_name") or header_name or "").strip()
    return device_id, device_name


@router.post("/api/cctv/camera/heartbeat")
async def cctv_camera_heartbeat(
    request: Request,
    x_cctv_token: Optional[str] = Header(default=None, alias="X-CCTV-Token"),
    legacy_token: Optional[str] = Header(default=None, alias="CCTV_CAMERA_TOKEN"),
    x_cctv_device_id: Optional[str] = Header(default=None, alias="X-CCTV-Device-Id"),
    x_cctv_device_name: Optional[str] = Header(default=None, alias="X-CCTV-Device-Name"),
):
    """폰이 3~5초마다 보내는 상태. 그 기기의 streaming_wanted 와 제어 명령을 돌려준다."""
    require_camera(x_cctv_token, legacy_token)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    device_id, device_name = _body_device(body, x_cctv_device_id, x_cctv_device_name)
    key = _camera_device_key(device_id, device_name)
    with _state.lock:
        if not device_id and not device_name:
            # 예전 앱(기기 정보 없이 보냄)은 지금 기본 기기의 하트비트로 본다.
            device = _state.pick(None) or _state.ensure("default")
        else:
            device = _state.ensure(key, device_id, device_name)
        device.camera_seen = _now()
        for field in (
            "battery",
            "charging",
            "camera",
            "flash",
            "quality",
            "device_name",
            "app_version",
            "screen_on",
            "fps",
            "model",
            "android",
            "sdk",
            "engine",
            "fps_setting",
        ):
            if field in body:
                device.camera[field] = body[field]
        if device_name:
            device.camera["device_name"] = device_name
        if device_id:
            device.camera["device_id"] = device_id
        if isinstance(body.get("logs"), list):
            _append_logs(
                body["logs"],
                device=device_name or device.name,
                version=str(body.get("app_version") or device.camera.get("app_version") or ""),
                source="heartbeat",
                device_key=device.key,
            )
        wanted = _viewer_active(device)
        commands = device.commands
        device.commands = []
        resolved_name = device.name
        resolved_key = device.key
    return {
        "ok": True,
        "device": resolved_key,
        "device_name": resolved_name,
        "streaming_wanted": wanted,
        "commands": commands,
        "viewer_active": wanted,
        "heartbeat_sec": int(getattr(config, "CCTV_HEARTBEAT_SEC", 4) or 4),
        "fps_choices": list(FPS_CHOICES),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post("/api/cctv/camera/frame")
async def cctv_camera_frame(
    request: Request,
    device: Optional[str] = None,
    x_cctv_token: Optional[str] = Header(default=None, alias="X-CCTV-Token"),
    legacy_token: Optional[str] = Header(default=None, alias="CCTV_CAMERA_TOKEN"),
    x_cctv_device_id: Optional[str] = Header(default=None, alias="X-CCTV-Device-Id"),
    x_cctv_device_name: Optional[str] = Header(default=None, alias="X-CCTV-Device-Name"),
):
    """폰이 JPEG 한 장을 올린다(raw image/jpeg 또는 multipart 'file')."""
    require_camera(x_cctv_token, legacy_token)
    limit = int(getattr(config, "CCTV_MAX_FRAME_BYTES", 4 * 1024 * 1024) or 4 * 1024 * 1024)
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="file 필드가 필요합니다.")
        data = await upload.read()
        sent_name = str(form.get("device_name") or "")
        sent_id = str(form.get("device_id") or "")
    else:
        data = await request.body()
        sent_name = ""
        sent_id = ""
    if not data:
        raise HTTPException(status_code=400, detail="빈 프레임입니다.")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="프레임이 너무 큽니다.")
    if not data.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=400, detail="JPEG 파일이 아닙니다.")
    device_id = sent_id or (x_cctv_device_id or "")
    device_name = sent_name or (x_cctv_device_name or "")
    with _state.lock:
        if device_id or device_name:
            target = _state.ensure(_camera_device_key(device_id, device_name), device_id, device_name)
        else:
            # 예전 앱(기기 정보 없이 올림)은 지금 기본 기기 슬롯에 넣는다.
            target = _state.pick(device or None) or _state.ensure("default")
        target.frame = data
        target.frame_seq += 1
        target.frame_at = _now()
        target.frames_from_camera += 1
        target.camera_seen = _now()
        if device_name:
            target.camera["device_name"] = device_name
        wanted = _viewer_active(target)
        target_key = target.key
    return {"ok": True, "streaming_wanted": wanted, "bytes": len(data), "device": target_key}


@router.post("/api/cctv/camera/logs")
async def cctv_camera_logs(
    request: Request,
    x_cctv_token: Optional[str] = Header(default=None, alias="X-CCTV-Token"),
    legacy_token: Optional[str] = Header(default=None, alias="CCTV_CAMERA_TOKEN"),
    x_cctv_device_id: Optional[str] = Header(default=None, alias="X-CCTV-Device-Id"),
    x_cctv_device_name: Optional[str] = Header(default=None, alias="X-CCTV-Device-Name"),
):
    """폰이 진단 로그(오류·스택트레이스)를 올린다.

    본문은 {"logs": [...], "device_name": ..., "app_version": ...} 또는 줄 배열 그대로다.
    메모리에 최근 LOG_MAX_LINES 줄만 남기고 디스크에는 저장하지 않는다.
    """
    require_camera(x_cctv_token, legacy_token)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if isinstance(body, list):
        lines = body
        device = x_cctv_device_name or ""
        version = ""
        source = "push"
        device_id = x_cctv_device_id or ""
    elif isinstance(body, dict):
        lines = body.get("logs", body.get("lines"))
        if lines is None:
            lines = []
        if not isinstance(lines, list):
            raise HTTPException(status_code=400, detail="logs 는 줄 배열이어야 합니다.")
        device = str(body.get("device_name") or x_cctv_device_name or "")
        version = str(body.get("app_version") or "")
        source = str(body.get("source") or "push")
        device_id = str(body.get("device_id") or x_cctv_device_id or "")
    else:
        raise HTTPException(status_code=400, detail="로그 본문이 필요합니다.")
    key = _camera_device_key(device_id, device) if (device_id or device) else ""
    accepted = _append_logs(lines, device=device, version=version, source=source, device_key=key)
    with _state.lock:
        total = len(_state.logs)
    if accepted:
        logger.info("CCTV 로그 %s줄 도착(%s), 보관 중 %s줄", accepted, device or "?", total)
    return {"ok": True, "received": accepted, "total": total, "keep": LOG_MAX_LINES, "device": key}


@router.get("/api/cctv/logs")
def cctv_logs(request: Request, limit: int = LOG_MAX_LINES, device: Optional[str] = None):
    """뷰어 로그인 상태에서 진단 로그를 읽는다(옛것 → 새것 순). device 로 거를 수 있다."""
    require_viewer(request)
    limit = max(1, min(int(limit or LOG_MAX_LINES), LOG_MAX_LINES))
    wanted = normalize_device_key(device) if device else ""
    with _state.lock:
        items = _state.logs
        if wanted:
            items = [item for item in items if item.get("device_key") == wanted]
        logs = list(items[-limit:])
    return {
        "ok": True,
        "count": len(logs),
        "limit": limit,
        "order": "oldest_first",
        "device": wanted or None,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "logs": logs,
    }


@router.get("/api/cctv/camera/config")
def cctv_camera_config(
    x_cctv_token: Optional[str] = Header(default=None, alias="X-CCTV-Token"),
    legacy_token: Optional[str] = Header(default=None, alias="CCTV_CAMERA_TOKEN"),
):
    """앱이 처음 켜질 때 확인용(토큰 검사만)."""
    require_camera(x_cctv_token, legacy_token)
    return {
        "ok": True,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "heartbeat_sec": int(getattr(config, "CCTV_HEARTBEAT_SEC", 4) or 4),
    }
