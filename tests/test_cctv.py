"""초경량 CCTV API 테스트 (인증·메모리 중계·제어 큐)."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import cctv, config, db
from app.main import app

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def isolate_cctv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "APK_DIR", tmp_path / "apks")
    monkeypatch.setattr(config, "MACRO_BACKUP_DIR", tmp_path / "macro_backups")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "CCTV_CONFIG_PATH", tmp_path / "cctv_config.json")
    monkeypatch.setattr(config, "CCTV_VIEWER_ACTIVE_SEC", 10)
    cctv.reset_state()
    db.init_db()
    yield
    cctv.reset_state()


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _cfg() -> dict:
    return json.loads(config.CCTV_CONFIG_PATH.read_text(encoding="utf-8"))


def _login(client: TestClient) -> None:
    password = _password_from_config()
    response = client.post("/cctv/login", json={"username": "admin", "password": password})
    assert response.status_code == 200, response.text


def _password_from_config() -> str:
    """처음 만들어진 비밀번호는 해시만 남으므로, 테스트에서는 새로 지정한다."""
    cctv.load_config()
    cctv.set_viewer_password("test-pass-1234")
    return "test-pass-1234"


def _camera_headers() -> dict:
    return {"X-CCTV-Token": _cfg()["camera_token"]}


def test_config_file_is_created_with_random_secrets(tmp_path):
    data = cctv.load_config()
    path = config.CCTV_CONFIG_PATH
    assert path.is_file()
    assert data["password_hash"].startswith("pbkdf2_sha256$")
    assert len(data["camera_token"]) == 32
    assert len(data["session_secret"]) == 64
    assert not path.read_text(encoding="utf-8").count("password\"")
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_viewer_page_requires_login(client):
    response = client.get("/cctv", follow_redirects=False)
    assert response.status_code == 401
    assert "비밀번호" in response.text
    assert "/api/cctv/stream" not in response.text


def test_viewer_apis_require_login(client):
    for path in ("/api/cctv/status", "/api/cctv/frame"):
        response = client.get(path)
        assert response.status_code == 401, path
    response = client.post("/api/cctv/command", json={"command": "front"})
    assert response.status_code == 401
    response = client.post("/api/cctv/settings", json={"new_password": "abcdef"})
    assert response.status_code == 401


def test_login_wrong_password_then_right_password(client):
    cctv.load_config()
    cctv.set_viewer_password("right-pass-1")
    bad = client.post("/cctv/login", json={"username": "admin", "password": "wrong-pass"})
    assert bad.status_code == 401
    good = client.post("/cctv/login", json={"username": "admin", "password": "right-pass-1"})
    assert good.status_code == 200
    assert good.json()["ok"] is True
    page = client.get("/cctv")
    assert page.status_code == 200
    assert "/api/cctv/stream" in page.text
    status = client.get("/api/cctv/status")
    assert status.status_code == 200
    assert status.json()["camera_online"] is False


def test_login_form_sets_cookie_and_logout_clears_it(client):
    cctv.load_config()
    cctv.set_viewer_password("form-pass-1")
    response = client.post(
        "/cctv/login",
        data={"username": "admin", "password": "form-pass-1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/cctv"
    assert "cctv_session" in response.headers.get("set-cookie", "")
    assert client.get("/cctv").status_code == 200
    client.post("/cctv/logout", follow_redirects=False)
    assert client.get("/cctv").status_code == 401


def test_basic_auth_works(client):
    cctv.load_config()
    cctv.set_viewer_password("basic-pass-1")
    raw = base64.b64encode(b"admin:basic-pass-1").decode()
    response = client.get("/api/cctv/status", headers={"Authorization": "Basic " + raw})
    assert response.status_code == 200
    bad = client.get(
        "/api/cctv/status",
        headers={"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()},
    )
    assert bad.status_code == 401


def test_login_lockout_after_repeated_failures(client):
    cctv.load_config()
    cctv.set_viewer_password("lock-pass-1")
    for _ in range(cctv.LOGIN_MAX_FAILS):
        assert client.post("/cctv/login", json={"password": "bad"}).status_code == 401
    blocked = client.post("/cctv/login", json={"password": "lock-pass-1"})
    assert blocked.status_code == 429


def test_camera_endpoints_need_token(client):
    cctv.load_config()
    bad = client.post("/api/cctv/camera/heartbeat", json={"battery": 50})
    assert bad.status_code == 401
    wrong = client.post(
        "/api/cctv/camera/heartbeat",
        json={"battery": 50},
        headers={"X-CCTV-Token": "nope"},
    )
    assert wrong.status_code == 401
    legacy = client.post(
        "/api/cctv/camera/heartbeat",
        json={"battery": 50},
        headers={"CCTV_CAMERA_TOKEN": _cfg()["camera_token"]},
    )
    assert legacy.status_code == 200
    ok = client.post("/api/cctv/camera/heartbeat", json={"battery": 50}, headers=_camera_headers())
    assert ok.status_code == 200


def test_heartbeat_reports_status_and_streaming_flag(client):
    _password_from_config()
    _login(client)
    first = client.post(
        "/api/cctv/camera/heartbeat",
        json={"battery": 77, "charging": True, "camera": "back", "quality": "mid", "device_name": "4.4 폰"},
        headers=_camera_headers(),
    )
    assert first.status_code == 200
    assert first.json()["streaming_wanted"] is False
    assert first.json()["commands"] == []

    status = client.get("/api/cctv/status").json()
    assert status["camera_online"] is True
    assert status["battery"] == 77
    assert status["charging"] is True
    assert status["device_name"] == "4.4 폰"

    # 뷰어가 프레임을 한 번 요청하면 그 뒤 10초 동안은 전송을 원한다
    assert client.get("/api/cctv/frame").status_code == 404
    wanted = client.post("/api/cctv/camera/heartbeat", json={}, headers=_camera_headers())
    assert wanted.json()["streaming_wanted"] is True


def test_streaming_flag_turns_off_when_viewer_leaves(client, monkeypatch):
    _password_from_config()
    _login(client)
    client.get("/api/cctv/status")
    now = cctv._now()
    monkeypatch.setattr(cctv, "_now", lambda: now + 11)
    response = client.post("/api/cctv/camera/heartbeat", json={}, headers=_camera_headers())
    assert response.json()["streaming_wanted"] is False


def test_frame_upload_and_fetch(client):
    _password_from_config()
    _login(client)
    response = client.post(
        "/api/cctv/camera/frame",
        content=JPEG,
        headers={**_camera_headers(), "Content-Type": "image/jpeg"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["bytes"] == len(JPEG)

    frame = client.get("/api/cctv/frame")
    assert frame.status_code == 200
    assert frame.content == JPEG
    assert frame.headers["content-type"] == "image/jpeg"

    status = client.get("/api/cctv/status").json()
    assert status["frame_seq"] == 1
    assert status["last_frame_sec_ago"] == 0


def test_frame_upload_multipart_and_validation(client):
    cctv.load_config()
    files = {"file": ("frame.jpg", JPEG, "image/jpeg")}
    assert client.post("/api/cctv/camera/frame", files=files, headers=_camera_headers()).status_code == 200
    not_jpeg = client.post(
        "/api/cctv/camera/frame",
        content=b"hello",
        headers={**_camera_headers(), "Content-Type": "image/jpeg"},
    )
    assert not_jpeg.status_code == 400
    empty = client.post(
        "/api/cctv/camera/frame",
        content=b"",
        headers={**_camera_headers(), "Content-Type": "image/jpeg"},
    )
    assert empty.status_code == 400


def test_frame_size_limit(client, monkeypatch):
    cctv.load_config()
    monkeypatch.setattr(config, "CCTV_MAX_FRAME_BYTES", 10)
    response = client.post(
        "/api/cctv/camera/frame",
        content=JPEG,
        headers={**_camera_headers(), "Content-Type": "image/jpeg"},
    )
    assert response.status_code == 413


def test_command_queue_is_delivered_once(client):
    _password_from_config()
    _login(client)
    assert client.post("/api/cctv/command", json={"command": "front"}).status_code == 200
    assert client.post("/api/cctv/command", json={"command": "flash_on"}).status_code == 200
    assert client.post("/api/cctv/command", json={"command": "quality_high"}).status_code == 200
    first = client.post("/api/cctv/camera/heartbeat", json={}, headers=_camera_headers()).json()
    assert first["commands"] == ["front", "flash_on", "quality_high"]
    second = client.post("/api/cctv/camera/heartbeat", json={}, headers=_camera_headers()).json()
    assert second["commands"] == []


def test_command_rejects_unknown(client):
    _password_from_config()
    _login(client)
    response = client.post("/api/cctv/command", json={"command": "format_disk"})
    assert response.status_code == 400


def test_change_password_invalidates_old_password(client):
    cctv.load_config()
    cctv.set_viewer_password("first-pass-1")
    login = client.post("/cctv/login", json={"username": "admin", "password": "first-pass-1"})
    assert login.status_code == 200
    changed = client.post("/api/cctv/settings", json={"new_password": "second-pass-2"})
    assert changed.status_code == 200
    client.post("/cctv/logout", follow_redirects=False)
    assert client.post("/cctv/login", json={"password": "first-pass-1"}).status_code == 401
    assert client.post("/cctv/login", json={"password": "second-pass-2"}).status_code == 200


def test_change_password_requires_six_characters(client):
    _password_from_config()
    _login(client)
    assert client.post("/api/cctv/settings", json={"new_password": "12345"}).status_code == 400


def test_regenerate_camera_token(client):
    _password_from_config()
    _login(client)
    before = _cfg()["camera_token"]
    response = client.post("/api/cctv/settings", json={"regenerate_camera_token": True})
    assert response.status_code == 200
    after = response.json()["camera_token"]
    assert after != before
    old = client.post("/api/cctv/camera/heartbeat", json={}, headers={"X-CCTV-Token": before})
    assert old.status_code == 401
    new = client.post("/api/cctv/camera/heartbeat", json={}, headers={"X-CCTV-Token": after})
    assert new.status_code == 200


def test_token_hint_does_not_leak_full_token(client):
    _password_from_config()
    _login(client)
    status = client.get("/api/cctv/status").json()
    token = _cfg()["camera_token"]
    assert status["camera_token_hint"] != token
    assert token not in json.dumps(status)


def test_mjpeg_stream_sets_viewer_stream_count(client):
    _password_from_config()
    _login(client)
    client.post(
        "/api/cctv/camera/frame",
        content=JPEG,
        headers={**_camera_headers(), "Content-Type": "image/jpeg"},
    )
    with client.stream("GET", "/api/cctv/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
        chunk = next(response.iter_bytes())
        assert b"--frame" in chunk
    status = client.get("/api/cctv/status").json()
    assert status["streaming_wanted"] is True


def test_cctv_appears_in_apps_list(client, tmp_path):
    folder = config.APK_DIR / "cctv"
    folder.mkdir(parents=True)
    (folder / "cctv-1.0.0.apk").write_bytes(b"APK")
    (folder / "latest.json").write_text(
        json.dumps(
            {
                "app_id": "cctv",
                "version_code": 1,
                "version_name": "1.0.0",
                "file_name": "cctv-1.0.0.apk",
                "sha256": "0" * 64,
                "size_bytes": 3,
                "updated_at": "2026-11-28T20:00:00",
            }
        ),
        encoding="utf-8",
    )
    response = client.get("/api/apps")
    assert response.status_code == 200
    apps = {item["app_id"]: item for item in response.json()["apps"]}
    assert apps["cctv"]["display_name"] == "초경량 CCTV"
    page = client.get("/apps")
    assert "초경량 CCTV" in page.text


# --- 진단 로그 (앱 오류·스택트레이스 서버 보관) --------------------------------
def test_camera_logs_need_camera_token(client):
    cctv.load_config()
    body = {"logs": ["hello"]}
    assert client.post("/api/cctv/camera/logs", json=body).status_code == 401
    assert client.post("/api/cctv/camera/logs", json=body, headers={"X-CCTV-Token": "nope"}).status_code == 401
    ok = client.post("/api/cctv/camera/logs", json=body, headers=_camera_headers())
    assert ok.status_code == 200
    assert ok.json()["received"] == 1


def test_viewer_logs_need_login(client):
    _password_from_config()
    assert client.get("/api/cctv/logs").status_code == 401
    _login(client)
    response = client.get("/api/cctv/logs")
    assert response.status_code == 200
    assert response.json()["logs"] == []


def test_camera_logs_are_kept_newest_two_hundred(client):
    cctv.load_config()
    lines = ["줄 {}".format(i) for i in range(250)]
    response = client.post(
        "/api/cctv/camera/logs",
        json={"logs": lines, "device_name": "TB371FC", "app_version": "1.0.2"},
        headers=_camera_headers(),
    )
    assert response.status_code == 200
    assert response.json()["received"] == 200  # 한 번에 받는 상한
    assert response.json()["total"] == 200

    _password_from_config()
    _login(client)
    logs = client.get("/api/cctv/logs").json()["logs"]
    assert len(logs) == 200
    assert logs[-1]["line"] == "줄 199"
    assert logs[0]["line"] == "줄 0"
    assert logs[-1]["device"] == "TB371FC"
    assert logs[-1]["version"] == "1.0.2"
    assert logs[-1]["source"] == "push"
    assert logs[-1]["at"]

    # 더 보내면 오래된 것부터 밀려난다
    client.post("/api/cctv/camera/logs", json={"logs": ["새 줄"]}, headers=_camera_headers())
    logs = client.get("/api/cctv/logs").json()["logs"]
    assert len(logs) == 200
    assert logs[-1]["line"] == "새 줄"
    assert logs[0]["line"] == "줄 1"
    assert client.get("/api/cctv/logs?limit=5").json()["logs"][-1]["line"] == "새 줄"
    assert len(client.get("/api/cctv/logs?limit=5").json()["logs"]) == 5


def test_heartbeat_carries_logs(client):
    _password_from_config()
    _login(client)
    stack = "java.lang.RuntimeException: Fail to connect to camera service\n\tat android.hardware.Camera.native_setup(Native Method)"
    response = client.post(
        "/api/cctv/camera/heartbeat",
        json={
            "battery": 60,
            "device_name": "TB371FC",
            "app_version": "1.0.2",
            "logs": ["[카메라 열기 실패] " + stack],
        },
        headers=_camera_headers(),
    )
    assert response.status_code == 200
    logs = client.get("/api/cctv/logs").json()["logs"]
    assert len(logs) == 1
    assert "RuntimeException" in logs[0]["line"]
    assert "\n" in logs[0]["line"]  # 스택트레이스는 한 덩어리로 보존
    assert logs[0]["source"] == "heartbeat"
    assert logs[0]["device"] == "TB371FC"
    assert client.get("/api/cctv/status").json()["log_lines"] == 1


def test_camera_logs_validate_payload_and_trim(client):
    cctv.load_config()
    assert client.post("/api/cctv/camera/logs", json=["a", "b"], headers=_camera_headers()).json()["received"] == 2
    bad = client.post("/api/cctv/camera/logs", json={"logs": "한 줄"}, headers=_camera_headers())
    assert bad.status_code == 400
    empty = client.post("/api/cctv/camera/logs", json={}, headers=_camera_headers())
    assert empty.status_code == 200
    long_line = "x" * 5000
    client.post("/api/cctv/camera/logs", json={"logs": [long_line, "   "]}, headers=_camera_headers())
    _password_from_config()
    _login(client)
    logs = client.get("/api/cctv/logs").json()["logs"]
    assert len(logs) == 3  # 빈 줄은 버린다
    assert len(logs[-1]["line"]) < 5000
    assert logs[-1]["line"].endswith("(잘림)")
