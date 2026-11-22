"""서버 자동 검증 테스트.

실행:  cd server && ../.venv/bin/python -m pytest -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import config, db, scheduler, telegram_client
from app.main import app
from app.timeutil import TZ, add_months, next_occurrence, parse_dt


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "API_KEY", "")
    db.init_db()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _conn():
    return db.connect()


def make_schedule(client: TestClient, **overrides):
    payload = {
        "name": "",
        "type": "call",
        "numbers": ["010-1111-0001", "010-1111-0002", "010-1111-0003"],
        "repeat_count": 2,
        "start_at": "2026-11-23T09:00",
        "recurrence": "once",
        "max_call_duration_sec": 15,
    }
    payload.update(overrides)
    response = client.post("/api/schedules", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["schedule"]


# --------------------------------------------------------------------------
# 1. 기동 / 스키마
# --------------------------------------------------------------------------
def test_server_starts_and_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["timezone"] == "Asia/Seoul"


def test_db_schema_created(isolate_db):
    conn = _conn()
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"devices", "schedules", "runs", "tasks", "sms_inbox", "settings", "logs"} <= tables

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    for required in ("id", "run_id", "type", "phone_number", "seq", "repeat_index", "device_id", "status", "result"):
        assert required in columns

    schedule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
    for required in ("numbers", "repeat_count", "max_call_duration_sec", "sms_body", "start_at", "recurrence", "next_run_at"):
        assert required in schedule_columns
    conn.close()


# --------------------------------------------------------------------------
# 2. 기기 등록 / 온라인 상태
# --------------------------------------------------------------------------
def test_device_registration_heartbeat_and_online(client):
    response = client.post(
        "/api/devices",
        json={"device_id": "DEV-01", "name": "거실폰", "model": "SM-S911N", "android_version": "14"},
    )
    assert response.status_code == 200
    assert response.json()["device"]["online"] is True

    devices = client.get("/api/devices").json()["devices"]
    assert len(devices) == 1
    assert devices[0]["name"] == "거실폰"

    heartbeat = client.post("/api/devices/DEV-01/heartbeat", json={"device_id": "DEV-01", "battery": 87, "network": "WiFi"})
    assert heartbeat.status_code == 200
    assert client.get("/api/devices").json()["devices"][0]["battery"] == 87

    # 마지막 하트비트가 오래되면 오프라인
    conn = _conn()
    old = (datetime.now(TZ) - timedelta(seconds=config.ONLINE_TIMEOUT_SEC + 60)).isoformat(timespec="seconds")
    conn.execute("UPDATE devices SET last_seen = ? WHERE id = 'DEV-01'", (old,))
    conn.close()
    assert client.get("/api/devices").json()["devices"][0]["online"] is False

    assert client.delete("/api/devices/DEV-01").status_code == 200
    assert client.get("/api/devices").json()["devices"] == []


def test_unknown_device_auto_registers_on_poll(client):
    response = client.get("/api/tasks/poll", params={"device_id": "NEW-99"})
    assert response.status_code == 200
    assert response.json()["task"] is None
    devices = client.get("/api/devices").json()["devices"]
    assert [d["id"] for d in devices] == ["NEW-99"]


# --------------------------------------------------------------------------
# 3. 스케줄 생성 / 번호 순서 / 반복 횟수 확장
# --------------------------------------------------------------------------
def test_schedule_creation_ordered_numbers_and_repeat(client):
    schedule = make_schedule(
        client,
        numbers=["010-1111-0001", "010-1111-0002", "010-1111-0003"],
        repeat_count=2,
    )
    assert schedule["number_count"] == 3
    assert schedule["tasks_per_run"] == 6
    assert schedule["status"] == "active"
    assert schedule["recurrence"] == "once"

    run = client.post(f"/api/schedules/{schedule['id']}/run-now").json()
    tasks = client.get("/api/tasks", params={"schedule_id": schedule["id"], "limit": 50}).json()["tasks"]
    ordered = sorted(tasks, key=lambda t: t["seq"])
    assert [t["phone_number"] for t in ordered] == [
        "010-1111-0001",
        "010-1111-0002",
        "010-1111-0003",
        "010-1111-0001",
        "010-1111-0002",
        "010-1111-0003",
    ]
    assert [t["repeat_index"] for t in ordered] == [1, 1, 1, 2, 2, 2]
    assert all(t["max_call_duration_sec"] == 15 for t in ordered)
    assert all(t["run_id"] == run["run_id"] for t in ordered)


def test_grouped_order_mode(client):
    schedule = make_schedule(
        client,
        numbers=["010-2222-0001", "010-2222-0002"],
        repeat_count=2,
        order_mode="grouped",
    )
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    tasks = client.get("/api/tasks", params={"schedule_id": schedule["id"]}).json()["tasks"]
    ordered = sorted(tasks, key=lambda t: t["seq"])
    assert [t["phone_number"] for t in ordered] == [
        "010-2222-0001",
        "010-2222-0001",
        "010-2222-0002",
        "010-2222-0002",
    ]


def test_sms_schedule_requires_body_per_recipient(client):
    response = client.post(
        "/api/schedules",
        json={"type": "sms", "numbers": ["010-3333-0001"], "start_at": "2026-11-23T09:00"},
    )
    assert response.status_code == 422

    schedule = make_schedule(
        client,
        type="sms",
        numbers=["010-3333-0001"],
        repeat_count=3,
        sms_body="안녕하세요. 테스트 문자입니다.",
        max_call_duration_sec=None,
    )
    assert schedule["sms_body"] == "안녕하세요. 테스트 문자입니다."
    assert schedule["max_call_duration_sec"] is None
    assert schedule["tasks_per_run"] == 3


def test_invalid_phone_number_rejected(client):
    response = client.post(
        "/api/schedules",
        json={"type": "call", "numbers": ["010-1234-5678", "abc"], "start_at": "2026-11-23T09:00"},
    )
    assert response.status_code == 422


def test_numbers_accept_newline_string(client):
    schedule = make_schedule(client, numbers="010-4444-0001\n010-4444-0002\n\n010-4444-0003", repeat_count=1)
    assert schedule["numbers"] == ["010-4444-0001", "010-4444-0002", "010-4444-0003"]


# --------------------------------------------------------------------------
# 4. 다음 실행 시각 계산 (1회/매일/매주/매월)
# --------------------------------------------------------------------------
def test_next_occurrence_rules():
    base = datetime(2026, 11, 22, 9, 30, tzinfo=TZ)
    assert next_occurrence(base, "once") is None
    assert next_occurrence(base, "daily") == datetime(2026, 11, 23, 9, 30, tzinfo=TZ)
    assert next_occurrence(base, "weekly") == datetime(2026, 11, 29, 9, 30, tzinfo=TZ)
    assert next_occurrence(base, "monthly") == datetime(2026, 12, 22, 9, 30, tzinfo=TZ)

    # 말일 보정
    jan31 = datetime(2027, 1, 31, 8, 0, tzinfo=TZ)
    assert add_months(jan31, 1) == datetime(2027, 2, 28, 8, 0, tzinfo=TZ)
    leap = datetime(2028, 1, 31, 8, 0, tzinfo=TZ)
    assert add_months(leap, 1) == datetime(2028, 2, 29, 8, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    "recurrence, expected",
    [
        ("once", None),
        ("daily", datetime(2026, 11, 23, 9, 0, tzinfo=TZ)),
        ("weekly", datetime(2026, 11, 29, 9, 0, tzinfo=TZ)),
        ("monthly", datetime(2026, 12, 22, 9, 0, tzinfo=TZ)),
    ],
)
def test_scheduler_advances_next_run(client, recurrence, expected):
    schedule = make_schedule(client, start_at="2026-11-22T09:00", recurrence=recurrence)
    conn = _conn()
    due = parse_dt("2026-11-22T09:00")
    created = scheduler.run_due_schedules(conn, current=due + timedelta(minutes=1))
    assert len(created) == 1

    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
    if expected is None:
        assert row["next_run_at"] is None
        assert row["status"] == "finished"
    else:
        assert parse_dt(row["next_run_at"]) == expected
        assert row["status"] == "active"
    conn.close()

    # 실행(run)이 작업과 함께 만들어졌는지
    tasks = client.get("/api/tasks", params={"schedule_id": schedule["id"]}).json()["tasks"]
    assert len(tasks) == 6


def test_repeated_missed_schedules_skip_to_future(client):
    schedule = make_schedule(client, start_at="2026-11-20T09:00", recurrence="daily")
    conn = _conn()
    current = parse_dt("2026-11-23T10:00")
    created = scheduler.run_due_schedules(conn, current=current)
    assert len(created) == 1  # 밀린 실행을 몰아서 만들지 않는다
    row = conn.execute("SELECT next_run_at FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
    assert parse_dt(row["next_run_at"]) == datetime(2026, 11, 24, 9, 0, tzinfo=TZ)
    conn.close()


def test_pause_resume(client):
    schedule = make_schedule(client, recurrence="daily", start_at="2026-11-22T09:00")
    paused = client.post(f"/api/schedules/{schedule['id']}/pause").json()["schedule"]
    assert paused["status"] == "paused"
    resumed = client.post(f"/api/schedules/{schedule['id']}/resume").json()["schedule"]
    assert resumed["status"] == "active"
    assert resumed["next_run_at"] is not None


# --------------------------------------------------------------------------
# 5. 작업 폴링 → 결과 보고
# --------------------------------------------------------------------------
def test_task_poll_and_result_flow(client):
    client.post("/api/devices", json={"device_id": "DEV-A", "name": "A폰"})
    schedule = make_schedule(client, numbers=["010-5555-0001", "010-5555-0002"], repeat_count=1)
    run_id = client.post(f"/api/schedules/{schedule['id']}/run-now").json()["run_id"]

    first = client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"]
    assert first["phone_number"] == "010-5555-0001"
    assert first["type"] == "call"
    assert first["max_call_duration_sec"] == 15
    assert first["status"] == "assigned"

    second = client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"]
    assert second["phone_number"] == "010-5555-0002"

    # 다른 기기에는 이미 배정된 작업이 보이지 않는다
    assert client.get("/api/tasks/poll", params={"device_id": "DEV-B"}).json()["task"] is None

    done = client.post(
        f"/api/tasks/{first['id']}/result",
        json={"device_id": "DEV-A", "status": "done", "result": "통화 종료 (15초)", "duration_sec": 15.2},
    )
    assert done.status_code == 200
    assert done.json()["task"]["status"] == "done"

    # 중복 보고는 무시되고 중복 표시
    duplicate = client.post(f"/api/tasks/{first['id']}/result", json={"device_id": "DEV-A", "status": "failed"})
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    client.post(
        f"/api/tasks/{second['id']}/result",
        json={"device_id": "DEV-A", "status": "failed", "error": "통화 연결 실패"},
    )

    runs = client.get("/api/runs").json()["runs"]
    run = [r for r in runs if r["id"] == run_id][0]
    assert run["status"] == "partial"
    assert run["done_tasks"] == 1
    assert run["failed_tasks"] == 1


def test_task_result_rejects_other_device(client):
    client.post("/api/devices", json={"device_id": "DEV-A"})
    schedule = make_schedule(client, numbers=["010-6666-0001"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    task = client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"]
    response = client.post(f"/api/tasks/{task['id']}/result", json={"device_id": "DEV-B", "status": "done"})
    assert response.status_code == 409


def test_device_specific_assignment(client):
    client.post("/api/devices", json={"device_id": "DEV-A"})
    client.post("/api/devices", json={"device_id": "DEV-B"})
    schedule = make_schedule(client, numbers=["010-7777-0001"], repeat_count=1, device_id="DEV-B")
    client.post(f"/api/schedules/{schedule['id']}/run-now")

    assert client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"] is None
    task = client.get("/api/tasks/poll", params={"device_id": "DEV-B"}).json()["task"]
    assert task["device_id"] == "DEV-B"


def test_stale_task_is_recovered(client):
    client.post("/api/devices", json={"device_id": "DEV-A"})
    schedule = make_schedule(client, numbers=["010-8888-0001"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    task = client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"]

    conn = _conn()
    old = (datetime.now(TZ) - timedelta(seconds=config.TASK_TIMEOUT_SEC + 10)).isoformat(timespec="seconds")
    conn.execute("UPDATE tasks SET claimed_at = ? WHERE id = ?", (old, task["id"]))
    scheduler.reap_stale_tasks(conn)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    assert row["status"] == "pending"
    assert row["device_id"] is None
    conn.close()


# --------------------------------------------------------------------------
# 6. 수신 문자 → 텔레그램
# --------------------------------------------------------------------------
def test_incoming_sms_is_stored_and_forwarded(client, monkeypatch):
    client.post("/api/devices", json={"device_id": "DEV-01", "name": "사무실폰"})
    client.post(
        "/api/settings/telegram",
        json={"bot_token": "123456789:TESTTOKEN", "chat_id": "-1001234567890"},
    )

    sent: list[str] = []

    def fake_send(token, chat_id, text, timeout=10.0):
        sent.append(text)
        assert token == "123456789:TESTTOKEN"
        assert chat_id == "-1001234567890"
        return True, None

    monkeypatch.setattr(telegram_client, "send_message", fake_send)

    response = client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-01",
            "sender": "010-9999-8888",
            "body": "주문하신 상품이 배송 출발했습니다.",
            "received_at": "2026-11-22T16:05:00+09:00",
        },
    )
    assert response.status_code == 200
    assert response.json()["id"] >= 1

    assert len(sent) == 1
    message = sent[0]
    assert "수신 문자" in message
    assert "사무실폰 (DEV-01)" in message
    assert "010-9999-8888" in message
    assert "2026-11-22 16:05:00" in message
    assert "주문하신 상품이 배송 출발했습니다." in message

    messages = client.get("/api/sms").json()["messages"]
    assert messages[0]["telegram_status"] == "sent"
    assert messages[0]["body"].startswith("주문하신")


def test_incoming_sms_with_epoch_millis(client, monkeypatch):
    client.post("/api/devices", json={"device_id": "DEV-02", "name": "차량폰"})
    client.post(
        "/api/settings/telegram",
        json={"bot_token": "123456789:TESTTOKEN", "chat_id": "-100999"},
    )
    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: (False, "전송 실패 테스트"))

    response = client.post(
        "/api/sms/incoming",
        json={"device_id": "DEV-02", "sender": "02-123-4567", "body": "테스트", "received_ts": 1795311900000},
    )
    assert response.status_code == 200
    stored = client.get("/api/sms").json()["messages"][0]
    assert stored["telegram_status"] == "pending"  # 재시도 대기
    assert stored["telegram_error"] == "전송 실패 테스트"
    assert stored["received_at"].startswith("2026-1")


def test_telegram_format_contains_all_fields():
    text = telegram_client.format_incoming_sms(
        device_id="DEV-03",
        device_name="",
        sender="010-0000-0000",
        body="안녕",
        received_at="2026-11-22T08:00:00+09:00",
    )
    assert "DEV-03" in text and "010-0000-0000" in text and "2026-11-22 08:00:00" in text and "안녕" in text


def test_telegram_settings_save_and_test(client, monkeypatch):
    saved = client.post(
        "/api/settings/telegram",
        json={"bot_token": "123456789:TESTTOKEN", "chat_id": "555", "sms_forward_enabled": True},
    )
    assert saved.status_code == 200
    assert saved.json()["telegram"]["configured"] is True

    bad = client.post("/api/settings/telegram", json={"bot_token": "not-a-token"})
    assert bad.status_code == 400

    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: (True, None))
    assert client.post("/api/telegram/test", json={}).status_code == 200

    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: (False, "채팅을 찾을 수 없습니다"))
    failed = client.post("/api/telegram/test", json={})
    assert failed.status_code == 400
    assert "채팅을 찾을 수 없습니다" in failed.json()["error"]


def test_sms_skipped_when_telegram_not_configured(client, monkeypatch):
    called = []
    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: called.append(1) or (True, None))
    response = client.post("/api/sms/incoming", json={"device_id": "DEV-X", "sender": "1", "body": "b"})
    assert response.status_code == 200
    assert called == []
    assert client.get("/api/sms").json()["messages"][0]["telegram_status"] == "skipped"


# --------------------------------------------------------------------------
# 7. 대시보드 / 인증 / 통계
# --------------------------------------------------------------------------
def test_dashboard_html_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    for keyword in ("기기 관리", "발신 스케줄", "텔레그램", "수신 문자"):
        assert keyword in html


def test_api_key_protection(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "secret-key")
    assert client.get("/api/devices").status_code == 401
    assert client.get("/api/devices", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/devices", headers={"X-API-Key": "secret-key"}).status_code == 200
    assert client.get("/api/health").status_code == 200  # 상태 확인은 열려 있다


def test_stats_endpoint(client):
    client.post("/api/devices", json={"device_id": "DEV-01", "name": "폰1"})
    make_schedule(client, start_at="2026-11-22T09:00")
    stats = client.get("/api/stats").json()
    assert stats["devices"] == {"total": 1, "online": 1}
    assert stats["schedules"]["active"] == 1
    assert stats["tasks"]["pending"] == 0


def test_schedule_delete_cancels_tasks(client):
    schedule = make_schedule(client, numbers=["010-1234-0001"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 200
    tasks = client.get("/api/tasks", params={"schedule_id": schedule["id"]}).json()["tasks"]
    assert tasks[0]["status"] == "skipped"
    assert client.get("/api/schedules").json()["schedules"] == []


def test_logs_recorded(client):
    client.post("/api/devices", json={"device_id": "DEV-LOG"})
    logs = client.get("/api/logs").json()["logs"]
    assert any("DEV-LOG" in entry["message"] for entry in logs)


def test_expand_tasks_helper():
    assert scheduler.expand_tasks(["A", "B"], 2) == [("A", 1), ("B", 1), ("A", 2), ("B", 2)]
    assert scheduler.expand_tasks(["A", "B"], 2, "grouped") == [("A", 1), ("A", 2), ("B", 1), ("B", 2)]
    assert scheduler.expand_tasks([], 3) == []


# --------------------------------------------------------------------------
# 9. 원격 기기 설정 (대시보드 → 폰, APK 재설치 없이 적용)
# --------------------------------------------------------------------------
DEFAULT_REMOTE = {
    "poll_interval_sec": 5,
    "call_gap_sec": 3,
    "sms_forward_enabled": True,
    "mms_forward_enabled": True,
    "device_enabled": True,
}


def test_settings_returns_remote_defaults(client):
    body = client.get("/api/settings").json()
    assert body["remote"] == DEFAULT_REMOTE
    # 기존 텔레그램 설정 응답은 그대로 유지된다(하위 호환).
    assert "telegram" in body and "server" in body


def test_remote_settings_save_applies_to_heartbeat_and_poll(client):
    client.post("/api/devices", json={"device_id": "DEV-RC", "name": "원격폰"})

    saved = client.post(
        "/api/settings/remote",
        json={"poll_interval_sec": 20, "call_gap_sec": 12, "sms_forward_enabled": False, "mms_forward_enabled": False},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["remote"] == {**DEFAULT_REMOTE, "poll_interval_sec": 20, "call_gap_sec": 12,
                                      "sms_forward_enabled": False, "mms_forward_enabled": False}

    # GET /api/settings 에도 저장값이 보인다.
    assert client.get("/api/settings").json()["remote"]["poll_interval_sec"] == 20
    assert client.get("/api/settings").json()["remote"]["sms_forward_enabled"] is False

    # 하트비트 응답: 최상위 poll_interval_sec + config 블록
    heartbeat = client.post("/api/devices/DEV-RC/heartbeat").json()
    assert heartbeat["poll_interval_sec"] == 20
    assert heartbeat["config"] == {**DEFAULT_REMOTE, "poll_interval_sec": 20, "call_gap_sec": 12,
                                   "sms_forward_enabled": False, "mms_forward_enabled": False}

    # 폴링 응답도 같은 설정을 돌려준다.
    poll = client.get("/api/tasks/poll", params={"device_id": "DEV-RC"}).json()
    assert poll["poll_interval_sec"] == 20
    assert poll["config"] == heartbeat["config"]

    # DB 에 실제로 저장된다(서버 재시작 후에도 유지).
    conn = _conn()
    settings = db.get_settings(conn)
    conn.close()
    assert settings["poll_interval_sec"] == "20"
    assert settings["call_gap_sec"] == "12"
    assert settings["sms_forward_enabled"] == "0"
    assert settings["mms_forward_enabled"] == "0"


def test_remote_settings_partial_update_keeps_other_values(client):
    client.post("/api/settings/remote", json={"poll_interval_sec": 30, "call_gap_sec": 9})
    client.post("/api/settings/remote", json={"mms_forward_enabled": False})
    remote = client.get("/api/settings").json()["remote"]
    assert remote["poll_interval_sec"] == 30
    assert remote["call_gap_sec"] == 9
    assert remote["sms_forward_enabled"] is True
    assert remote["mms_forward_enabled"] is False

    # 빈 본문은 아무것도 바꾸지 않는다.
    assert client.post("/api/settings/remote", json={}).status_code == 200
    assert client.get("/api/settings").json()["remote"] == remote


@pytest.mark.parametrize(
    "payload",
    [
        {"poll_interval_sec": 1},
        {"poll_interval_sec": 301},
        {"call_gap_sec": -1},
        {"call_gap_sec": 601},
        {"sms_forward_enabled": "maybe"},
    ],
)
def test_remote_settings_rejects_out_of_range(client, payload):
    assert client.post("/api/settings/remote", json=payload).status_code == 422
    assert client.get("/api/settings").json()["remote"] == DEFAULT_REMOTE


def test_remote_settings_alias_post_settings(client):
    response = client.post("/api/settings", json={"poll_interval_sec": 7, "call_gap_sec": 0})
    assert response.status_code == 200, response.text
    assert response.json()["remote"]["poll_interval_sec"] == 7
    assert client.get("/api/settings").json()["remote"]["call_gap_sec"] == 0


def test_remote_config_clamps_corrupted_values(client):
    conn = _conn()
    db.set_setting(conn, "poll_interval_sec", "abc")
    db.set_setting(conn, "call_gap_sec", "9999")
    conn.close()
    remote = client.get("/api/settings").json()["remote"]
    assert remote["poll_interval_sec"] == 5   # 기본값으로 복구
    assert remote["call_gap_sec"] == 600      # 상한으로 제한


def test_remote_settings_change_is_logged(client):
    client.post("/api/settings/remote", json={"poll_interval_sec": 11, "call_gap_sec": 4,
                                              "sms_forward_enabled": True, "mms_forward_enabled": False})
    logs = client.get("/api/logs").json()["logs"]
    assert any("원격 기기 설정 저장" in entry["message"] and "11초" in entry["message"] for entry in logs)


def test_disabled_device_is_not_assigned_tasks(client):
    client.post("/api/devices", json={"device_id": "DEV-OFF", "name": "중지폰"})
    schedule = make_schedule(client, numbers=["010-9999-0001", "010-9999-0002"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")

    disabled = client.patch("/api/devices/DEV-OFF", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["device"]["enabled"] is False

    # 중지된 기기는 하트비트는 계속하지만 작업은 받지 않는다.
    heartbeat = client.post("/api/devices/DEV-OFF/heartbeat").json()
    assert heartbeat["ok"] is True
    assert heartbeat["config"]["device_enabled"] is False
    assert heartbeat["pending_tasks"] == 2

    poll = client.get("/api/tasks/poll", params={"device_id": "DEV-OFF"}).json()
    assert poll["task"] is None
    assert poll["config"]["device_enabled"] is False
    assert poll["pending_tasks"] == 2

    tasks = client.get("/api/tasks", params={"schedule_id": schedule["id"]}).json()["tasks"]
    assert [task["status"] for task in tasks] == ["pending", "pending"]

    # 다시 사용 설정하면 정상적으로 작업을 받는다.
    assert client.patch("/api/devices/DEV-OFF", json={"enabled": True}).json()["device"]["enabled"] is True
    task = client.get("/api/tasks/poll", params={"device_id": "DEV-OFF"}).json()
    assert task["task"]["phone_number"] == "010-9999-0001"
    assert task["config"]["device_enabled"] is True


def test_disabled_device_does_not_block_other_devices(client):
    client.post("/api/devices", json={"device_id": "DEV-OFF"})
    client.post("/api/devices", json={"device_id": "DEV-ON"})
    client.patch("/api/devices/DEV-OFF", json={"enabled": False})
    schedule = make_schedule(client, numbers=["010-9999-0100"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")

    task = client.get("/api/tasks/poll", params={"device_id": "DEV-ON"}).json()["task"]
    assert task is not None
    assert task["phone_number"] == "010-9999-0100"
