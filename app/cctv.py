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
}
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SEC = 10 * 60


def _now() -> float:
    """현재 시각(초). 테스트에서 monkeypatch 로 바꿔 쓴다."""
    return time.time()


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
# 메모리 상태 (최신 프레임 1장 + 카메라 상태 + 제어 큐 + 뷰어 활동 시각)
# --------------------------------------------------------------------------
class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.frame: Optional[bytes] = None
        self.frame_seq = 0
        self.frame_at = 0.0
        self.camera: dict = {}
        self.camera_seen = 0.0
        self.commands: list[str] = []
        self.viewer_last = 0.0
        self.viewer_streams = 0
        self.frames_from_camera = 0

    def reset(self) -> None:
        with self.lock:
            self.frame = None
            self.frame_seq = 0
            self.frame_at = 0.0
            self.camera = {}
            self.camera_seen = 0.0
            self.commands = []
            self.viewer_last = 0.0
            self.viewer_streams = 0
            self.frames_from_camera = 0


_state = _State()
_login_fails: dict[str, list[float]] = {}


def reset_state() -> None:
    """테스트에서 메모리 상태를 비운다."""
    _state.reset()
    _login_fails.clear()


def _viewer_active() -> bool:
    window = float(getattr(config, "CCTV_VIEWER_ACTIVE_SEC", 10) or 10)
    with _state.lock:
        last = _state.viewer_last
        streams = _state.viewer_streams
    return streams > 0 or (_now() - last) <= window


def _touch_viewer() -> None:
    with _state.lock:
        _state.viewer_last = _now()


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


def require_viewer(request: Request) -> None:
    """뷰어 인증(쿠키 세션 또는 HTTP Basic). 없으면 401."""
    if check_session(request.cookies.get(COOKIE_NAME)):
        _touch_viewer()
        return
    if _check_basic(request.headers.get("authorization")):
        _touch_viewer()
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
        _touch_viewer()
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
def cctv_status(request: Request):
    require_viewer(request)
    data = load_config(create=False)
    with _state.lock:
        camera = dict(_state.camera)
        camera_seen = _state.camera_seen
        frame_at = _state.frame_at
        frame_seq = _state.frame_seq
        streams = _state.viewer_streams
    online = (camera_seen > 0) and ((_now() - camera_seen) < 20)
    return {
        "ok": True,
        "camera_online": online,
        "streaming_wanted": _viewer_active(),
        "viewer_streams": streams,
        "viewer_window_sec": float(getattr(config, "CCTV_VIEWER_ACTIVE_SEC", 10) or 10),
        "battery": camera.get("battery"),
        "charging": camera.get("charging"),
        "camera": camera.get("camera"),
        "flash": camera.get("flash"),
        "quality": camera.get("quality"),
        "device_name": camera.get("device_name"),
        "app_version": camera.get("app_version"),
        "screen_on": camera.get("screen_on"),
        "fps": camera.get("fps"),
        "last_seen_sec_ago": round(_now() - camera_seen) if camera_seen else None,
        "last_frame_sec_ago": round(_now() - frame_at) if frame_at else None,
        "frame_seq": frame_seq,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "camera_token_hint": data["camera_token"][:4] + "…" + data["camera_token"][-4:],
        "username": data.get("username", "admin"),
    }


@router.get("/api/cctv/frame")
def cctv_frame(request: Request):
    """최신 JPEG 한 장(메모리)."""
    require_viewer(request)
    with _state.lock:
        frame = _state.frame
        seq = _state.frame_seq
    if not frame:
        raise HTTPException(status_code=404, detail="아직 카메라 영상이 없습니다.")
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Frame-Seq": str(seq)},
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
async def cctv_stream(request: Request):
    """MJPEG 실시간 스트림(뷰어가 연결한 동안만 카메라에 전송 요청)."""
    require_viewer(request)
    with _state.lock:
        _state.viewer_streams += 1

    async def gen():
        last_seq = -1
        try:
            while True:
                if await request.is_disconnected():
                    break
                _touch_viewer()
                with _state.lock:
                    frame = _state.frame
                    seq = _state.frame_seq
                if frame and seq != last_seq:
                    last_seq = seq
                    yield _mjpeg_chunk(frame)
                await asyncio.sleep(0.2)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            with _state.lock:
                _state.viewer_streams = max(0, _state.viewer_streams - 1)

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/api/cctv/command")
async def cctv_command(request: Request):
    """뷰어가 카메라에 전/후면·플래시·화질 명령을 넣는다."""
    require_viewer(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    command = str((body or {}).get("command") or "").strip().lower()
    if command not in ALLOWED_COMMANDS:
        raise HTTPException(status_code=400, detail="알 수 없는 명령입니다.")
    with _state.lock:
        _state.commands.append(command)
        queued = len(_state.commands)
    logger.info("CCTV 명령 대기열 추가: %s", command)
    return {"ok": True, "command": command, "queued": queued}


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
@router.post("/api/cctv/camera/heartbeat")
async def cctv_camera_heartbeat(
    request: Request,
    x_cctv_token: Optional[str] = Header(default=None, alias="X-CCTV-Token"),
    legacy_token: Optional[str] = Header(default=None, alias="CCTV_CAMERA_TOKEN"),
):
    """폰이 3~5초마다 보내는 상태. streaming_wanted 와 제어 명령을 돌려준다."""
    require_camera(x_cctv_token, legacy_token)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    with _state.lock:
        _state.camera_seen = _now()
        for key in (
            "battery",
            "charging",
            "camera",
            "flash",
            "quality",
            "device_name",
            "app_version",
            "screen_on",
            "fps",
        ):
            if key in body:
                _state.camera[key] = body[key]
        wanted = _viewer_active()
        commands = _state.commands
        _state.commands = []
    return {
        "ok": True,
        "streaming_wanted": wanted,
        "commands": commands,
        "viewer_active": wanted,
        "heartbeat_sec": int(getattr(config, "CCTV_HEARTBEAT_SEC", 4) or 4),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post("/api/cctv/camera/frame")
async def cctv_camera_frame(
    request: Request,
    x_cctv_token: Optional[str] = Header(default=None, alias="X-CCTV-Token"),
    legacy_token: Optional[str] = Header(default=None, alias="CCTV_CAMERA_TOKEN"),
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
    else:
        data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="빈 프레임입니다.")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="프레임이 너무 큽니다.")
    if not data.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=400, detail="JPEG 파일이 아닙니다.")
    with _state.lock:
        _state.frame = data
        _state.frame_seq += 1
        _state.frame_at = _now()
        _state.frames_from_camera += 1
        _state.camera_seen = _now()
        wanted = _viewer_active()
    return {"ok": True, "streaming_wanted": wanted, "bytes": len(data)}


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
