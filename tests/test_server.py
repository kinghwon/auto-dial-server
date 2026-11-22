"""서버 자동 검증 테스트.

실행:  cd server && ../.venv/bin/python -m pytest -v
"""
from __future__ import annotations

import base64
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, scheduler, telegram_client
from app.main import app
from app.timeutil import TZ, add_months, next_occurrence, parse_dt


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
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
# 6-2. MMS 사진 첨부 전달
# --------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


PHOTO_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-photo-bytes" * 8


def _capture_telegram(monkeypatch):
    """send_message / send_photo / send_document 호출을 기록한다."""
    calls = {"text": [], "photo": [], "document": []}

    def fake_message(token, chat_id, text, timeout=10.0):
        calls["text"].append(text)
        return True, None

    def fake_photo(token, chat_id, image_bytes, filename, caption=None, **kwargs):
        calls["photo"].append({"bytes": image_bytes, "filename": filename, "caption": caption})
        return True, None

    def fake_document(token, chat_id, file_bytes, filename, caption=None, **kwargs):
        calls["document"].append({"bytes": file_bytes, "filename": filename, "caption": caption})
        return True, None

    monkeypatch.setattr(telegram_client, "send_message", fake_message)
    monkeypatch.setattr(telegram_client, "send_photo", fake_photo)
    monkeypatch.setattr(telegram_client, "send_document", fake_document)
    return calls


def _prepare_telegram(client):
    client.post("/api/devices", json={"device_id": "DEV-MMS", "name": "갤23+"})
    client.post(
        "/api/settings/telegram",
        json={"bot_token": "123456789:TESTTOKEN", "chat_id": "-100777"},
    )


def test_incoming_mms_photo_is_forwarded_as_photo(client, monkeypatch):
    _prepare_telegram(client)
    calls = _capture_telegram(monkeypatch)

    response = client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-MMS",
            "sender": "010-1234-5678",
            "body": "[MMS]\n제목: 매장 안내\n사진 보냅니다",
            "received_at": "2026-11-22T20:30:00+09:00",
            "attachments": [
                {"filename": "photo.jpg", "content_type": "image/jpeg", "data_base64": _b64(PHOTO_BYTES)}
            ],
        },
    )
    assert response.status_code == 200, response.text

    stored = client.get("/api/sms").json()["messages"][0]
    assert stored["attachment_count"] == 1
    assert stored["telegram_status"] == "sent"
    assert stored["telegram_error"] is None

    # 짧은 글은 사진 설명(caption)으로 한 번에 나간다(글 메시지 따로 없음).
    assert calls["text"] == []
    assert len(calls["photo"]) == 1
    sent_photo = calls["photo"][0]
    assert sent_photo["bytes"] == PHOTO_BYTES
    assert sent_photo["filename"] == "photo.jpg"
    assert "수신 문자" in sent_photo["caption"]
    assert "갤23+ (DEV-MMS)" in sent_photo["caption"]
    assert "제목: 매장 안내" in sent_photo["caption"]

    # 첨부는 서버 파일로 남고, 대시보드에서 다시 내려받을 수 있다.
    conn = _conn()
    row = conn.execute("SELECT attachment_paths FROM sms_inbox WHERE id = ?", (stored["id"],)).fetchone()
    conn.close()
    saved = json.loads(row["attachment_paths"])
    assert Path(saved[0]["path"]).read_bytes() == PHOTO_BYTES
    assert saved[0]["content_type"] == "image/jpeg"

    download = client.get(f"/api/sms/{stored['id']}/attachments/0")
    assert download.status_code == 200
    assert download.content == PHOTO_BYTES


def test_incoming_mms_long_body_sends_text_then_photo(client, monkeypatch):
    _prepare_telegram(client)
    calls = _capture_telegram(monkeypatch)

    long_body = "[MMS]\n" + ("긴 광고 문구입니다. " * 120)  # 1,000자 초과
    response = client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-MMS",
            "sender": "1588-0000",
            "body": long_body,
            "attachments": [
                {"filename": "a.jpg", "content_type": "image/jpeg", "data_base64": _b64(PHOTO_BYTES)}
            ],
        },
    )
    assert response.status_code == 200
    assert len(calls["text"]) == 1
    assert "긴 광고 문구입니다." in calls["text"][0]
    assert len(calls["photo"]) == 1
    assert calls["photo"][0]["caption"] is None


def test_incoming_mms_multiple_photos_sent_in_order(client, monkeypatch):
    _prepare_telegram(client)
    calls = _capture_telegram(monkeypatch)
    second = PHOTO_BYTES + b"-2"

    client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-MMS",
            "sender": "010-2222-3333",
            "body": "사진 두 장",
            "attachments": [
                {"filename": "1.jpg", "content_type": "image/jpeg", "data_base64": _b64(PHOTO_BYTES)},
                {"filename": "2.png", "content_type": "image/png", "data_base64": _b64(second)},
            ],
        },
    )
    assert [item["filename"] for item in calls["photo"]] == ["1.jpg", "2.png"]
    assert calls["photo"][0]["caption"] is not None
    assert calls["photo"][1]["caption"] is None
    assert client.get("/api/sms").json()["messages"][0]["attachment_count"] == 2


def test_incoming_mms_non_image_attachment_uses_document(client, monkeypatch):
    _prepare_telegram(client)
    calls = _capture_telegram(monkeypatch)

    client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-MMS",
            "sender": "010-4444-5555",
            "body": "계약서 첨부",
            "attachments": [
                {"filename": "contract.pdf", "content_type": "application/pdf", "data_base64": _b64(b"%PDF-1.4 test")}
            ],
        },
    )
    assert len(calls["text"]) == 1
    assert calls["text"][0].count("계약서 첨부") == 1
    assert calls["photo"] == []
    assert len(calls["document"]) == 1
    assert calls["document"][0]["filename"] == "contract.pdf"


def test_sms_without_attachments_keeps_text_only_path(client, monkeypatch):
    _prepare_telegram(client)
    calls = _capture_telegram(monkeypatch)

    client.post("/api/sms/incoming", json={"device_id": "DEV-MMS", "sender": "010-1", "body": "사진 없는 문자"})
    assert len(calls["text"]) == 1
    assert calls["photo"] == []
    assert calls["document"] == []


def test_incoming_mms_broken_attachment_still_forwards_text(client, monkeypatch):
    _prepare_telegram(client)
    calls = _capture_telegram(monkeypatch)

    response = client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-MMS",
            "sender": "010-6",
            "body": "깨진 첨부",
            "attachments": [{"filename": "x.jpg", "content_type": "image/jpeg", "data_base64": "!!!!"}],
        },
    )
    assert response.status_code == 200
    assert client.get("/api/sms").json()["messages"][0]["telegram_status"] == "sent"
    assert len(calls["text"]) == 1
    assert calls["photo"] == []


def test_incoming_sms_rejects_too_many_attachments(client):
    response = client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-MMS",
            "sender": "010-7",
            "body": "첨부 11건",
            "attachments": [
                {"filename": f"{i}.jpg", "content_type": "image/jpeg", "data_base64": _b64(PHOTO_BYTES)}
                for i in range(11)
            ],
        },
    )
    assert response.status_code == 422


def test_resend_sms_uses_saved_photo(client, monkeypatch):
    _prepare_telegram(client)
    calls = _capture_telegram(monkeypatch)

    created = client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-MMS",
            "sender": "010-8",
            "body": "사진 다시 보내기",
            "attachments": [
                {"filename": "again.jpg", "content_type": "image/jpeg", "data_base64": _b64(PHOTO_BYTES)}
            ],
        },
    ).json()

    calls["photo"].clear()
    calls["text"].clear()
    resent = client.post(f"/api/sms/{created['id']}/resend")
    assert resent.status_code == 200 and resent.json()["ok"] is True
    assert len(calls["photo"]) == 1
    assert calls["photo"][0]["bytes"] == PHOTO_BYTES
    assert "사진 다시 보내기" in calls["photo"][0]["caption"]


def test_incoming_sms_openapi_exposes_attachments(client):
    schema = client.get("/openapi.json").json()
    incoming = schema["components"]["schemas"]["SmsIncoming"]
    assert "attachments" in incoming["properties"]
    assert "SmsAttachment" in schema["components"]["schemas"]


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


def test_schedule_delete_soft_deletes_schedule_runs_and_tasks(client):
    """실행된 스케줄을 지워도 행은 보관함에 남고, 목록·대기열에서는 사라진다."""
    schedule = make_schedule(client, numbers=["010-1234-0001"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    task = client.get("/api/tasks").json()["tasks"][0]
    client.post(f"/api/tasks/{task['id']}/result", json={"status": "done", "result": "통화 성공"})

    response = client.delete(f"/api/schedules/{schedule['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == 1
    assert body["deleted_runs"] == 1
    assert body["deleted_tasks"] == 1
    assert body["soft_deleted"] is True

    assert client.get("/api/schedules").json()["schedules"] == []
    assert client.get("/api/tasks").json()["tasks"] == []
    assert client.get("/api/runs").json()["runs"] == []

    conn = _conn()
    try:
        # 행은 지우지 않고 deleted_at 만 채운다(삭제 보관함).
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE schedule_id = ?", (schedule["id"],)).fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM runs WHERE schedule_id = ?", (schedule["id"],)).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE schedule_id = ? AND deleted_at IS NULL", (schedule["id"],)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT status FROM schedules WHERE id = ?", (schedule["id"],)
        ).fetchone()["status"] == "paused"
    finally:
        conn.close()

    archive = client.get("/api/archive", params={"kind": "schedules"}).json()
    assert [item["id"] for item in archive["items"]] == [schedule["id"]]


def test_schedule_delete_with_running_task_leaves_no_orphans(client):
    client.post("/api/devices", json={"device_id": "DEV-DEL"})
    schedule = make_schedule(client, numbers=["010-2222-0001"], repeat_count=1, device_id="DEV-DEL")
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    polled = client.get("/api/tasks/poll", params={"device_id": "DEV-DEL"}).json()["task"]
    assert polled is not None
    client.post(f"/api/tasks/{polled['id']}/start")

    assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 200
    assert client.get("/api/tasks").json()["tasks"] == []
    assert client.get("/api/runs").json()["runs"] == []


def test_schedule_delete_unknown_returns_404(client):
    assert client.delete("/api/schedules/9999").status_code == 404


# --------------------------------------------------------------------------
# 9-1. 스케줄 복사 / 선택 삭제 / 실행했던 스케줄 삭제
# --------------------------------------------------------------------------
def test_schedule_copy_is_paused_and_keeps_settings(client):
    source = make_schedule(
        client,
        name="오전 안내",
        numbers=["010-3333-0001", "010-3333-0002"],
        total_calls=6,
        order_mode="random",
        start_at="2026-11-24T09:00",
        recurrence="daily",
        device_id="DEV-A",
        max_call_duration_sec=45,
    )
    response = client.post(f"/api/schedules/{source['id']}/copy", json={})
    assert response.status_code == 200
    copy = response.json()["schedule"]

    assert copy["id"] != source["id"]
    assert copy["name"] == "오전 안내 (복사)"
    assert copy["status"] == "paused"  # 확인 전에 저절로 발신되지 않게 일시정지로 시작
    assert copy["numbers"] == source["numbers"]
    assert copy["total_calls"] == 6
    assert copy["order_mode"] == "random"
    assert copy["recurrence"] == "daily"
    assert copy["device_id"] == "DEV-A"
    assert copy["max_call_duration_sec"] == 45
    assert copy["start_at"] == source["start_at"]
    assert copy["run_count"] == 0
    assert copy["latest_run"] is None
    assert len(client.get("/api/schedules").json()["schedules"]) == 2


def test_schedule_copy_custom_name_and_enabled(client):
    source = make_schedule(client, name="저녁", start_at="2026-11-25T19:00")
    body = client.post(
        f"/api/schedules/{source['id']}/copy", json={"enabled": True, "name": "저녁 2"}
    ).json()
    copy = body["schedule"]
    assert copy["name"] == "저녁 2"
    assert copy["status"] == "active"
    assert copy["next_run_at"] == source["next_run_at"]


def test_schedule_copy_of_finished_schedule_does_not_fire_again(client):
    schedule = make_schedule(client, numbers=["010-4444-0001"], repeat_count=1, start_at="2026-11-21T09:00")
    client.post("/api/scheduler/tick")
    finished = client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]
    assert finished["status"] == "finished"

    copy = client.post(f"/api/schedules/{schedule['id']}/copy", json={}).json()["schedule"]
    assert copy["status"] == "paused"
    assert copy["next_run_at"] is None
    assert copy["run_count"] == 0

    conn = _conn()
    try:
        created = scheduler.run_due_schedules(conn)
    finally:
        conn.close()
    assert created == []
    runs = client.get("/api/runs").json()["runs"]
    assert all(run["schedule_id"] != copy["id"] for run in runs)


def test_schedule_copy_unknown_returns_404(client):
    assert client.post("/api/schedules/9999/copy", json={}).status_code == 404


def test_bulk_delete_selected_schedules(client):
    first = make_schedule(client, name="A", numbers=["010-5555-0001"], repeat_count=1)
    second = make_schedule(client, name="B", numbers=["010-5555-0002"], repeat_count=1)
    third = make_schedule(client, name="C", numbers=["010-5555-0003"], repeat_count=1)
    client.post(f"/api/schedules/{first['id']}/run-now")
    client.post(f"/api/schedules/{third['id']}/run-now")

    response = client.post(
        "/api/schedules/delete", json={"ids": [first["id"], second["id"], first["id"]]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == 2
    assert body["deleted_runs"] == 1
    assert body["deleted_tasks"] == 1

    assert [s["id"] for s in client.get("/api/schedules").json()["schedules"]] == [third["id"]]
    assert [run["schedule_id"] for run in client.get("/api/runs").json()["runs"]] == [third["id"]]
    assert {task["schedule_id"] for task in client.get("/api/tasks").json()["tasks"]} == {third["id"]}


def test_bulk_delete_executed_only_keeps_unexecuted_and_recurring(client):
    """이미 끝난 스케줄만 지우고, 아직 실행 전이거나 계속 예약되는 스케줄은 남긴다."""
    finished = make_schedule(client, name="끝난 1회", numbers=["010-6666-0001"], repeat_count=1, start_at="2026-11-21T09:00")
    client.post("/api/scheduler/tick")  # 만기 실행 후 종료(finished) 처리
    if client.get(f"/api/schedules/{finished['id']}").json()["schedule"]["status"] != "finished":
        # 서버 시각이 테스트 날짜보다 앞선 경우를 대비해 직접 종료 상태로 맞춘다.
        conn = _conn()
        conn.execute("UPDATE schedules SET status = 'finished', next_run_at = NULL WHERE id = ?", (finished["id"],))
        conn.close()
    waiting = make_schedule(client, name="대기", numbers=["010-6666-0002"], repeat_count=1)
    recurring = make_schedule(
        client,
        name="매일 반복",
        numbers=["010-6666-0003"],
        repeat_count=1,
        recurrence="daily",
        start_at="2026-11-21T09:00",
    )
    client.post("/api/scheduler/tick")
    assert len(client.get("/api/runs").json()["runs"]) >= 2

    response = client.post("/api/schedules/delete", json={"executed_only": True})
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == 1
    assert body["deleted_ids"] == [finished["id"]]

    remaining = {s["id"] for s in client.get("/api/schedules").json()["schedules"]}
    assert remaining == {waiting["id"], recurring["id"]}
    # 남은 스케줄의 실행 기록은 그대로 유지된다.
    assert {run["schedule_id"] for run in client.get("/api/runs").json()["runs"]} >= {recurring["id"]}


def test_bulk_delete_executed_only_without_history_returns_404(client):
    make_schedule(client)
    assert client.post("/api/schedules/delete", json={"executed_only": True}).status_code == 404
    assert len(client.get("/api/schedules").json()["schedules"]) == 1


def test_bulk_delete_all_schedules(client):
    for index in range(3):
        make_schedule(client, name=f"S{index}", numbers=[f"010-7777-000{index}"], repeat_count=1)
    response = client.post("/api/schedules/delete", json={"all_schedules": True})
    assert response.status_code == 200
    assert response.json()["deleted"] == 3
    assert client.get("/api/schedules").json()["schedules"] == []


def test_bulk_delete_schedules_requires_target(client):
    assert client.post("/api/schedules/delete", json={}).status_code == 422
    assert client.post("/api/schedules/delete", json={"ids": []}).status_code == 422


def test_schedule_copy_and_delete_require_api_key(client, monkeypatch):
    schedule = make_schedule(client, numbers=["010-1234-0009"], repeat_count=1)
    monkeypatch.setattr(config, "API_KEY", "secret-key")
    assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 401
    assert client.post("/api/schedules/delete", json={"ids": [schedule["id"]]}).status_code == 401
    assert client.post(f"/api/schedules/{schedule['id']}/copy", json={}).status_code == 401
    assert client.delete("/api/runs/1").status_code == 401
    assert client.post("/api/runs/delete", json={"all_finished": True}).status_code == 401


def test_delete_run_removes_its_tasks(client):
    schedule = make_schedule(client, numbers=["010-8888-0001", "010-8888-0002"], total_calls=2)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    run_id = client.get("/api/runs").json()["runs"][0]["id"]

    response = client.delete(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["deleted_tasks"] == 2
    assert client.get("/api/runs").json()["runs"] == []
    assert client.get("/api/tasks").json()["tasks"] == []
    # 실행 기록만 지우고 스케줄 자체는 남긴다.
    assert [s["id"] for s in client.get("/api/schedules").json()["schedules"]] == [schedule["id"]]


def test_delete_finished_runs_keeps_pending_runs(client):
    schedule = make_schedule(client, numbers=["010-9999-0001"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    first_run = client.get("/api/runs").json()["runs"][0]["id"]
    task = client.get("/api/tasks").json()["tasks"][0]
    client.post(f"/api/tasks/{task['id']}/result", json={"status": "done"})
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    assert len(client.get("/api/runs").json()["runs"]) == 2

    response = client.post("/api/runs/delete", json={"all_finished": True})
    assert response.status_code == 200
    assert response.json()["deleted"] == 1
    remaining = client.get("/api/runs").json()["runs"]
    assert len(remaining) == 1 and remaining[0]["id"] != first_run


def test_delete_run_unknown_returns_404(client):
    assert client.delete("/api/runs/12345").status_code == 404
    assert client.post("/api/runs/delete", json={"ids": [12345]}).status_code == 404


def test_openapi_exposes_schedule_copy_and_delete(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/api/schedules/{schedule_id}/copy"]
    assert "post" in paths["/api/schedules/delete"]
    assert "delete" in paths["/api/schedules/{schedule_id}"]
    assert "delete" in paths["/api/runs/{run_id}"]
    assert "post" in paths["/api/runs/delete"]


def test_dashboard_has_schedule_copy_and_delete_controls(client):
    html = client.get("/").text
    for keyword in (
        "copySchedule(",
        "/copy",
        "scheduleSelectAll",
        "deleteSelectedSchedules",
        "deleteExecutedSchedules",
        "실행했던 스케줄 삭제",
        "/api/schedules/delete",
        "deleteFinishedRuns",
    ):
        assert keyword in html, keyword


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
    # 통화 중 동작 기본값: 녹음 끔 · 통화 볼륨 0 켬 · 내 소리 차단 켬
    "call_recording_enabled": False,
    "call_mute_volume": True,
    "call_mute_mic": True,
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
        {"call_recording_enabled": "maybe"},
        {"call_mute_volume": "maybe"},
        {"call_mute_mic": "maybe"},
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


def test_call_audio_defaults(client):
    """기본값: 녹음 끄기 · 통화 볼륨 0 켜기 · 내 소리 차단 켜기."""
    remote = client.get("/api/settings").json()["remote"]
    assert remote["call_recording_enabled"] is False
    assert remote["call_mute_volume"] is True
    assert remote["call_mute_mic"] is True

    conn = _conn()
    settings = db.get_settings(conn)
    conn.close()
    assert settings["call_recording_enabled"] == "0"
    assert settings["call_mute_volume"] == "1"
    assert settings["call_mute_mic"] == "1"


def test_call_audio_settings_save_and_reach_device(client):
    """통화 중 동작 3가지를 저장하면 하트비트·폴링 config 로 폰에 내려간다."""
    client.post("/api/devices", json={"device_id": "DEV-AUDIO", "name": "통화폰"})

    saved = client.post(
        "/api/settings/remote",
        json={"call_recording_enabled": True, "call_mute_volume": False, "call_mute_mic": False},
    )
    assert saved.status_code == 200, saved.text
    expected = {**DEFAULT_REMOTE, "call_recording_enabled": True, "call_mute_volume": False, "call_mute_mic": False}
    assert saved.json()["remote"] == expected

    heartbeat = client.post("/api/devices/DEV-AUDIO/heartbeat").json()
    assert heartbeat["config"] == expected
    poll = client.get("/api/tasks/poll", params={"device_id": "DEV-AUDIO"}).json()
    assert poll["config"] == expected

    conn = _conn()
    settings = db.get_settings(conn)
    conn.close()
    assert settings["call_recording_enabled"] == "1"
    assert settings["call_mute_volume"] == "0"
    assert settings["call_mute_mic"] == "0"


def test_call_audio_partial_update_keeps_other_values(client):
    client.post("/api/settings/remote", json={"call_recording_enabled": True})
    remote = client.get("/api/settings").json()["remote"]
    assert remote["call_recording_enabled"] is True
    assert remote["call_mute_volume"] is True
    assert remote["call_mute_mic"] is True
    # 기존 설정도 그대로 유지된다.
    assert remote["poll_interval_sec"] == 5
    assert remote["sms_forward_enabled"] is True

    # 통화 설정만 되돌려도 나머지는 유지된다.
    client.post("/api/settings/remote", json={"call_recording_enabled": False})
    remote = client.get("/api/settings").json()["remote"]
    assert remote["call_recording_enabled"] is False
    assert remote["call_mute_volume"] is True


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


# --------------------------------------------------------------------------
# 10. 대기열 부분 삭제 / 전체 삭제
# --------------------------------------------------------------------------
def queue_tasks(client: TestClient, **params):
    query = {"limit": 200, **params}
    return client.get("/api/tasks", params=query).json()["tasks"]


def pending_run(client: TestClient, numbers=("010-7777-0001", "010-7777-0002", "010-7777-0003")):
    schedule = make_schedule(client, numbers=list(numbers), repeat_count=1)
    run_id = client.post(f"/api/schedules/{schedule['id']}/run-now").json()["run_id"]
    return schedule, run_id


def run_of(client: TestClient, run_id: int) -> dict:
    return [row for row in client.get("/api/runs").json()["runs"] if row["id"] == run_id][0]


def test_delete_single_pending_task(client):
    schedule, run_id = pending_run(client)
    tasks = sorted(queue_tasks(client, schedule_id=schedule["id"]), key=lambda t: t["seq"])
    target = tasks[0]

    response = client.delete(f"/api/tasks/{target['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["deleted_count"] == 1
    assert body["deleted_ids"] == [target["id"]]
    assert body["canceled_count"] == 0

    remaining = sorted(queue_tasks(client, schedule_id=schedule["id"]), key=lambda t: t["seq"])
    assert [t["id"] for t in remaining] == [tasks[1]["id"], tasks[2]["id"]]

    # 실행 묶음 집계가 남은 작업 기준으로 다시 계산된다
    run = run_of(client, run_id)
    assert run["total_tasks"] == 2
    assert run["status"] == "pending"

    # 대시보드 통계에도 즉시 반영된다
    assert client.get("/api/stats").json()["tasks"]["pending"] == 2
    # 삭제 이력이 로그에 남는다
    logs = client.get("/api/logs").json()["logs"]
    assert any("대기열 선택 삭제" in entry["message"] for entry in logs)


def test_delete_last_task_in_run_marks_run_canceled(client):
    schedule, run_id = pending_run(client, numbers=["010-7777-0101"])
    task = queue_tasks(client, schedule_id=schedule["id"])[0]

    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    assert queue_tasks(client, schedule_id=schedule["id"]) == []
    run = run_of(client, run_id)
    assert run["status"] == "canceled"
    assert run["total_tasks"] == 0


def test_delete_unknown_task_returns_404(client):
    assert client.delete("/api/tasks/12345").status_code == 404


def test_delete_finished_task_is_rejected(client):
    client.post("/api/devices", json={"device_id": "DEV-DONE"})
    schedule, _ = pending_run(client, numbers=["010-7777-0201"])
    task = client.get("/api/tasks/poll", params={"device_id": "DEV-DONE"}).json()["task"]
    client.post(
        f"/api/tasks/{task['id']}/result",
        json={"device_id": "DEV-DONE", "status": "done", "result": "통화 종료"},
    )

    response = client.delete(f"/api/tasks/{task['id']}")
    assert response.status_code == 200
    assert queue_tasks(client, schedule_id=schedule["id"]) == []


def test_delete_running_task_marks_it_canceled(client):
    client.post("/api/devices", json={"device_id": "DEV-RUN"})
    schedule, run_id = pending_run(client, numbers=["010-7777-0301"])
    task = client.get("/api/tasks/poll", params={"device_id": "DEV-RUN"}).json()["task"]
    client.post(f"/api/tasks/{task['id']}/start")

    response = client.delete(f"/api/tasks/{task['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_count"] == 0
    assert body["canceled_count"] == 1
    assert body["canceled_ids"] == [task["id"]]

    row = queue_tasks(client, schedule_id=schedule["id"])[0]
    assert row["status"] == "skipped"
    assert row["device_id"] is None
    run = run_of(client, run_id)
    assert run["status"] == "canceled"
    assert run["total_tasks"] == 0

    # 폰이 걸고 있던 통화를 마치고 뒤늦게 보고해도 무시된다(중복 처리).
    late = client.post(f"/api/tasks/{task['id']}/result", json={"device_id": "DEV-RUN", "status": "done"})
    assert late.status_code == 200
    assert late.json()["duplicate"] is True


def test_bulk_delete_selected_tasks(client):
    schedule, run_id = pending_run(
        client,
        numbers=["010-7777-0401", "010-7777-0402", "010-7777-0403", "010-7777-0404"],
    )
    tasks = sorted(queue_tasks(client, schedule_id=schedule["id"]), key=lambda t: t["seq"])
    ids = [tasks[0]["id"], tasks[2]["id"]]

    response = client.post("/api/tasks/bulk-delete", json={"task_ids": ids})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_count"] == 2

    remaining = sorted(queue_tasks(client, schedule_id=schedule["id"]), key=lambda t: t["seq"])
    assert [t["phone_number"] for t in remaining] == ["010-7777-0402", "010-7777-0404"]
    run = run_of(client, run_id)
    assert run["status"] == "pending"
    assert run["total_tasks"] == 2


def test_bulk_delete_duplicate_ids_are_counted_once(client):
    schedule, _ = pending_run(client, numbers=["010-7777-0501", "010-7777-0502"])
    task = queue_tasks(client, schedule_id=schedule["id"])[0]
    body = client.post("/api/tasks/bulk-delete", json={"task_ids": [task["id"], task["id"]]}).json()
    assert body["deleted_count"] == 1
    assert body["deleted_ids"] == [task["id"]]


def test_bulk_delete_all_pending_queue(client):
    client.post("/api/devices", json={"device_id": "DEV-KEEP"})
    first, run_a = pending_run(client, numbers=["010-8888-0001", "010-8888-0002"])
    second, run_b = pending_run(client, numbers=["010-8888-0003"])

    # 한 건을 먼저 완료 처리해 둔다(전체 삭제는 이 기록을 건드리지 않아야 한다).
    done_task = client.get("/api/tasks/poll", params={"device_id": "DEV-KEEP"}).json()["task"]
    client.post(
        f"/api/tasks/{done_task['id']}/result",
        json={"device_id": "DEV-KEEP", "status": "done", "duration_sec": 5},
    )

    response = client.post("/api/tasks/bulk-delete", json={"all_pending": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_count"] == 2
    assert body["protected_ids"] == []

    assert queue_tasks(client, status="pending,assigned,running") == []
    done = queue_tasks(client, status="done")
    assert [t["id"] for t in done] == [done_task["id"]]

    assert run_of(client, run_b)["status"] == "canceled"
    run_a_row = run_of(client, run_a)
    assert run_a_row["status"] == "done"  # 완료 1건 + 나머지 삭제
    assert run_a_row["total_tasks"] == 1
    assert run_a_row["done_tasks"] == 1

    # 전체 삭제 후 스케줄은 그대로 남아 있다(다음 회차는 정상 실행).
    schedules = client.get("/api/schedules").json()["schedules"]
    assert {s["id"] for s in schedules} == {first["id"], second["id"]}


def test_bulk_delete_all_pending_within_one_run(client):
    schedule_a, run_a = pending_run(client, numbers=["010-9999-1001", "010-9999-1002"])
    schedule_b, run_b = pending_run(client, numbers=["010-9999-1003"])

    body = client.post("/api/tasks/bulk-delete", json={"all_pending": True, "run_id": run_a}).json()
    assert body["deleted_count"] == 2
    assert queue_tasks(client, schedule_id=schedule_a["id"]) == []
    assert len(queue_tasks(client, schedule_id=schedule_b["id"])) == 1
    assert run_of(client, run_a)["status"] == "canceled"
    assert run_of(client, run_b)["status"] == "pending"


def test_bulk_delete_reports_protected_and_missing(client):
    client.post("/api/devices", json={"device_id": "DEV-MIX"})
    schedule, _ = pending_run(client, numbers=["010-8888-1001", "010-8888-1002"])
    first = client.get("/api/tasks/poll", params={"device_id": "DEV-MIX"}).json()["task"]
    client.post(f"/api/tasks/{first['id']}/result", json={"device_id": "DEV-MIX", "status": "failed"})
    pending = [t for t in queue_tasks(client, schedule_id=schedule["id"]) if t["status"] == "pending"]
    assert len(pending) == 1

    response = client.post(
        "/api/tasks/bulk-delete",
        json={"task_ids": [first["id"], pending[0]["id"], 98765]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_count"] == 2
    assert body["missing_ids"] == [98765]
    assert queue_tasks(client, schedule_id=schedule["id"]) == []


def test_bulk_delete_all_tasks_clears_entire_list(client):
    client.post("/api/devices", json={"device_id": "DEV-ALL"})
    schedule, _ = pending_run(client, numbers=["010-8888-2001", "010-8888-2002"])
    first = client.get("/api/tasks/poll", params={"device_id": "DEV-ALL"}).json()["task"]
    client.post(f"/api/tasks/{first['id']}/result", json={"device_id": "DEV-ALL", "status": "done"})

    response = client.post("/api/tasks/bulk-delete", json={"all_tasks": True})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_count"] == 2
    assert queue_tasks(client, schedule_id=schedule["id"]) == []


def test_bulk_delete_requires_target(client):
    assert client.post("/api/tasks/bulk-delete", json={}).status_code == 422
    assert client.post("/api/tasks/bulk-delete", json={"task_ids": []}).status_code == 422


def test_bulk_delete_nothing_to_delete_is_conflict(client):
    response = client.post("/api/tasks/bulk-delete", json={"all_pending": True})
    assert response.status_code == 409


def test_task_delete_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "secret-key")
    assert client.delete("/api/tasks/1").status_code == 401
    assert client.post("/api/tasks/bulk-delete", json={"all_pending": True}).status_code == 401


def test_dashboard_has_queue_delete_controls(client):
    html = client.get("/").text
    for keyword in ("선택 삭제", "대기열 전체 삭제", "taskSelectAll", "bulk-delete"):
        assert keyword in html


# --------------------------------------------------------------------------
# 20. 총 발신 횟수(total_calls) · 순차/랜덤 순서 · 스케줄 수정(PATCH)
# --------------------------------------------------------------------------
def _tasks_of(client, schedule_id, limit=1000):
    tasks = client.get("/api/tasks", params={"schedule_id": schedule_id, "limit": limit}).json()["tasks"]
    return sorted(tasks, key=lambda t: t["seq"])


@pytest.mark.parametrize("total_calls", [1, 2, 3, 4, 7])
def test_expand_tasks_total_calls_sequence(total_calls):
    numbers = ["A", "B", "C"]
    pairs = scheduler.expand_tasks(numbers, 5, "sequence", total_calls=total_calls)
    assert len(pairs) == total_calls
    assert [number for number, _ in pairs] == [numbers[i % 3] for i in range(total_calls)]
    assert [round_ for _, round_ in pairs] == [i // 3 + 1 for i in range(total_calls)]


def test_expand_tasks_total_calls_overrides_repeat_count():
    # 25개 번호에 총 10건 (번호 수보다 적게)
    numbers = [f"N{i:02d}" for i in range(25)]
    pairs = scheduler.expand_tasks(numbers, 4, "sequence", total_calls=10)
    assert len(pairs) == 10
    assert [number for number, _ in pairs] == numbers[:10]

    # 25개 번호에 총 60건 (번호 수보다 많게)
    pairs = scheduler.expand_tasks(numbers, 1, "sequence", total_calls=60)
    assert len(pairs) == 60
    assert [number for number, _ in pairs] == [numbers[i % 25] for i in range(60)]

    # repeat_count 만 쓰는 기존 방식은 그대로
    assert len(scheduler.expand_tasks(numbers, 4)) == 100
    assert scheduler.expand_tasks(numbers, 4, "sequence", total_calls=None) == scheduler.expand_tasks(numbers, 4)


@pytest.mark.parametrize("total_calls", [2, 3, 4, 7, 25, 60])
def test_expand_tasks_total_calls_random(total_calls):
    numbers = ["A", "B", "C", "D", "E"]
    pairs = scheduler.expand_tasks(numbers, 1, "random", total_calls=total_calls)
    assert len(pairs) == total_calls

    phones = [number for number, _ in pairs]
    assert set(phones) <= set(numbers)
    # 바로 앞 번호가 연속으로 다시 나오지 않는다
    assert all(previous != current for previous, current in zip(phones, phones[1:]))
    assert [round_ for _, round_ in pairs] == [i // 5 + 1 for i in range(total_calls)]

    counts = Counter(phones)
    low, high = total_calls // 5, total_calls // 5 + (1 if total_calls % 5 else 0)
    assert min(counts.values()) >= (low if low else 1)
    assert max(counts.values()) <= max(high, 1)


def test_expand_tasks_random_shuffles_order():
    numbers = [f"N{i:02d}" for i in range(10)]
    pairs = scheduler.expand_tasks(numbers, 1, "random", total_calls=50)
    phones = [number for number, _ in pairs]
    assert set(phones) == set(numbers)
    assert phones != [numbers[i % 10] for i in range(50)]


def test_expand_tasks_random_single_number_and_grouped():
    assert scheduler.expand_tasks(["A"], 1, "random", total_calls=3) == [("A", 1), ("A", 2), ("A", 3)]

    # grouped 는 기존 동작 유지 + 총 건수만큼 잘라내거나 이어 붙인다
    assert scheduler.expand_tasks(["A", "B"], 2, "grouped") == [("A", 1), ("A", 2), ("B", 1), ("B", 2)]
    assert scheduler.expand_tasks(["A", "B"], 2, "grouped", total_calls=3) == [("A", 1), ("A", 2), ("B", 1)]
    assert scheduler.expand_tasks(["A", "B"], 2, "grouped", total_calls=6) == [
        ("A", 1),
        ("A", 2),
        ("B", 1),
        ("B", 2),
        ("A", 3),
        ("A", 4),
    ]


def test_expand_tasks_total_calls_capped_and_empty():
    assert len(scheduler.expand_tasks(["A", "B"], 1, "sequence", total_calls=10**6)) == config.MAX_TOTAL_CALLS
    assert scheduler.expand_tasks([], 3, "sequence", total_calls=5) == []
    assert scheduler.expand_tasks(["A", " "], 1, "random", total_calls=2) == [("A", 1), ("A", 2)]


def test_create_schedule_with_total_calls_and_random_order(client):
    numbers = [f"010-8800-{index:04d}" for index in range(1, 26)]  # 25개
    schedule = make_schedule(client, numbers=numbers, total_calls=60, order_mode="random")
    assert schedule["number_count"] == 25
    assert schedule["total_calls"] == 60
    assert schedule["tasks_per_run"] == 60
    assert schedule["order_mode"] == "random"

    run = client.post(f"/api/schedules/{schedule['id']}/run-now").json()
    assert run["run_id"]
    tasks = _tasks_of(client, schedule["id"])
    assert len(tasks) == 60
    phones = [task["phone_number"] for task in tasks]
    assert set(phones) <= set(numbers)
    # 25개 번호가 60건에 고르게(2~3회) 나뉜다
    assert set(Counter(phones).values()) <= {2, 3}
    # 랜덤 순서지만 같은 번호가 연달아 나가지는 않는다
    assert all(previous != current for previous, current in zip(phones, phones[1:]))
    assert [task["repeat_index"] for task in tasks] == [i // 25 + 1 for i in range(60)]

    # 대시보드 목록에서도 총 건수/순서가 보인다
    listed = client.get("/api/schedules").json()["schedules"][0]
    assert listed["total_calls"] == 60
    assert listed["tasks_per_run"] == 60
    assert listed["order_mode"] == "random"


def test_create_schedule_total_calls_fewer_than_numbers(client):
    numbers = ["010-9900-0001", "010-9900-0002", "010-9900-0003", "010-9900-0004", "010-9900-0005"]
    schedule = make_schedule(client, numbers=numbers, total_calls=3, order_mode="sequence")
    assert schedule["tasks_per_run"] == 3
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    tasks = _tasks_of(client, schedule["id"])
    assert [task["phone_number"] for task in tasks] == numbers[:3]


def test_create_schedule_legacy_repeat_count_has_no_total_calls(client):
    schedule = make_schedule(client, numbers=["010-7700-0001", "010-7700-0002"], repeat_count=3)
    assert schedule["total_calls"] is None
    assert schedule["repeat_count"] == 3
    assert schedule["tasks_per_run"] == 6


@pytest.mark.parametrize("bad_total", [0, -1, config.MAX_TOTAL_CALLS + 1])
def test_total_calls_out_of_range_rejected(client, bad_total):
    response = client.post(
        "/api/schedules",
        json={
            "type": "call",
            "numbers": ["010-1234-0001"],
            "start_at": "2026-11-23T09:00",
            "total_calls": bad_total,
        },
    )
    assert response.status_code == 422


def test_create_schedule_rejects_unknown_order_mode(client):
    response = client.post(
        "/api/schedules",
        json={"type": "call", "numbers": ["010-1234-0001"], "start_at": "2026-11-23T09:00", "order_mode": "shuffle"},
    )
    assert response.status_code == 422


def test_update_schedule_changes_all_fields(client):
    schedule = make_schedule(client, numbers=["010-6600-0001", "010-6600-0002", "010-6600-0003"], repeat_count=2)
    new_numbers = ["010-6600-0011", "010-6600-0012", "010-6600-0013", "010-6600-0014"]

    response = client.patch(
        f"/api/schedules/{schedule['id']}",
        json={
            "name": "수정된 스케줄",
            "numbers": new_numbers,
            "total_calls": 10,
            "order_mode": "random",
            "max_call_duration_sec": 45,
            "device_id": "DEV-9",
            "start_at": "2026-11-25T14:30",
            "recurrence": "weekly",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()["schedule"]
    assert body["id"] == schedule["id"]
    assert body["name"] == "수정된 스케줄"
    assert body["numbers"] == new_numbers
    assert body["number_count"] == 4
    assert body["total_calls"] == 10
    assert body["tasks_per_run"] == 10
    assert body["order_mode"] == "random"
    assert body["max_call_duration_sec"] == 45
    assert body["device_id"] == "DEV-9"
    assert body["recurrence"] == "weekly"
    assert parse_dt(body["start_at"]) == datetime(2026, 11, 25, 14, 30, tzinfo=TZ)
    assert parse_dt(body["next_run_at"]) == datetime(2026, 11, 25, 14, 30, tzinfo=TZ)

    # 수정한 내용대로 실제 작업이 만들어진다
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    tasks = _tasks_of(client, schedule["id"])
    assert len(tasks) == 10
    assert {task["phone_number"] for task in tasks} <= set(new_numbers)
    assert all(task["device_id"] == "DEV-9" for task in tasks)
    assert all(task["max_call_duration_sec"] == 45 for task in tasks)
    assert all(
        previous != current
        for previous, current in zip([t["phone_number"] for t in tasks], [t["phone_number"] for t in tasks][1:])
    )


def test_update_schedule_only_changes_given_fields(client):
    schedule = make_schedule(
        client,
        numbers=["010-5500-0001", "010-5500-0002"],
        repeat_count=2,
        order_mode="grouped",
        start_at="2026-11-23T09:00",
        recurrence="daily",
    )
    updated = client.patch(f"/api/schedules/{schedule['id']}", json={"total_calls": 6}).json()["schedule"]
    assert updated["total_calls"] == 6
    assert updated["tasks_per_run"] == 6
    assert updated["numbers"] == ["010-5500-0001", "010-5500-0002"]
    assert updated["repeat_count"] == 2
    assert updated["order_mode"] == "grouped"
    assert updated["recurrence"] == "daily"
    assert updated["start_at"] == schedule["start_at"]
    assert updated["next_run_at"] == schedule["next_run_at"]

    # total_calls 를 지우면(NULL) 기존 번호×반복 방식으로 돌아간다
    cleared = client.patch(f"/api/schedules/{schedule['id']}", json={"total_calls": None}).json()["schedule"]
    assert cleared["total_calls"] is None
    assert cleared["tasks_per_run"] == 4


def test_update_schedule_recomputes_next_run_on_recurrence(client):
    schedule = make_schedule(client, start_at="2026-11-23T09:00", recurrence="once")
    updated = client.patch(f"/api/schedules/{schedule['id']}", json={"recurrence": "daily"}).json()["schedule"]
    assert updated["recurrence"] == "daily"
    assert parse_dt(updated["next_run_at"]) == datetime(2026, 11, 23, 9, 0, tzinfo=TZ)


def test_update_start_at_reactivates_finished_schedule(client):
    schedule = make_schedule(client, start_at="2026-11-20T09:00", recurrence="once")
    conn = _conn()
    scheduler.run_due_schedules(conn, current=parse_dt("2026-11-20T09:01"))
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
    assert row["status"] == "finished"
    assert row["next_run_at"] is None
    conn.close()

    updated = client.patch(f"/api/schedules/{schedule['id']}", json={"start_at": "2026-11-26T10:00"}).json()["schedule"]
    assert updated["status"] == "active"
    assert parse_dt(updated["next_run_at"]) == datetime(2026, 11, 26, 10, 0, tzinfo=TZ)

    # 상태를 명시해서 보내면 그 값이 우선한다
    paused = client.patch(
        f"/api/schedules/{schedule['id']}",
        json={"start_at": "2026-11-27T10:00", "status": "paused"},
    ).json()["schedule"]
    assert paused["status"] == "paused"
    assert parse_dt(paused["next_run_at"]) == datetime(2026, 11, 27, 10, 0, tzinfo=TZ)


def test_update_schedule_status_patch_compatible(client):
    schedule = make_schedule(client, start_at="2026-11-23T09:00", recurrence="daily")
    paused = client.patch(f"/api/schedules/{schedule['id']}", json={"status": "paused"}).json()["schedule"]
    assert paused["status"] == "paused"
    assert paused["next_run_at"] == schedule["next_run_at"]

    active = client.patch(f"/api/schedules/{schedule['id']}", json={"status": "active"}).json()["schedule"]
    assert active["status"] == "active"
    assert active["next_run_at"] is not None


def test_update_schedule_type_switch(client):
    schedule = make_schedule(client, numbers=["010-4400-0001"], repeat_count=2)

    missing_body = client.patch(f"/api/schedules/{schedule['id']}", json={"type": "sms"})
    assert missing_body.status_code == 400

    switched = client.patch(
        f"/api/schedules/{schedule['id']}",
        json={"type": "sms", "sms_body": "  안내 문자입니다.  "},
    ).json()["schedule"]
    assert switched["type"] == "sms"
    assert switched["sms_body"] == "안내 문자입니다."
    assert switched["max_call_duration_sec"] is None
    assert switched["tasks_per_run"] == 2

    back = client.patch(f"/api/schedules/{schedule['id']}", json={"type": "call"}).json()["schedule"]
    assert back["type"] == "call"
    assert back["sms_body"] is None
    assert back["max_call_duration_sec"] == config.DEFAULT_CALL_MAX_SEC


@pytest.mark.parametrize(
    "payload, status_code",
    [
        ({}, 422),
        ({"name": "x"}, 404),
        ({"numbers": []}, 422),
        ({"numbers": ["abc"]}, 422),
        ({"start_at": "언젠가"}, 422),
        ({"total_calls": 0}, 422),
        ({"total_calls": config.MAX_TOTAL_CALLS + 1}, 422),
        ({"order_mode": "shuffle"}, 422),
        ({"max_call_duration_sec": 0}, 422),
        ({"device_id": None}, 200),
    ],
)
def test_update_schedule_validation(client, payload, status_code):
    schedule = make_schedule(client)
    target = 999999 if status_code == 404 else schedule["id"]
    response = client.patch(f"/api/schedules/{target}", json=payload)
    assert response.status_code == status_code, response.text


def test_update_schedule_is_logged(client):
    schedule = make_schedule(client)
    client.patch(
        f"/api/schedules/{schedule['id']}",
        json={"numbers": ["010-3300-0001"], "total_calls": 4, "order_mode": "random"},
    )
    logs = client.get("/api/logs").json()["logs"]
    assert any("스케줄 수정" in entry["message"] and "random" in entry["message"] for entry in logs)


OLD_SCHEDULES_SCHEMA = """
CREATE TABLE schedules (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT NOT NULL DEFAULT '',
    type                   TEXT NOT NULL CHECK (type IN ('call', 'sms')),
    numbers                TEXT NOT NULL,
    repeat_count           INTEGER NOT NULL DEFAULT 1,
    order_mode             TEXT NOT NULL DEFAULT 'sequence',
    max_call_duration_sec  INTEGER,
    sms_body               TEXT,
    start_at               TEXT NOT NULL,
    recurrence             TEXT NOT NULL DEFAULT 'once',
    device_id              TEXT,
    status                 TEXT NOT NULL DEFAULT 'active',
    next_run_at            TEXT,
    last_run_at            TEXT,
    run_count              INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL
);
"""


def test_db_migration_adds_total_calls_to_existing_db(tmp_path, monkeypatch):
    import sqlite3

    old_db = tmp_path / "old.db"
    raw = sqlite3.connect(old_db)
    raw.executescript(OLD_SCHEDULES_SCHEMA)
    raw.execute(
        """INSERT INTO schedules(name, type, numbers, repeat_count, order_mode, max_call_duration_sec,
                                 start_at, recurrence, status, next_run_at, created_at)
           VALUES ('기존 스케줄', 'call', ?, 2, 'sequence', 30, '2026-11-23T09:00:00+09:00', 'once',
                   'active', '2026-11-23T09:00:00+09:00', '2026-11-20T10:00:00+09:00')""",
        (json.dumps(["010-2200-0001", "010-2200-0002"]),),
    )
    raw.commit()
    raw.close()

    db.init_db(old_db)  # 운영 중인 DB 에 total_calls 컬럼 추가

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
    assert "total_calls" in columns
    row = conn.execute("SELECT * FROM schedules").fetchone()
    assert row["name"] == "기존 스케줄"
    assert row["repeat_count"] == 2
    assert row["total_calls"] is None
    conn.close()

    # 마이그레이션 후에도 기존 스케줄은 예전 방식(번호 × 반복)대로 동작한다
    monkeypatch.setattr(config, "DB_PATH", old_db)
    with TestClient(app) as migrated_client:
        schedules = migrated_client.get("/api/schedules").json()["schedules"]
        assert schedules[0]["total_calls"] is None
        assert schedules[0]["tasks_per_run"] == 4
        run = migrated_client.post(f"/api/schedules/{schedules[0]['id']}/run-now").json()
        tasks = migrated_client.get("/api/tasks", params={"schedule_id": schedules[0]["id"]}).json()["tasks"]
        assert run["run_id"]
        assert len(tasks) == 4

    # 두 번 실행해도 안전(마이그레이션 멱등)
    db.init_db(old_db)
    conn = sqlite3.connect(old_db)
    assert {row[1] for row in conn.execute("PRAGMA table_info(schedules)")} >= {"total_calls"}
    assert conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 1
    conn.close()


def test_dashboard_has_total_calls_edit_mode_and_order_options(client):
    html = client.get("/").text
    for keyword in (
        'id="sTotalCalls"',
        'id="sOrder"',
        "총 통화 횟수",
        "발신량 / 순서",
        "순차",
        "랜덤",
        "수정",
        "스케줄 수정 저장",
        "수정 취소",
        "PATCH",
        "startEditSchedule",
        "cancelScheduleEdit",
    ):
        assert keyword in html, keyword


# --------------------------------------------------------------------------
# 21. 수신 문자 조회(MMS 미리보기) · 삭제
# --------------------------------------------------------------------------
def _post_sms(client, *, device_id="DEV-INBOX", sender="010-2222-3333", body="문자 본문", attachments=None):
    response = client.post(
        "/api/sms/incoming",
        json={
            "device_id": device_id,
            "sender": sender,
            "body": body,
            "received_at": "2026-11-22T21:10:00+09:00",
            "attachments": attachments or [],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["sms"]


def _post_photo_sms(client, *, body="사진 보냅니다"):
    client.post("/api/devices", json={"device_id": "DEV-INBOX", "name": "사무실폰"})
    return _post_sms(
        client,
        body=body,
        attachments=[{"filename": "photo.jpg", "content_type": "image/jpeg", "data_base64": _b64(PHOTO_BYTES)}],
    )


def _attachment_paths(sms_id):
    conn = _conn()
    row = conn.execute("SELECT attachment_paths FROM sms_inbox WHERE id = ?", (sms_id,)).fetchone()
    conn.close()
    return [Path(item["path"]) for item in json.loads(row["attachment_paths"])]


def test_list_sms_returns_attachment_preview_metadata(client):
    created = _post_photo_sms(client)

    data = client.get("/api/sms").json()
    assert data["count"] == 1
    assert data["total"] == 1
    item = data["messages"][0]
    assert item["id"] == created["id"]
    assert item["device_name"] == "사무실폰"
    assert item["has_mms"] is True
    assert item["attachment_count"] == 1

    attachment = item["attachments"][0]
    assert attachment["index"] == 0
    assert attachment["filename"] == "photo.jpg"
    assert attachment["content_type"] == "image/jpeg"
    assert attachment["is_image"] is True
    assert attachment["size"] == len(PHOTO_BYTES)
    assert attachment["url"] == f"/api/sms/{created['id']}/attachments/0"

    # 대시보드가 쓰는 URL 그대로 사진 원본을 받을 수 있다.
    download = client.get(attachment["url"])
    assert download.status_code == 200
    assert download.content == PHOTO_BYTES


def test_list_sms_marks_non_image_attachment(client):
    created = _post_sms(
        client,
        body="계약서 첨부",
        attachments=[{"filename": "contract.pdf", "content_type": "application/pdf", "data_base64": _b64(b"%PDF-1.4 test")}],
    )
    item = client.get("/api/sms").json()["messages"][0]
    attachment = item["attachments"][0]
    assert attachment["is_image"] is False
    assert attachment["filename"] == "contract.pdf"
    assert item["has_mms"] is True
    assert client.get(f"/api/sms/{created['id']}/attachments/0").content == b"%PDF-1.4 test"


def test_list_sms_text_only_has_empty_attachments(client):
    _post_sms(client, body="사진 없는 문자")
    item = client.get("/api/sms").json()["messages"][0]
    assert item["attachments"] == []
    assert item["has_mms"] is False
    assert item["attachment_count"] == 0


def test_list_sms_filter_by_device_counts_total(client):
    _post_sms(client, device_id="DEV-A", body="A 기기 문자")
    _post_sms(client, device_id="DEV-B", body="B 기기 문자")
    _post_sms(client, device_id="DEV-B", body="B 기기 문자 2")

    everything = client.get("/api/sms").json()
    assert everything["total"] == 3
    filtered = client.get("/api/sms", params={"device_id": "DEV-B"}).json()
    assert filtered["total"] == 2
    assert {m["device_id"] for m in filtered["messages"]} == {"DEV-B"}

    limited = client.get("/api/sms", params={"limit": 1}).json()
    assert limited["count"] == 1
    assert limited["total"] == 3


def test_sms_attachment_blocks_file_outside_media_dir(client, tmp_path):
    created = _post_photo_sms(client)
    outside = tmp_path / "secret.txt"
    outside.write_text("서버 밖 파일", encoding="utf-8")

    conn = _conn()
    conn.execute(
        "UPDATE sms_inbox SET attachment_paths = ? WHERE id = ?",
        (
            json.dumps(
                [{"path": str(outside), "filename": "secret.txt", "content_type": "text/plain"}],
                ensure_ascii=False,
            ),
            created["id"],
        ),
    )
    conn.close()

    response = client.get(f"/api/sms/{created['id']}/attachments/0")
    assert response.status_code == 403
    assert "서버 밖 파일" not in response.text


def test_sms_attachment_bad_index_is_404(client):
    created = _post_photo_sms(client)
    assert client.get(f"/api/sms/{created['id']}/attachments/5").status_code == 404
    assert client.get(f"/api/sms/{created['id']}/attachments/-1").status_code == 404
    assert client.get("/api/sms/9999/attachments/0").status_code == 404


def test_delete_single_sms_soft_deletes_and_keeps_attachment_file(client):
    first = _post_photo_sms(client, body="첫 번째 문자")
    second = _post_photo_sms(client, body="두 번째 문자")
    first_files = _attachment_paths(first["id"])
    assert first_files and all(path.is_file() for path in first_files)

    response = client.delete(f"/api/sms/{first['id']}")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True
    assert result["deleted_count"] == 1
    assert result["deleted_ids"] == [first["id"]]
    assert result["soft_deleted"] is True
    # 사진·첨부 파일은 서버에 그대로 남아 보관함에서 다시 볼 수 있다.
    assert result["files_removed"] == 0
    assert result["files_kept"] == len(first_files)
    assert all(path.is_file() for path in first_files)

    remaining = client.get("/api/sms").json()
    assert [m["id"] for m in remaining["messages"]] == [second["id"]]
    assert remaining["total"] == 1

    # 두 번째 문자와 그 사진은 그대로 남는다.
    assert client.get(f"/api/sms/{second['id']}/attachments/0").status_code == 200
    assert all(path.is_file() for path in _attachment_paths(second["id"]))

    # 지운 문자도 보관함에서는 사진까지 볼 수 있고, 다시 지우면 404.
    assert client.get(f"/api/sms/{first['id']}/attachments/0").status_code == 200
    assert client.delete(f"/api/sms/{first['id']}").status_code == 404
    archived = client.get("/api/archive", params={"kind": "sms"}).json()["items"]
    assert [item["id"] for item in archived] == [first["id"]]
    assert archived[0]["has_mms"] is True


def test_delete_sms_without_attachments(client):
    created = _post_sms(client, body="사진 없는 문자")
    result = client.delete(f"/api/sms/{created['id']}").json()
    assert result["deleted_count"] == 1
    assert result["files_removed"] == 0
    assert client.get("/api/sms").json()["total"] == 0


def test_bulk_delete_selected_sms(client):
    ids = [_post_sms(client, body=f"문자 {index}")["id"] for index in range(3)]

    result = client.post("/api/sms/delete", json={"ids": [ids[0], ids[2], ids[0]]}).json()
    assert result["ok"] is True
    assert result["deleted_count"] == 2
    assert sorted(result["deleted_ids"]) == sorted([ids[0], ids[2]])
    assert result["missing_ids"] == []

    remaining = [m["id"] for m in client.get("/api/sms").json()["messages"]]
    assert remaining == [ids[1]]
    assert client.delete(f"/api/sms/{ids[0]}").status_code == 404


def test_bulk_delete_all_sms_keeps_attachment_files_for_archive(client):
    _post_photo_sms(client, body="전체 삭제 1")
    _post_photo_sms(client, body="전체 삭제 2")
    media_dir = config.MEDIA_DIR / "sms"
    assert len(list(media_dir.glob("*"))) == 2

    result = client.post("/api/sms/delete", json={"all_sms": True}).json()
    assert result["deleted_count"] == 2
    assert result["files_removed"] == 0
    assert result["files_kept"] == 2
    assert client.get("/api/sms").json()["total"] == 0
    # 보관함에서 다시 볼 수 있도록 사진 파일은 지우지 않는다.
    assert len(list(media_dir.glob("*"))) == 2

    # 이미 비어 있어도 오류 없이 0건으로 응답한다.
    again = client.post("/api/sms/delete", json={"all_sms": True})
    assert again.status_code == 200
    assert again.json()["deleted_count"] == 0


def test_bulk_delete_reports_missing_ids(client):
    created = _post_sms(client)
    result = client.post("/api/sms/delete", json={"ids": [created["id"], 9999]}).json()
    assert result["deleted_count"] == 1
    assert result["deleted_ids"] == [created["id"]]
    assert result["missing_ids"] == [9999]


def test_bulk_delete_sms_requires_target(client):
    assert client.post("/api/sms/delete", json={}).status_code == 422
    assert client.post("/api/sms/delete", json={"ids": []}).status_code == 422


def test_delete_all_sms_via_http_delete(client):
    _post_photo_sms(client)
    response = client.delete("/api/sms")
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    assert response.json()["files_kept"] == 1
    assert client.get("/api/sms").json()["total"] == 0


def test_sms_delete_requires_api_key(client, monkeypatch):
    created = _post_sms(client)
    monkeypatch.setattr(config, "API_KEY", "secret-key")
    assert client.delete(f"/api/sms/{created['id']}").status_code == 401
    assert client.delete("/api/sms").status_code == 401
    assert client.post("/api/sms/delete", json={"all_sms": True}).status_code == 401

    headers = {"X-API-Key": "secret-key"}
    assert client.delete(f"/api/sms/{created['id']}", headers=headers).status_code == 200


def test_openapi_exposes_sms_view_and_delete_endpoints(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "get" in paths["/api/sms"]
    assert "delete" in paths["/api/sms"]
    assert "delete" in paths["/api/sms/{sms_id}"]
    assert "post" in paths["/api/sms/delete"]
    assert "get" in paths["/api/sms/{sms_id}/attachments/{index}"]

    bulk_schema = spec["components"]["schemas"]["SmsBulkDelete"]["properties"]
    assert "ids" in bulk_schema and "all_sms" in bulk_schema
    for route in ("/api/sms/{sms_id}", "/api/sms"):
        header_params = [p for p in paths[route]["delete"].get("parameters", []) if p.get("in") == "header"]
        assert any(p.get("name") == "X-API-Key" for p in header_params)


def test_dashboard_has_sms_view_and_delete_controls(client):
    html = client.get("/").text
    for keyword in (
        'id="smsTable"',
        'id="smsSelectAll"',
        'id="smsDeleteSelectedBtn"',
        'id="smsDeleteAllBtn"',
        "선택 삭제",
        "수신문자 전체 삭제",
        "/api/sms/delete",
        "deleteSms(",
        "deleteSelectedSms(",
        "deleteAllSms(",
        "openSmsPhoto(",
        "attachmentPreview(",
        "sms-thumb",
        'id="smsPhotoModal"',
        "smsAttachmentBlobUrl",
    ):
        assert keyword in html, keyword


# --------------------------------------------------------------------------
# 수신 번호 (기기 전화번호 + 수신 문자 탭 컬럼)
# --------------------------------------------------------------------------
def test_db_schema_has_phone_and_receiver_columns(isolate_db):
    conn = _conn()
    device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    sms_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sms_inbox)")}
    conn.close()
    assert "phone_number" in device_columns
    assert "receiver_number" in sms_columns


def test_device_phone_number_register_patch_and_clear(client):
    created = client.post(
        "/api/devices",
        json={"device_id": "DEV-PHONE", "name": "사무실폰", "phone_number": "010-1234-5678"},
    ).json()["device"]
    assert created["phone_number"] == "010-1234-5678"

    listed = client.get("/api/devices").json()["devices"][0]
    assert listed["phone_number"] == "010-1234-5678"

    # 앱이 번호 없이 다시 등록(하트비트 대신 재등록)해도 대시보드에서 넣은 번호가 지워지지 않는다
    again = client.post("/api/devices", json={"device_id": "DEV-PHONE", "name": "사무실폰"}).json()["device"]
    assert again["phone_number"] == "010-1234-5678"

    patched = client.patch("/api/devices/DEV-PHONE", json={"phone_number": "02-123-4567"}).json()["device"]
    assert patched["phone_number"] == "02-123-4567"

    cleared = client.patch("/api/devices/DEV-PHONE", json={"phone_number": ""}).json()["device"]
    assert cleared["phone_number"] == ""

    # 형식이 틀린 번호는 저장하지 않는다
    assert client.patch("/api/devices/DEV-PHONE", json={"phone_number": "전화번호아님!!"}).status_code == 422
    assert client.get("/api/devices").json()["devices"][0]["phone_number"] == ""


def test_incoming_sms_stores_explicit_receiver_number(client):
    client.post("/api/devices", json={"device_id": "DEV-R1", "name": "A폰", "phone_number": "010-1000-0001"})
    response = client.post(
        "/api/sms/incoming",
        json={
            "device_id": "DEV-R1",
            "sender": "010-2000-0002",
            "body": "명시한 수신 번호",
            "receiver_number": "010-9999-0000",
        },
    )
    assert response.status_code == 200
    assert response.json()["sms"]["receiver_number"] == "010-9999-0000"

    stored = client.get("/api/sms").json()["messages"][0]
    assert stored["receiver_number"] == "010-9999-0000"

    # 기기 번호(fallback)가 아니라 문자에 기록된 값이 남는다
    conn = _conn()
    row = conn.execute("SELECT receiver_number FROM sms_inbox").fetchone()
    conn.close()
    assert row["receiver_number"] == "010-9999-0000"


def test_incoming_sms_falls_back_to_device_phone_number(client):
    client.post("/api/devices", json={"device_id": "DEV-R2", "name": "B폰", "phone_number": "010-3000-0003"})
    response = client.post(
        "/api/sms/incoming",
        json={"device_id": "DEV-R2", "sender": "010-4000-0004", "body": "번호 없이 도착"},
    )
    assert response.json()["sms"]["receiver_number"] == "010-3000-0003"
    assert client.get("/api/sms").json()["messages"][0]["receiver_number"] == "010-3000-0003"

    conn = _conn()
    row = conn.execute("SELECT receiver_number FROM sms_inbox").fetchone()
    conn.close()
    assert row["receiver_number"] == "010-3000-0003"


def test_incoming_sms_accepts_recipient_alias_and_fills_device_number(client):
    client.post("/api/devices", json={"device_id": "DEV-R3", "name": "C폰"})
    response = client.post(
        "/api/sms/incoming",
        json={"device_id": "DEV-R3", "sender": "010-5000-0005", "body": "recipient 이름", "recipient": "010-7000-0007"},
    )
    assert response.json()["sms"]["receiver_number"] == "010-7000-0007"

    # 앱이 알려 준 수신 번호는 기기 정보에도 채워 둔다 (다음 문자부터 fallback 으로도 보인다)
    device = client.get("/api/devices").json()["devices"][0]
    assert device["phone_number"] == "010-7000-0007"
    assert client.get("/api/sms").json()["messages"][0]["receiver_number"] == "010-7000-0007"


def test_sms_receiver_number_falls_back_when_device_number_added_later(client):
    client.post("/api/sms/incoming", json={"device_id": "DEV-R4", "sender": "1", "body": "먼저 온 문자"})
    first = client.get("/api/sms").json()["messages"][0]
    assert first["receiver_number"] == ""

    # 나중에 대시보드에서 기기 번호를 넣으면 이미 받은 문자에도 수신 번호가 보인다
    client.patch("/api/devices/DEV-R4", json={"phone_number": "010-6000-0006"})
    after = client.get("/api/sms").json()["messages"][0]
    assert after["receiver_number"] == "010-6000-0006"


def test_sms_receiver_number_is_empty_string_for_unknown_number(client):
    client.post("/api/sms/incoming", json={"device_id": "DEV-NONUM", "sender": "1", "body": "번호 미설정"})
    item = client.get("/api/sms").json()["messages"][0]
    assert item["receiver_number"] == ""


def test_db_migration_adds_phone_and_receiver_columns_to_existing_db(tmp_path, monkeypatch):
    import sqlite3

    old_db = tmp_path / "old_sms.db"
    raw = sqlite3.connect(old_db)
    raw.executescript(
        """
        CREATE TABLE devices (
            id               TEXT PRIMARY KEY,
            name             TEXT NOT NULL DEFAULT '',
            model            TEXT DEFAULT '',
            android_version  TEXT DEFAULT '',
            app_version      TEXT DEFAULT '',
            battery          INTEGER,
            network          TEXT DEFAULT '',
            note             TEXT DEFAULT '',
            enabled          INTEGER NOT NULL DEFAULT 1,
            last_seen        TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE TABLE sms_inbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id       TEXT,
            sender          TEXT DEFAULT '',
            body            TEXT DEFAULT '',
            received_at     TEXT,
            created_at      TEXT NOT NULL,
            telegram_status TEXT NOT NULL DEFAULT 'pending',
            telegram_error  TEXT,
            telegram_attempts INTEGER NOT NULL DEFAULT 0,
            telegram_sent_at  TEXT
        );
        """
    )
    raw.execute(
        "INSERT INTO devices(id, name, enabled, created_at) VALUES ('5656', '5656', 1, '2026-11-22T19:07:06+09:00')"
    )
    raw.execute(
        """INSERT INTO sms_inbox(device_id, sender, body, received_at, created_at, telegram_status)
           VALUES ('5656', '01076762164', '기존 문자', '2026-11-22T21:30:22+09:00',
                   '2026-11-22T21:30:24+09:00', 'sent')"""
    )
    raw.commit()
    raw.close()

    db.init_db(old_db)  # 운영 중인 DB(수신 번호 컬럼 없음)에 컬럼 추가

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row
    device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    sms_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sms_inbox)")}
    assert "phone_number" in device_columns
    assert "receiver_number" in sms_columns
    device_row = conn.execute("SELECT * FROM devices").fetchone()
    sms_row = conn.execute("SELECT * FROM sms_inbox").fetchone()
    assert device_row["name"] == "5656"
    assert device_row["phone_number"] == ""
    assert sms_row["body"] == "기존 문자"
    assert sms_row["receiver_number"] == ""
    conn.close()

    # 마이그레이션 후에도 기존 문자는 그대로 보이고, 기기 번호를 넣으면 수신 번호가 채워진다
    monkeypatch.setattr(config, "DB_PATH", old_db)
    with TestClient(app) as migrated_client:
        messages = migrated_client.get("/api/sms").json()["messages"]
        assert messages[0]["body"] == "기존 문자"
        assert messages[0]["receiver_number"] == ""
        migrated_client.patch("/api/devices/5656", json={"phone_number": "010-5656-3939"})
        messages = migrated_client.get("/api/sms").json()["messages"]
        assert messages[0]["receiver_number"] == "010-5656-3939"
        assert messages[0]["device_id"] == "5656"

    # 두 번 실행해도 안전(멱등)
    db.init_db(old_db)
    conn = sqlite3.connect(old_db)
    assert conn.execute("SELECT COUNT(*) FROM sms_inbox").fetchone()[0] == 1
    conn.close()


def test_dashboard_sms_tab_has_receiver_number_column(client):
    html = client.get("/").text
    for keyword in (
        "수신 번호",
        "m.receiver_number",
        "openDeviceEdit(",
        "saveDeviceEdit(",
        'id="devPhone"',
        "phone_number: phoneNumber",
        "state.editingDeviceId",
        "devEditPhone",
        "devEditNote",
    ):
        assert keyword in html, keyword

    # 수신 문자 표: 체크박스 + 수신시각 + 기기 + 수신번호 + 발신번호 + 내용 + 텔레그램 + 액션
    header = html.split('id="smsTable"', 1)[1].split("</thead>", 1)[0]
    assert header.count("</th>") == 8
    assert header.index("수신 번호") < header.index("발신 번호")
    assert 'emptyRow(8, "수신된 문자가 없습니다.' in html

    # 기기 표: 상태 + 기기 + 수신번호 + 모델 + 배터리 + 마지막 연결 + 액션
    device_header = html.split('id="devicesTable"', 1)[1].split("</thead>", 1)[0]
    assert device_header.count("</th>") == 7
    assert 'emptyRow(7, "등록된 기기가 없습니다.' in html


# --------------------------------------------------------------------------
# 22. 스케줄 다중 기기 선택(기기당 같은 횟수) · 기기명/번호 표시
# --------------------------------------------------------------------------
def _register_device(client: TestClient, device_id: str, name: str, phone_number: str = "") -> str:
    response = client.post(
        "/api/devices", json={"device_id": device_id, "name": name, "phone_number": phone_number}
    )
    assert response.status_code == 200, response.text
    return device_id


def _tasks_in_run(client: TestClient, run_id: int) -> list[dict]:
    tasks = client.get("/api/tasks", params={"limit": 1000}).json()["tasks"]
    picked = [task for task in tasks if task["run_id"] == run_id]
    return sorted(picked, key=lambda task: task["seq"])


def test_create_schedule_with_multiple_target_devices(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")

    schedule = make_schedule(client, total_calls=3, target_device_ids=["DEV-A", "DEV-B", "DEV-A"])

    # 중복은 한 번만, 한 대가 아니면 device_id 는 비운다.
    assert schedule["target_device_ids"] == ["DEV-A", "DEV-B"]
    assert schedule["device_id"] is None
    assert schedule["target_device_count"] == 2
    # 기기마다 같은 횟수(3회)를 따로 발신 → 합계 6건
    assert schedule["tasks_per_device"] == 3
    assert schedule["tasks_per_run"] == 6

    devices = {d["device_id"]: d for d in schedule["target_devices"]}
    assert devices["DEV-A"]["name"] == "갤럭시 A"
    assert devices["DEV-A"]["phone_number"] == "010-1111-2222"
    assert devices["DEV-B"]["name"] == "갤럭시 B"
    assert devices["DEV-B"]["phone_number"] == "010-3333-4444"
    assert all(d["online"] for d in schedule["target_devices"])


def test_schedule_single_device_id_is_backwards_compatible(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")

    schedule = make_schedule(client, device_id="DEV-A")

    assert schedule["device_id"] == "DEV-A"
    assert schedule["target_device_ids"] == ["DEV-A"]
    assert schedule["tasks_per_device"] == schedule["tasks_per_run"] == 6
    assert schedule["target_devices"][0]["name"] == "갤럭시 A"


def test_schedule_accepts_target_device_id_alias(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")

    schedule = make_schedule(client, target_device_id="DEV-A")

    assert schedule["target_device_ids"] == ["DEV-A"]
    assert schedule["device_id"] == "DEV-A"


def test_schedule_without_devices_stays_auto_share(client):
    schedule = make_schedule(client)

    assert schedule["target_device_ids"] == []
    assert schedule["target_devices"] == []
    assert schedule["device_id"] is None
    # 자동 배분은 예전처럼 1묶음만 만든다.
    assert schedule["tasks_per_run"] == 6


def test_update_schedule_switches_between_auto_and_devices(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(client, total_calls=2)
    assert schedule["target_device_ids"] == []

    body = client.patch(
        f"/api/schedules/{schedule['id']}", json={"target_device_ids": ["DEV-A", "DEV-B"]}
    ).json()["schedule"]
    assert body["target_device_ids"] == ["DEV-A", "DEV-B"]
    assert body["device_id"] is None
    assert body["tasks_per_device"] == 2
    assert body["tasks_per_run"] == 4

    body = client.patch(
        f"/api/schedules/{schedule['id']}", json={"target_device_ids": ["DEV-B"]}
    ).json()["schedule"]
    assert body["target_device_ids"] == ["DEV-B"]
    assert body["device_id"] == "DEV-B"
    assert body["tasks_per_run"] == 2

    body = client.patch(f"/api/schedules/{schedule['id']}", json={"target_device_ids": []}).json()["schedule"]
    assert body["target_device_ids"] == []
    assert body["device_id"] is None


def test_update_schedule_device_id_replaces_multi_selection(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(client, target_device_ids=["DEV-A", "DEV-B"])

    body = client.patch(f"/api/schedules/{schedule['id']}", json={"device_id": "DEV-A"}).json()["schedule"]

    assert body["target_device_ids"] == ["DEV-A"]
    assert body["device_id"] == "DEV-A"


def test_copy_schedule_keeps_multiple_target_devices(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(client, total_calls=2, target_device_ids=["DEV-A", "DEV-B"])

    copied = client.post(f"/api/schedules/{schedule['id']}/copy", json={}).json()["schedule"]

    assert copied["target_device_ids"] == ["DEV-A", "DEV-B"]
    assert copied["status"] == "paused"
    assert copied["tasks_per_device"] == 2
    assert copied["tasks_per_run"] == 4


def test_multi_device_run_creates_same_amount_per_device(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(
        client,
        numbers=["010-1111-0001", "010-1111-0002", "010-1111-0003"],
        total_calls=4,
        order_mode="sequence",
        target_device_ids=["DEV-A", "DEV-B"],
    )

    run_id = client.post(f"/api/schedules/{schedule['id']}/run-now").json()["run_id"]
    tasks = _tasks_in_run(client, run_id)

    assert len(tasks) == 8
    counts = Counter(task["device_id"] for task in tasks)
    assert counts == {"DEV-A": 4, "DEV-B": 4}
    expected = ["010-1111-0001", "010-1111-0002", "010-1111-0003", "010-1111-0001"]
    for device_id in ("DEV-A", "DEV-B"):
        assert [t["phone_number"] for t in tasks if t["device_id"] == device_id] == expected
    # 같은 실행 안에서 순번은 이어지고 겹치지 않는다.
    assert sorted(task["seq"] for task in tasks) == list(range(1, 9))


def test_multi_device_random_order_is_shuffled_per_device(client, monkeypatch):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    numbers = ["010-1111-0001", "010-1111-0002", "010-1111-0003", "010-1111-0004"]

    calls: list[list[str]] = []

    def fake_shuffle(items):
        calls.append(list(items))
        if len(calls) % 2 == 0:
            items.reverse()

    monkeypatch.setattr(scheduler.random, "shuffle", fake_shuffle)
    schedule = make_schedule(
        client,
        numbers=numbers,
        total_calls=4,
        order_mode="random",
        target_device_ids=["DEV-A", "DEV-B"],
    )

    run_id = client.post(f"/api/schedules/{schedule['id']}/run-now").json()["run_id"]
    tasks = _tasks_in_run(client, run_id)

    assert len(calls) == 2, "기기마다 따로 섞어야 한다"
    first = [t["phone_number"] for t in tasks if t["device_id"] == "DEV-A"]
    second = [t["phone_number"] for t in tasks if t["device_id"] == "DEV-B"]
    assert first == numbers
    assert second == list(reversed(numbers))


def test_multi_device_tasks_are_claimed_only_by_their_device(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(
        client,
        numbers=["010-1111-0001", "010-1111-0002"],
        total_calls=2,
        target_device_ids=["DEV-A", "DEV-B"],
    )
    client.post(f"/api/schedules/{schedule['id']}/run-now")

    for _ in range(2):
        task = client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"]
        assert task["device_id"] == "DEV-A"
    assert client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"] is None

    task_b = client.get("/api/tasks/poll", params={"device_id": "DEV-B"}).json()["task"]
    assert task_b["device_id"] == "DEV-B"


def test_delete_device_cleans_schedule_target_devices(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(client, target_device_ids=["DEV-A", "DEV-B"])

    assert client.delete("/api/devices/DEV-A").status_code == 200
    body = client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]
    assert body["target_device_ids"] == ["DEV-B"]
    assert body["device_id"] == "DEV-B"

    assert client.delete("/api/devices/DEV-B").status_code == 200
    body = client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]
    assert body["target_device_ids"] == []
    assert body["device_id"] is None


def test_deleted_device_still_shows_in_schedule_with_id_name(client):
    conn = _conn()
    conn.execute(
        """INSERT INTO schedules(name, type, numbers, repeat_count, order_mode, max_call_duration_sec,
                                 start_at, recurrence, device_id, target_device_ids, status, created_at)
           VALUES ('남은 스케줄', 'call', ?, 1, 'sequence', 30, '2026-11-23T09:00:00+09:00', 'once',
                   '5656', '["5656"]', 'active', '2026-11-22T20:00:00+09:00')""",
        (json.dumps(["010-1111-0001"]),),
    )
    conn.close()

    schedules = client.get("/api/schedules").json()["schedules"]
    schedule = schedules[0]
    assert schedule["target_device_ids"] == ["5656"]
    assert schedule["target_devices"] == [
        {
            "device_id": "5656",
            "name": "5656",
            "phone_number": "",
            "online": False,
            "enabled": False,
            "missing": True,
        }
    ]


def test_multi_device_volume_over_limit_is_rejected(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")

    response = client.post(
        "/api/schedules",
        json={
            "name": "너무 큰 스케줄",
            "type": "call",
            "numbers": ["010-1111-0001"],
            "total_calls": 4000,
            "start_at": "2026-11-23T09:00",
            "recurrence": "once",
            "target_device_ids": ["DEV-A", "DEV-B"],
        },
    )

    assert response.status_code == 422, response.text
    assert "8000" in response.text or "5000" in response.text


def test_db_migration_adds_target_device_ids_preserving_device_id(tmp_path, monkeypatch):
    import sqlite3

    old_db = tmp_path / "old_targets.db"
    raw = sqlite3.connect(old_db)
    raw.executescript(OLD_SCHEDULES_SCHEMA)
    raw.execute(
        """INSERT INTO schedules(name, type, numbers, repeat_count, order_mode, max_call_duration_sec,
                                 start_at, recurrence, device_id, status, next_run_at, created_at)
           VALUES ('운영 스케줄', 'call', ?, 1, 'random', 300, '2026-11-22T08:54:00+09:00', 'once',
                   '5656', 'finished', NULL, '2026-11-22T08:00:00+09:00')""",
        (json.dumps(["02-2192-0200"]),),
    )
    raw.commit()
    raw.close()

    db.init_db(old_db)  # 운영 DB 에 target_device_ids 컬럼 추가 + device_id 옮겨 담기

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
    assert "target_device_ids" in columns
    row = conn.execute("SELECT * FROM schedules").fetchone()
    assert row["device_id"] == "5656", "기존 지정 기기를 잃으면 안 된다"
    assert json.loads(row["target_device_ids"]) == ["5656"]
    assert row["name"] == "운영 스케줄"
    conn.close()

    # 마이그레이션 후 API 에서도 기기 한 대 지정으로 그대로 보인다
    monkeypatch.setattr(config, "DB_PATH", old_db)
    with TestClient(app) as migrated_client:
        schedule = migrated_client.get("/api/schedules").json()["schedules"][0]
        assert schedule["device_id"] == "5656"
        assert schedule["target_device_ids"] == ["5656"]
        assert schedule["tasks_per_run"] == schedule["tasks_per_device"] == 1

    # 두 번 실행해도 안전(마이그레이션 멱등)
    db.init_db(old_db)
    conn = sqlite3.connect(old_db)
    assert conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 1
    assert json.loads(conn.execute("SELECT target_device_ids FROM schedules").fetchone()[0]) == ["5656"]
    conn.close()


def test_dashboard_has_multi_device_picker_and_device_column(client):
    html = client.get("/").text

    for keyword in (
        'id="schedDevicePicker"',
        'id="schedDeviceAuto"',
        'id="schedDeviceList"',
        'id="schedDeviceHelp"',
        "selectAllScheduleDevices(",
        "renderScheduleDevicePicker(",
        "toggleScheduleDevice(",
        "onDeviceAutoToggle(",
        "scheduleDeviceCell(",
        "target_device_ids",
        "scheduleTargetDeviceIds(",
        "자동 배분",
        "기기당",
        "번호 미설정",
        "선택한",
    ):
        assert keyword in html, keyword

    # 스케줄 목록: 체크박스 + 스케줄 + 유형 + 발신 기기 + 발신량 + 주기/종료일 + 다음 실행
    #            + 보낸 회수 + 최근 실행 + 상태 + 액션
    header = html.split('id="schedulesTable"', 1)[1].split("</thead>", 1)[0]
    assert header.count("</th>") == 11
    assert header.index("발신 기기") < header.index("발신량")

    # 기기를 고르면 각 기기에서 같은 횟수씩 발신한다는 안내
    assert "각각" in html and "회씩" in html
    assert 'emptyRow(11, "등록된 스케줄이 없습니다.' in html


def test_pause_and_resume_keep_multiple_target_devices(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(client, target_device_ids=["DEV-A", "DEV-B"])

    paused = client.post(f"/api/schedules/{schedule['id']}/pause").json()["schedule"]
    assert paused["status"] == "paused"
    assert paused["target_device_ids"] == ["DEV-A", "DEV-B"]

    resumed = client.post(f"/api/schedules/{schedule['id']}/resume").json()["schedule"]
    assert resumed["target_device_ids"] == ["DEV-A", "DEV-B"]
    assert resumed["device_id"] is None

    # 다른 항목만 고치는 PATCH 도 기기 목록을 건드리지 않는다
    patched = client.patch(f"/api/schedules/{schedule['id']}", json={"name": "이름만 변경"}).json()["schedule"]
    assert patched["target_device_ids"] == ["DEV-A", "DEV-B"]


# --------------------------------------------------------------------------
# 23. 서버 활동 로그: 검색 · 분류 · 기간 · 전체 보기 / 페이지 나누기
# --------------------------------------------------------------------------
def _seed_logs(entries):
    """(메시지, 레벨, 작성시각) 목록을 DB 에 직접 넣는다(시간 필터 검증용)."""
    conn = _conn()
    for message, level, created in entries:
        db.log(conn, message, level)
        conn.execute("UPDATE logs SET created_at = ? WHERE id = last_insert_rowid()", (created,))
    conn.close()


LOG_SEED = [
    ("기기 등록: ALPHA-1 (사무실폰)", "info", "2026-11-21T09:00:00+09:00"),
    ("스케줄 등록: '아침 안내' call 총 3건", "info", "2026-11-21T10:30:00+09:00"),
    ("수신 문자 저장: 010-2222-3333 → 사무실폰 (id=1)", "info", "2026-11-21T23:30:00+09:00"),
    ("대기열 전체 삭제(보관함): 2건 보관함으로 이동", "warning", "2026-11-22T08:10:00+09:00"),
]


def test_logs_search_q_matches_message_category_and_level(client):
    _seed_logs(LOG_SEED)

    # 영문 대소문자 구분 없이 메시지 검색
    data = client.get("/api/logs", params={"q": "alpha"}).json()
    assert [log["message"] for log in data["logs"]] == [LOG_SEED[0][0]]
    assert data["total"] == 1 and data["count"] == 1

    # 한글 메시지 검색
    data = client.get("/api/logs", params={"q": "아침 안내"}).json()
    assert data["total"] == 1 and data["logs"][0]["category"] == "schedule"

    # 분류 이름(schedule)으로도 찾힌다
    data = client.get("/api/logs", params={"q": "schedule"}).json()
    assert data["total"] == 1 and data["logs"][0]["category"] == "schedule"

    # 레벨 이름(warning)으로도 찾힌다
    data = client.get("/api/logs", params={"q": "warning"}).json()
    assert data["total"] == 1 and data["logs"][0]["level"] == "warning"

    # 아무 것도 맞지 않으면 0건
    assert client.get("/api/logs", params={"q": "없는검색어"}).json()["total"] == 0


def test_logs_filter_by_level_and_category(client):
    _seed_logs(LOG_SEED)

    warnings = client.get("/api/logs", params={"level": "warning"}).json()
    assert warnings["total"] == 1
    assert warnings["logs"][0]["level"] == "warning"

    devices = client.get("/api/logs", params={"category": "device"}).json()
    assert devices["total"] == 1
    assert devices["logs"][0]["category_label"] == "기기"

    both = client.get("/api/logs", params={"category": "device", "level": "warning"}).json()
    assert both["total"] == 0

    bad = client.get("/api/logs", params={"category": "nope"})
    assert bad.status_code == 400


def test_logs_time_range_start_and_end(client):
    _seed_logs(LOG_SEED)

    middle = client.get(
        "/api/logs",
        params={"start": "2026-11-21T10:00", "end": "2026-11-21T23:00"},
    ).json()
    assert [log["message"] for log in middle["logs"]] == [LOG_SEED[1][0]]

    # 종료 날짜만 주면 그날 23:59:59 까지 포함한다.
    day = client.get("/api/logs", params={"start": "2026-11-21", "end": "2026-11-21"}).json()
    assert day["total"] == 3
    assert day["logs"][0]["message"] == LOG_SEED[2][0]  # 최신(23:30)이 먼저

    after = client.get("/api/logs", params={"start": "2026-11-22T00:00"}).json()
    assert after["total"] == 1 and after["logs"][0]["level"] == "warning"

    # ISO 오프셋(UTC)으로 줘도 KST 기준으로 해석한다.
    utc = client.get("/api/logs", params={"start": "2026-11-21T01:00:00Z", "end": "2026-11-21T02:00:00Z"}).json()
    assert utc["total"] == 1 and utc["logs"][0]["message"] == LOG_SEED[1][0]


def test_logs_invalid_time_and_reversed_range_are_400(client):
    _seed_logs(LOG_SEED)
    assert client.get("/api/logs", params={"start": "어제"}).status_code == 400
    assert client.get("/api/logs", params={"end": "2026-13-45T99:99"}).status_code == 400
    reversed_range = client.get(
        "/api/logs", params={"start": "2026-11-22T00:00", "end": "2026-11-21T00:00"}
    )
    assert reversed_range.status_code == 400
    assert "늦습니다" in reversed_range.json()["detail"]


def test_logs_limit_offset_total_and_all(client):
    _seed_logs(LOG_SEED + [(f"기기 등록: EXTRA-{index}", "info", f"2026-11-22T10:0{index}:00+09:00") for index in range(3)])

    page = client.get("/api/logs", params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 7
    assert page["count"] == 2 and len(page["logs"]) == 2
    assert page["offset"] == 1 and page["has_more"] is True
    # 최신순 정렬이라 id 가 큰 것부터, offset 만큼 건너뛴다.
    assert page["logs"][0]["message"] == "기기 등록: EXTRA-1"
    assert page["logs"][1]["message"] == "기기 등록: EXTRA-0"

    everyone = client.get("/api/logs", params={"all": True}).json()
    assert everyone["total"] == 7 and everyone["count"] == 7
    assert everyone["all"] is True and everyone["truncated"] is False

    zero_limit = client.get("/api/logs", params={"limit": 0}).json()
    assert zero_limit["count"] == 7 and zero_limit["all"] is True

    last = client.get("/api/logs", params={"limit": 100, "offset": 5}).json()
    assert last["count"] == 2 and last["has_more"] is False

    # 기본 limit 은 200 이고, 조건에 맞는 전체 건수를 total 로 알려 준다.
    default = client.get("/api/logs").json()
    assert default["limit"] == 200
    assert default["total"] == 7


def test_logs_have_category_and_full_timestamp(client):
    _seed_logs(LOG_SEED)
    log = client.get("/api/logs", params={"q": "ALPHA-1"}).json()["logs"][0]
    assert log["category"] == "device"
    assert log["category_label"] == "기기"
    assert log["created_at_full"] == "2026-11-21 09:00:00"
    assert log["created_at_text"] == "11-21 09:00:00"


# --------------------------------------------------------------------------
# 24. 기기 정보 편집(이름·번호·메모·사용 여부) · 앱 등록이 덮어쓰지 않음
# --------------------------------------------------------------------------
def test_device_patch_edits_name_phone_note_and_enabled(client):
    client.post("/api/devices", json={"device_id": "DEV-EDIT", "name": "앱이름"})

    body = client.patch(
        "/api/devices/DEV-EDIT",
        json={"name": "사무실폰", "phone_number": "010-4444-5555", "note": "유심 2번", "enabled": False},
    ).json()["device"]
    assert body["name"] == "사무실폰"
    assert body["phone_number"] == "010-4444-5555"
    assert body["note"] == "유심 2번"
    assert body["enabled"] is False
    assert body["name_customized"] is True
    assert body["phone_customized"] is True

    # 이름만 고쳐도 번호·메모는 그대로 남는다.
    body = client.patch("/api/devices/DEV-EDIT", json={"name": "거실폰"}).json()["device"]
    assert (body["name"], body["phone_number"], body["note"]) == ("거실폰", "010-4444-5555", "유심 2번")
    assert client.patch("/api/devices/NOPE", json={"name": "x"}).status_code == 404


def test_device_customized_name_and_phone_survive_app_registration(client):
    client.post(
        "/api/devices",
        json={"device_id": "DEV-KEEP", "name": "앱이 알려준 이름", "phone_number": "010-1111-0001"},
    )
    client.patch(
        "/api/devices/DEV-KEEP",
        json={"name": "내가 고친 이름", "phone_number": "010-2222-0002", "note": "책상 위"},
    )

    # 앱은 30초마다 등록 요청을 보낸다(이름·번호가 예전 값, 번호는 빈 값일 수도 있다).
    for payload in (
        {"device_id": "DEV-KEEP", "name": "앱이 알려준 이름", "phone_number": ""},
        {"device_id": "DEV-KEEP", "name": "다른 이름", "phone_number": "010-9999-9999"},
    ):
        device = client.post("/api/devices", json=payload).json()["device"]
        assert device["name"] == "내가 고친 이름"
        assert device["phone_number"] == "010-2222-0002"
        assert device["note"] == "책상 위"
        assert device["name_customized"] is True and device["phone_customized"] is True


def test_device_unmodified_name_follows_app_registration(client):
    client.post("/api/devices", json={"device_id": "DEV-FOLLOW", "name": "예전 이름"})
    device = client.post("/api/devices", json={"device_id": "DEV-FOLLOW", "name": "새 이름"}).json()["device"]
    assert device["name"] == "새 이름"
    assert device["name_customized"] is False


def test_device_phone_cleared_by_user_is_not_refilled_by_incoming_sms(client):
    client.post("/api/devices", json={"device_id": "DEV-NUM", "name": "폰", "phone_number": "010-1111-0001"})
    client.patch("/api/devices/DEV-NUM", json={"phone_number": ""})

    client.post(
        "/api/sms/incoming",
        json={"device_id": "DEV-NUM", "sender": "010-2", "body": "문자", "receiver_number": "010-7777-8888"},
    )
    device = client.get("/api/devices").json()["devices"][0]
    assert device["phone_number"] == ""
    assert device["phone_customized"] is True
    # 문자에는 앱이 알려준 수신 번호가 그대로 보인다.
    assert client.get("/api/sms").json()["messages"][0]["receiver_number"] == "010-7777-8888"


def test_device_patch_edits_model_and_android_version(client):
    client.post(
        "/api/devices",
        json={"device_id": "DEV-SPEC", "name": "폰", "model": "samsung SM-S916N", "android_version": "17"},
    )

    body = client.patch(
        "/api/devices/DEV-SPEC", json={"model": "Galaxy S23 (내가 고침)", "android_version": "14"}
    ).json()["device"]
    assert body["model"] == "Galaxy S23 (내가 고침)"
    assert body["android_version"] == "14"
    assert body["model_customized"] is True and body["android_customized"] is True
    # 모델·버전만 고쳐도 다른 항목은 그대로다.
    assert body["name"] == "폰" and body["enabled"] is True

    # 앱은 30초마다 등록 정보를 다시 보낸다. 직접 고친 모델·버전은 덮어쓰지 않는다.
    for payload in (
        {"device_id": "DEV-SPEC", "model": "samsung SM-S916N", "android_version": "17"},
        {"device_id": "DEV-SPEC"},
    ):
        device = client.post("/api/devices", json=payload).json()["device"]
        assert device["model"] == "Galaxy S23 (내가 고침)"
        assert device["android_version"] == "14"
        assert device["model_customized"] is True and device["android_customized"] is True

    # 비워 보내면 직접 지정을 풀어 앱이 알려주는 값으로 돌아간다.
    body = client.patch("/api/devices/DEV-SPEC", json={"model": "", "android_version": ""}).json()["device"]
    assert (body["model"], body["android_version"]) == ("", "")
    assert body["model_customized"] is False and body["android_customized"] is False
    device = client.post(
        "/api/devices", json={"device_id": "DEV-SPEC", "model": "samsung SM-S916N", "android_version": "17"}
    ).json()["device"]
    assert (device["model"], device["android_version"]) == ("samsung SM-S916N", "17")


def test_device_rename_moves_schedules_tasks_and_sms(client):
    _register_device(client, "DEV-OLD", "옛 폰", "010-1111-2222")
    schedule = make_schedule(
        client, numbers=["010-1111-0001", "010-1111-0002"], total_calls=2, target_device_ids=["DEV-OLD"]
    )
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    client.post("/api/sms/incoming", json={"device_id": "DEV-OLD", "sender": "010-2", "body": "옛 번호로 온 문자"})

    body = client.patch("/api/devices/DEV-OLD", json={"new_device_id": "DEV-NEW", "name": "새 폰"}).json()
    assert body["renamed_from"] == "DEV-OLD"
    assert body["device"]["id"] == "DEV-NEW"
    assert body["device"]["name"] == "새 폰"
    assert [d["id"] for d in client.get("/api/devices").json()["devices"]] == ["DEV-NEW"]

    moved = client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]
    assert moved["device_id"] == "DEV-NEW"
    assert moved["target_device_ids"] == ["DEV-NEW"]
    assert moved["target_devices"][0]["device_id"] == "DEV-NEW"
    assert moved["target_devices"][0]["name"] == "새 폰"
    assert [t["device_id"] for t in client.get("/api/tasks").json()["tasks"]] == ["DEV-NEW", "DEV-NEW"]
    message = client.get("/api/sms").json()["messages"][0]
    assert message["device_id"] == "DEV-NEW"
    assert message["device_name"] == "새 폰"

    # 새 ID 로 작업을 받고, 옛 ID 로는 받지 않는다.
    assert client.get("/api/tasks/poll", params={"device_id": "DEV-OLD"}).json()["task"] is None
    task = client.get("/api/tasks/poll", params={"device_id": "DEV-NEW"}).json()["task"]
    assert task["device_id"] == "DEV-NEW"


def test_device_rename_keeps_multi_device_schedule_list(client):
    _register_device(client, "DEV-A", "갤럭시 A")
    _register_device(client, "DEV-B", "갤럭시 B")
    schedule = make_schedule(client, target_device_ids=["DEV-A", "DEV-B"])

    client.patch("/api/devices/DEV-A", json={"new_device_id": "DEV-A2"})
    body = client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]
    # 순서를 유지한 채 바뀐 기기만 새 ID 로 바뀐다.
    assert body["target_device_ids"] == ["DEV-A2", "DEV-B"]
    assert body["device_id"] is None


def test_device_rename_rejects_duplicate_or_bad_id(client):
    _register_device(client, "DEV-A", "A")
    _register_device(client, "DEV-B", "B")

    response = client.patch("/api/devices/DEV-A", json={"new_device_id": "DEV-B"})
    assert response.status_code == 409
    assert "DEV-B" in response.json()["detail"]
    assert {d["id"] for d in client.get("/api/devices").json()["devices"]} == {"DEV-A", "DEV-B"}
    assert client.get("/api/devices").json()["devices"][0]["name"] == "A"

    for bad in ("BAD/ID", "BAD\\ID"):
        assert client.patch("/api/devices/DEV-A", json={"new_device_id": bad}).status_code == 422
    assert client.patch("/api/devices/NOPE", json={"new_device_id": "X"}).status_code == 404

    # 같은 ID 를 보내면 바꿀 것 없이 그대로 둔다.
    body = client.patch("/api/devices/DEV-A", json={"new_device_id": "DEV-A"}).json()
    assert body["device"]["id"] == "DEV-A"
    assert body["renamed_from"] is None


# --------------------------------------------------------------------------
# 25. 소프트 삭제(삭제 보관함) · 복원
# --------------------------------------------------------------------------
def _deleted_at_of(table, row_id):
    conn = _conn()
    row = conn.execute(f"SELECT deleted_at FROM {table} WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    return row["deleted_at"] if row else None


def _archive(client, **params):
    return client.get("/api/archive", params=params).json()


def test_soft_delete_hides_every_list_and_stats(client):
    schedule = make_schedule(client, numbers=["010-5000-0001"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    run_id = client.get("/api/runs").json()["runs"][0]["id"]
    task_id = client.get("/api/tasks").json()["tasks"][0]["id"]
    sms = _post_sms(client, body="보관함 테스트")

    assert client.get("/api/stats").json()["tasks"]["pending"] == 1
    assert client.get("/api/stats").json()["sms"]["today"] == 1

    assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 200
    assert client.delete(f"/api/sms/{sms['id']}").status_code == 200

    assert client.get("/api/schedules").json()["schedules"] == []
    assert client.get("/api/runs").json()["runs"] == []
    assert client.get("/api/tasks").json()["tasks"] == []
    assert client.get("/api/sms").json()["total"] == 0
    stats = client.get("/api/stats").json()
    assert stats["tasks"]["pending"] == 0
    assert stats["sms"]["today"] == 0

    # 행은 남아 있고 deleted_at 만 채워진다.
    assert _deleted_at_of("schedules", schedule["id"])
    assert _deleted_at_of("runs", run_id)
    assert _deleted_at_of("tasks", task_id)
    assert _deleted_at_of("sms_inbox", sms["id"])

    # 보관함에는 네 종류가 모두 보인다.
    data = _archive(client)
    assert {item["kind"] for item in data["items"]} == {"schedule", "run", "task", "sms"}
    assert data["counts"] == {"schedules": 1, "runs": 1, "tasks": 1, "sms": 1, "total": 4}


def test_schedule_soft_delete_cancels_tasks_so_phone_never_dials(client):
    client.post("/api/devices", json={"device_id": "DEV-SOFT"})
    schedule = make_schedule(client, numbers=["010-5000-0101"], repeat_count=1, device_id="DEV-SOFT")
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    first = client.get("/api/tasks/poll", params={"device_id": "DEV-SOFT"}).json()["task"]
    assert first is not None
    client.post(f"/api/tasks/{first['id']}/start")

    body = client.delete(f"/api/schedules/{schedule['id']}").json()
    assert body["canceled_tasks"] == 1

    polled = client.get("/api/tasks/poll", params={"device_id": "DEV-SOFT"}).json()
    assert polled["task"] is None
    assert polled["pending_tasks"] == 0

    conn = _conn()
    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (first["id"],)).fetchone()["status"]
    conn.close()
    assert status == "skipped"

    # 이미 발신 중이던 폰이 뒤늦게 결과를 보고해도 되살아나지 않는다.
    assert client.post(
        f"/api/tasks/{first['id']}/result", json={"device_id": "DEV-SOFT", "status": "done"}
    ).status_code == 404


def test_deleted_schedule_is_not_picked_up_by_scheduler(client):
    schedule = make_schedule(client, numbers=["010-5000-0201"], repeat_count=1, start_at="2026-11-20T09:00")
    client.delete(f"/api/schedules/{schedule['id']}")

    conn = _conn()
    try:
        created = scheduler.run_due_schedules(conn, parse_dt("2026-11-22T12:00:00"))
        assert created == []
        assert conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"] == 0
        assert conn.execute("SELECT status FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()["status"] == "paused"
    finally:
        conn.close()


def test_archive_search_and_kind_filters(client):
    first = make_schedule(client, name="보관함 검색 알파", numbers=["010-5000-0301"], repeat_count=1)
    second = make_schedule(client, name="보관함 검색 베타", numbers=["010-5000-0302"], repeat_count=1)
    client.delete(f"/api/schedules/{first['id']}")
    client.delete(f"/api/schedules/{second['id']}")
    sms = _post_sms(client, sender="010-8888-0001", body="보관함에 남는 문자")

    found = _archive(client, kind="schedules", q="알파")
    assert [item["id"] for item in found["items"]] == [first["id"]]
    assert found["items"][0]["title"] == "보관함 검색 알파"
    assert found["items"][0]["kind_label"] == "스케줄"

    client.delete(f"/api/sms/{sms['id']}")
    # 번호로도 문자를 찾을 수 있다.
    by_number = _archive(client, kind="sms", q="8888")
    assert [item["id"] for item in by_number["items"]] == [sms["id"]]

    assert _archive(client, kind="runs")["items"] == []
    assert _archive(client, kind="bogus").get("detail")


def test_archive_kind_filter_and_counts(client):
    schedule = make_schedule(client, numbers=["010-5000-0401"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    sms = _post_sms(client, body="보관함 개수")
    client.delete(f"/api/schedules/{schedule['id']}")
    client.delete(f"/api/sms/{sms['id']}")

    runs = _archive(client, kind="runs")
    assert [item["kind"] for item in runs["items"]] == ["run"]
    assert runs["counts"]["schedules"] == 1 and runs["counts"]["sms"] == 1
    assert runs["counts"]["total"] == 4

    tasks = _archive(client, kind="tasks")
    assert [item["kind"] for item in tasks["items"]] == ["task"]


def test_archive_time_range_uses_item_time(client):
    old = _post_sms(client, body="어제 받은 문자")
    new = _post_sms(client, body="오늘 받은 문자")
    conn = _conn()
    conn.execute("UPDATE sms_inbox SET received_at = ? WHERE id = ?", ("2026-11-21T08:30:00+09:00", old["id"]))
    conn.execute("UPDATE sms_inbox SET received_at = ? WHERE id = ?", ("2026-11-22T08:30:00+09:00", new["id"]))
    conn.close()
    client.post("/api/sms/delete", json={"all_sms": True})

    day = _archive(client, kind="sms", start="2026-11-21", end="2026-11-21")
    assert [item["id"] for item in day["items"]] == [old["id"]]
    assert _archive(client, kind="sms", start="2026-11-22")["counts"]["sms"] == 2
    assert [item["id"] for item in _archive(client, kind="sms", start="2026-11-22")["items"]] == [new["id"]]


def test_archive_restore_schedule_brings_back_paused_with_children(client):
    schedule = make_schedule(client, numbers=["010-5000-0501", "010-5000-0502"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    task = client.get("/api/tasks").json()["tasks"][0]
    client.post(f"/api/tasks/{task['id']}/result", json={"status": "done", "result": "통화 성공"})
    client.delete(f"/api/schedules/{schedule['id']}")

    result = client.post("/api/archive/restore", json={"kind": "schedule", "id": schedule["id"]}).json()
    assert result["ok"] is True
    assert result["restored"] == 1
    assert result["restored_runs"] == 1
    assert result["restored_tasks"] == 2

    restored = client.get("/api/schedules").json()["schedules"]
    assert [s["id"] for s in restored] == [schedule["id"]]
    # 복원된 스케줄은 일시정지 상태라 복원하자마자 발신되지 않는다.
    assert restored[0]["status"] == "paused"
    assert len(client.get("/api/runs").json()["runs"]) == 1
    assert len(client.get("/api/tasks").json()["tasks"]) == 2
    assert _archive(client)["items"] == []


def test_archive_restore_run_and_task(client):
    schedule = make_schedule(client, numbers=["010-5000-0601", "010-5000-0602"], repeat_count=1)
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    run_id = client.get("/api/runs").json()["runs"][0]["id"]
    task_id = client.get("/api/tasks").json()["tasks"][0]["id"]

    client.delete(f"/api/tasks/{task_id}")
    assert [item["kind"] for item in _archive(client)["items"]] == ["task"]
    assert client.post("/api/archive/restore", json={"kind": "task", "id": task_id}).json()["restored"] == 1
    assert task_id in [t["id"] for t in client.get("/api/tasks").json()["tasks"]]

    client.delete(f"/api/runs/{run_id}")
    assert client.get("/api/tasks").json()["tasks"] == []
    result = client.post("/api/archive/restore", json={"kind": "run", "id": run_id}).json()
    assert result["restored"] == 1 and result["restored_tasks"] == 2
    assert client.get("/api/runs").json()["runs"][0]["id"] == run_id
    assert len(client.get("/api/tasks").json()["tasks"]) == 2
    # 스케줄은 그대로 남아 있다.
    assert [s["id"] for s in client.get("/api/schedules").json()["schedules"]] == [schedule["id"]]


def test_archive_restore_sms_keeps_photo(client):
    created = _post_photo_sms(client, body="보관함 사진")
    client.delete(f"/api/sms/{created['id']}")

    item = _archive(client, kind="sms")["items"][0]
    assert item["has_mms"] is True
    assert item["attachments"][0]["url"] == f"/api/sms/{created['id']}/attachments/0"
    assert client.get(f"/api/sms/{created['id']}/attachments/0").status_code == 200

    assert client.post("/api/archive/restore", json={"kind": "sms", "id": created["id"]}).json()["restored"] == 1
    assert [m["id"] for m in client.get("/api/sms").json()["messages"]] == [created["id"]]
    assert client.get("/api/sms").json()["messages"][0]["has_mms"] is True


def test_archive_restore_unknown_and_bad_kind(client):
    missing = client.post("/api/archive/restore", json={"kind": "sms", "id": 9999})
    assert missing.status_code == 404
    assert "보관함" in missing.json()["detail"]
    assert client.post("/api/archive/restore", json={"kind": "device", "id": 1}).status_code == 422
    assert client.post("/api/archive/restore", json={"kind": "sms", "id": 0}).status_code == 422


def test_archive_restore_twice_is_404(client):
    created = _post_sms(client, body="두 번 복원")
    client.delete(f"/api/sms/{created['id']}")
    assert client.post("/api/archive/restore", json={"kind": "sms", "id": created["id"]}).status_code == 200
    assert client.post("/api/archive/restore", json={"kind": "sms", "id": created["id"]}).status_code == 404


def test_bulk_deletes_are_soft_and_archived(client):
    first = make_schedule(client, name="일괄 A", numbers=["010-5000-0701"], repeat_count=1)
    second = make_schedule(client, name="일괄 B", numbers=["010-5000-0702"], repeat_count=1)
    body = client.post("/api/schedules/delete", json={"ids": [first["id"], second["id"]]}).json()
    assert body["deleted"] == 2 and body["soft_deleted"] is True
    assert client.get("/api/schedules").json()["schedules"] == []
    assert {item["id"] for item in _archive(client, kind="schedules")["items"]} == {first["id"], second["id"]}

    # 실행 기록 일괄 삭제도 보관함으로
    schedule = make_schedule(client, name="일괄 실행", numbers=["010-5000-0703"], repeat_count=1)
    schedule["id"]
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    run_id = client.get("/api/runs").json()["runs"][0]["id"]
    assert client.post("/api/runs/delete", json={"all_finished": True}).status_code == 404  # 아직 대기 중
    body = client.post("/api/runs/delete", json={"ids": [run_id]}).json()
    assert body["deleted"] == 1 and body["soft_deleted"] is True
    assert [item["id"] for item in _archive(client, kind="runs")["items"]] == [run_id]


def test_soft_deleted_items_survive_restart_and_init(client):
    """소프트 삭제는 파일/DB 재시작 후에도 보관함에 남는다(스키마 마이그레이션 멱등)."""
    created = _post_sms(client, body="재시작 후에도 보관")
    client.delete(f"/api/sms/{created['id']}")
    db.init_db()  # 다시 마이그레이션해도 deleted_at 은 유지된다.
    assert [item["id"] for item in _archive(client, kind="sms")["items"]] == [created["id"]]


# --------------------------------------------------------------------------
# 26. 스키마 마이그레이션(소프트 삭제 · 기기 편집 플래그 · 로그 분류)
# --------------------------------------------------------------------------
def test_db_migration_adds_soft_delete_and_device_flags(tmp_path, monkeypatch):
    import sqlite3

    old_db = tmp_path / "old_soft.db"
    raw = sqlite3.connect(old_db)
    raw.executescript(
        """
        CREATE TABLE devices (
            id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', model TEXT DEFAULT '',
            android_version TEXT DEFAULT '', app_version TEXT DEFAULT '', battery INTEGER,
            network TEXT DEFAULT '', phone_number TEXT DEFAULT '', note TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, last_seen TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', type TEXT NOT NULL,
            numbers TEXT NOT NULL, repeat_count INTEGER NOT NULL DEFAULT 1, total_calls INTEGER,
            order_mode TEXT NOT NULL DEFAULT 'sequence', max_call_duration_sec INTEGER, sms_body TEXT,
            start_at TEXT NOT NULL, recurrence TEXT NOT NULL DEFAULT 'once', device_id TEXT,
            target_device_ids TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active',
            next_run_at TEXT, last_run_at TEXT, run_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER, schedule_name TEXT DEFAULT '',
            type TEXT NOT NULL DEFAULT 'call', scheduled_for TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            total_tasks INTEGER NOT NULL DEFAULT 0, done_tasks INTEGER NOT NULL DEFAULT 0,
            failed_tasks INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, schedule_id INTEGER,
            schedule_name TEXT DEFAULT '', type TEXT NOT NULL, phone_number TEXT NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0, repeat_index INTEGER NOT NULL DEFAULT 1,
            max_call_duration_sec INTEGER, sms_body TEXT, device_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT, duration_sec REAL,
            claimed_at TEXT, started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE sms_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, sender TEXT DEFAULT '',
            receiver_number TEXT DEFAULT '', body TEXT DEFAULT '', received_at TEXT, created_at TEXT NOT NULL,
            telegram_status TEXT NOT NULL DEFAULT 'pending', telegram_error TEXT,
            telegram_attempts INTEGER NOT NULL DEFAULT 0, telegram_sent_at TEXT,
            attachment_count INTEGER NOT NULL DEFAULT 0, attachment_paths TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL DEFAULT 'info',
                           message TEXT NOT NULL, created_at TEXT NOT NULL);
        """
    )
    raw.execute("INSERT INTO devices(id, name, created_at) VALUES ('5656', '5656', '2026-11-22T19:07:06+09:00')")
    raw.execute(
        """INSERT INTO schedules(name, type, numbers, start_at, status, created_at)
           VALUES ('기존 스케줄', 'call', '["010-1"]', '2026-11-20T09:00:00+09:00', 'finished',
                   '2026-11-20T08:00:00+09:00')"""
    )
    raw.execute(
        """INSERT INTO runs(schedule_id, schedule_name, scheduled_for, status, created_at)
           VALUES (1, '기존 스케줄', '2026-11-20T09:00:00+09:00', 'done', '2026-11-20T09:00:00+09:00')"""
    )
    raw.execute(
        """INSERT INTO tasks(run_id, schedule_id, type, phone_number, status, created_at)
           VALUES (1, 1, 'call', '010-1', 'done', '2026-11-20T09:00:00+09:00')"""
    )
    raw.execute(
        """INSERT INTO sms_inbox(device_id, sender, body, received_at, created_at, telegram_status)
           VALUES ('5656', '010-2', '기존 문자', '2026-11-21T10:00:00+09:00',
                   '2026-11-21T10:00:01+09:00', 'sent')"""
    )
    raw.execute("INSERT INTO logs(level, message, created_at) VALUES ('info', '기기 등록: 5656', '2026-11-20T09:00:00+09:00')")
    raw.commit()
    raw.close()

    db.init_db(old_db)

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row
    for table in ("schedules", "runs", "tasks", "sms_inbox"):
        assert "deleted_at" in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    assert {"name_customized", "phone_customized", "model_customized", "android_customized"} <= device_columns
    assert "category" in {row["name"] for row in conn.execute("PRAGMA table_info(logs)")}
    assert conn.execute("SELECT COUNT(*) FROM logs WHERE category = 'device'").fetchone()[0] == 1
    for table in ("schedules", "runs", "tasks", "sms_inbox"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0] == 1
    conn.close()

    # 마이그레이션 뒤에도 기존 데이터가 그대로 보인다.
    monkeypatch.setattr(config, "DB_PATH", old_db)
    with TestClient(app) as migrated:
        assert [s["id"] for s in migrated.get("/api/schedules").json()["schedules"]] == [1]
        assert [m["id"] for m in migrated.get("/api/sms").json()["messages"]] == [1]
        assert migrated.get("/api/archive").json()["counts"]["total"] == 0
        # 대시보드에서 고친 이름·번호는 앱 재등록에도 유지된다.
        migrated.patch("/api/devices/5656", json={"name": "형원님 폰", "phone_number": "010-5656-3939"})
        device = migrated.post("/api/devices", json={"device_id": "5656", "name": "5656", "phone_number": ""}).json()["device"]
        assert device["name"] == "형원님 폰" and device["phone_number"] == "010-5656-3939"

        # 예전 DB 에서도 모델명·안드로이드 버전 수정과 기기 ID 변경이 그대로 동작한다.
        device = migrated.patch(
            "/api/devices/5656",
            json={"model": "samsung SM-S916N (내가 고침)", "android_version": "17", "new_device_id": "5656-NEW"},
        ).json()["device"]
        assert device["id"] == "5656-NEW"
        assert device["model_customized"] is True and device["android_customized"] is True
        device = migrated.post(
            "/api/devices", json={"device_id": "5656-NEW", "model": "samsung SM-S916N", "android_version": "16"}
        ).json()["device"]
        assert device["model"] == "samsung SM-S916N (내가 고침)" and device["android_version"] == "17"


# --------------------------------------------------------------------------
# 27. 대시보드 UI · OpenAPI
# --------------------------------------------------------------------------
def test_dashboard_has_log_search_and_archive_controls(client):
    html = client.get("/").text
    for keyword in (
        'id="logQuery"',
        'oninput="markLogDirty()"',
        'id="logStart"',
        'id="logEnd"',
        'id="logCategory"',
        'id="logLevel"',
        "전체 보기",
        "더 보기",
        "loadMoreLogs(",
        "loadAllLogs(",
        "setLogRange(",
        'data-tab="archive"',
        'id="pane-archive"',
        'id="archiveTable"',
        "restoreArchive(",
        "/api/archive/restore",
        "보관함",
        "로그를 불러오지 못했습니다",
    ):
        assert keyword in html, keyword
    # 로그 행에는 시각·분류·메시지가 함께 나온다.
    assert "log.category_label" in html


def test_dashboard_delete_confirmations_mention_archive(client):
    html = client.get("/").text
    assert "삭제 보관함에서" in html
    assert "웹 목록에서 숨겨" in html.lower() or "웹 목록에서 숨김" in html or "웹 목록에서 숨겨" in html
    assert "웹 목록에서 숨겨지고" in html


def test_dashboard_has_device_edit_controls(client):
    html = client.get("/").text
    for keyword in (
        'id="deviceModal"',
        'id="devEditName"',
        'id="devEditDeviceId"',
        'id="devEditPhone"',
        'id="devEditModel"',
        'id="devEditAndroid"',
        'id="devEditNote"',
        'id="devEditEnabled"',
        "openDeviceEdit(",
        "saveDeviceEdit(",
        "closeDeviceEdit(",
        "기기 정보 편집",
        "표시 이름 (기기명)",
        "기기 ID",
        "수신 번호",
        "모델명",
        "안드로이드 버전",
        "메모",
        "스케줄·작업·수신문자 기록도 함께 옮겨지며",
        "devicesSignature",
        "data-lastseen",
    ):
        assert keyword in html, keyword
    # 기기 표의 액션은 편집 · 사용 중지 · 삭제 세 가지
    assert "편집</button>" in html
    # 저장 후에는 기기·스케줄·대기열·수신 문자 목록을 함께 다시 읽는다.
    save_fn = html.split("async function saveDeviceEdit()", 1)[1].split("async function deleteDevice", 1)[0]
    for loader in ("loadDevices()", "loadSchedules()", "loadTasks()", "loadRuns()", "loadSms()"):
        assert loader in save_fn, loader
    assert "new_device_id" in save_fn


def test_openapi_exposes_log_filters_and_archive(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    log_params = {p["name"] for p in paths["/api/logs"]["get"].get("parameters", [])}
    assert {"q", "level", "category", "start", "end", "all", "limit", "offset"} <= log_params
    assert "get" in paths["/api/archive"]
    assert "post" in paths["/api/archive/restore"]
    archive_params = {p["name"] for p in paths["/api/archive"]["get"].get("parameters", [])}
    assert {"kind", "q", "start", "end", "limit", "offset"} <= archive_params
    restore_schema = spec["components"]["schemas"]["ArchiveRestore"]
    assert {"kind", "id"} <= set(restore_schema["properties"])
    device_patch = spec["components"]["schemas"]["DeviceUpdate"]["properties"]
    assert {"name", "new_device_id", "phone_number", "model", "android_version", "note", "enabled"} <= set(
        device_patch
    )


def test_archive_and_logs_require_api_key(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "secret-key")
    assert client.get("/api/archive").status_code == 401
    assert client.post("/api/archive/restore", json={"kind": "sms", "id": 1}).status_code == 401
    assert client.get("/api/logs").status_code == 401
    headers = {"X-API-Key": "secret-key"}
    assert client.get("/api/archive", headers=headers).status_code == 200
    assert client.get("/api/logs", headers=headers).status_code == 200


# --------------------------------------------------------------------------
# 24. 실행 종료일(end_date) — 검증 · 자동 종료 · 복사/수정 보존
# --------------------------------------------------------------------------
def test_end_date_created_and_returned(client):
    schedule = make_schedule(client, start_at="2026-11-23T09:00", recurrence="daily", end_date="2026-11-30")
    assert schedule["end_date"] == "2026-11-30"
    assert schedule["end_date_text"] == "2026-11-30"
    assert schedule["end_date_expired"] is False

    # 종료일을 비우면(기본) 종료일 없이 반복한다.
    plain = make_schedule(client, start_at="2026-11-23T09:00", recurrence="daily")
    assert plain["end_date"] == ""
    assert plain["end_date_expired"] is False


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "2026/11/30", "20261130", "내일", "2026-1-5"])
def test_end_date_invalid_format_rejected(client, bad):
    response = client.post(
        "/api/schedules",
        json={"type": "call", "numbers": ["010-1234-5678"], "start_at": "2026-11-23T09:00", "end_date": bad},
    )
    assert response.status_code == 422, response.text


def test_end_date_before_start_rejected(client):
    response = client.post(
        "/api/schedules",
        json={"type": "call", "numbers": ["010-1234-5678"], "start_at": "2026-12-01T09:00", "end_date": "2026-11-30"},
    )
    assert response.status_code == 422
    assert "종료일" in response.text

    # 같은 날이면 허용(그날 23:59 까지)
    ok = make_schedule(client, start_at="2026-11-30T21:00", end_date="2026-11-30")
    assert ok["end_date"] == "2026-11-30"


def test_end_date_stops_recurring_schedule_and_marks_finished(client):
    schedule = make_schedule(
        client,
        numbers=["010-9090-0001"],
        repeat_count=1,
        start_at="2026-11-20T09:00",
        recurrence="daily",
        end_date="2026-11-20",
    )
    conn = _conn()
    try:
        created = scheduler.run_due_schedules(conn, parse_dt("2026-11-22T10:00:00+09:00"))
    finally:
        conn.close()
    assert created == []  # 종료일이 지나 실행을 만들지 않는다

    after = client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]
    assert after["status"] == "finished"
    assert after["next_run_at"] is None
    assert after["end_date_expired"] is True
    assert client.get("/api/runs").json()["runs"] == []


def test_end_date_runs_on_last_day_then_finishes(client):
    schedule = make_schedule(
        client,
        numbers=["010-9090-0002"],
        repeat_count=1,
        start_at="2026-11-20T09:00",
        recurrence="daily",
        end_date="2026-11-21",
    )
    conn = _conn()
    try:
        # 20일: 종료일 전이므로 실행
        first = scheduler.run_due_schedules(conn, parse_dt("2026-11-20T10:00:00+09:00"))
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
        assert first and row["status"] == "active"
        assert row["next_run_at"].startswith("2026-11-21T09:00")

        # 21일(종료일 당일): 마지막 실행 후 종료
        second = scheduler.run_due_schedules(conn, parse_dt("2026-11-21T10:00:00+09:00"))
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
    finally:
        conn.close()
    assert second
    assert row["status"] == "finished"
    assert row["next_run_at"] is None
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 2
    assert all(run["schedule_id"] == schedule["id"] for run in runs)


def test_end_date_past_finishes_schedule_with_future_next_run(client):
    """예전에 저장된 예약 시각이 종료일 뒤로 잡혀 있어도 스케줄러가 종료 처리한다."""
    schedule = make_schedule(
        client,
        numbers=["010-9090-0003"],
        repeat_count=1,
        start_at="2026-11-23T09:00",
        recurrence="daily",
        end_date="2026-11-23",
    )
    conn = _conn()
    try:
        # 종료일 뒤로 예약이 밀린 상태를 흉내 낸다.
        conn.execute(
            "UPDATE schedules SET next_run_at = ? WHERE id = ?",
            ("2026-11-25T09:00:00+09:00", schedule["id"]),
        )
        finished = scheduler.finish_expired_schedules(conn, parse_dt("2026-11-23T10:00:00+09:00"))
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
    finally:
        conn.close()
    assert finished == [schedule["id"]]
    assert row["status"] == "finished"
    assert row["next_run_at"] is None


def test_end_date_preserved_on_copy_and_edit(client):
    schedule = make_schedule(
        client, start_at="2026-11-23T09:00", recurrence="daily", end_date="2026-11-30"
    )
    copied = client.post(f"/api/schedules/{schedule['id']}/copy", json={}).json()["schedule"]
    assert copied["end_date"] == "2026-11-30"

    # 종료일만 늘리면 값이 그대로 저장된다.
    edited = client.patch(f"/api/schedules/{schedule['id']}", json={"end_date": "2026-12-15"}).json()["schedule"]
    assert edited["end_date"] == "2026-12-15"

    # 종료일을 지우면 종료일 없이 반복한다.
    cleared = client.patch(f"/api/schedules/{schedule['id']}", json={"end_date": ""}).json()["schedule"]
    assert cleared["end_date"] == ""

    # 이름만 고쳐도 종료일은 유지된다.
    renamed = client.patch(f"/api/schedules/{copied['id']}", json={"name": "이름만 변경"}).json()["schedule"]
    assert renamed["end_date"] == "2026-11-30"


def test_edit_end_date_before_start_rejected(client):
    schedule = make_schedule(client, start_at="2026-11-23T09:00", recurrence="daily")
    response = client.patch(f"/api/schedules/{schedule['id']}", json={"end_date": "2026-11-22"})
    assert response.status_code == 422


def test_finished_schedule_reactivates_when_end_date_extended(client):
    schedule = make_schedule(
        client,
        numbers=["010-9090-0004"],
        repeat_count=1,
        start_at="2026-11-20T09:00",
        recurrence="daily",
        end_date="2026-11-20",
    )
    conn = _conn()
    try:
        scheduler.run_due_schedules(conn, parse_dt("2026-11-22T10:00:00+09:00"))
    finally:
        conn.close()
    assert client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]["status"] == "finished"

    extended = client.patch(
        f"/api/schedules/{schedule['id']}", json={"end_date": "2026-12-31"}
    ).json()["schedule"]
    assert extended["status"] == "active"
    assert extended["next_run_at"] is not None


def test_resume_after_end_date_is_blocked(client):
    schedule = make_schedule(
        client,
        start_at="2026-11-20T09:00",
        recurrence="daily",
        end_date="2026-11-20",
    )
    client.post(f"/api/schedules/{schedule['id']}/pause")
    response = client.post(f"/api/schedules/{schedule['id']}/resume")
    assert response.status_code == 400
    assert "종료일" in response.text
    assert client.get(f"/api/schedules/{schedule['id']}").json()["schedule"]["status"] == "finished"


def test_end_date_column_added_by_migration(tmp_path, monkeypatch):
    """예전 DB(운영 중 Railway 볼륨)에 end_date 컬럼이 손실 없이 추가된다."""
    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(
        """
        CREATE TABLE schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL,
            numbers TEXT NOT NULL,
            repeat_count INTEGER NOT NULL DEFAULT 1,
            order_mode TEXT NOT NULL DEFAULT 'sequence',
            max_call_duration_sec INTEGER,
            sms_body TEXT,
            start_at TEXT NOT NULL,
            recurrence TEXT NOT NULL DEFAULT 'once',
            device_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            next_run_at TEXT,
            last_run_at TEXT,
            run_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """INSERT INTO schedules(name, type, numbers, repeat_count, start_at, recurrence, status, created_at)
           VALUES ('운영 스케줄', 'call', '["02-2192-0200"]', 1, '2026-11-22T08:54:00+09:00', 'daily', 'paused',
                   '2026-11-22T08:00:00+09:00')"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", old_db)
    db.init_db()

    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM schedules").fetchone()
    conn.close()
    assert row["end_date"] == ""  # 기존 스케줄은 종료일 없음(그대로 반복)
    assert row["name"] == "운영 스케줄"


# --------------------------------------------------------------------------
# 25. 문자(SMS) 자동 발송 · 앱 poll 계약 · 보낸 회수 관리
# --------------------------------------------------------------------------
ANDROID_TASK_FIELDS = {
    "id",
    "type",
    "phone_number",
    "max_call_duration_sec",
    "sms_body",
    "schedule_name",
    "repeat_index",
}


def test_sms_schedule_multi_device_enqueue_and_poll_payload(client):
    """문자 스케줄이 기기마다 같은 건수로 만들어지고, 앱(v1.3.6)이 읽는 필드로 내려간다."""
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(
        client,
        type="sms",
        numbers=["010-7777-0001", "010-7777-0002"],
        repeat_count=1,
        total_calls=2,
        sms_body="안녕하세요. 안내 문자입니다.",
        max_call_duration_sec=None,
        target_device_ids=["DEV-A", "DEV-B"],
    )
    assert schedule["type"] == "sms"
    assert schedule["tasks_per_device"] == 2
    assert schedule["tasks_per_run"] == 4

    run = client.post(f"/api/schedules/{schedule['id']}/run-now").json()
    tasks = _tasks_in_run(client, run["run_id"])
    assert len(tasks) == 4
    assert {task["device_id"] for task in tasks} == {"DEV-A", "DEV-B"}
    assert all(task["type"] == "sms" and task["sms_body"] == "안녕하세요. 안내 문자입니다." for task in tasks)

    polled = client.get("/api/tasks/poll", params={"device_id": "DEV-A"}).json()["task"]
    assert polled is not None
    assert ANDROID_TASK_FIELDS <= set(polled)
    assert polled["type"] == "sms"
    assert polled["sms_body"] == "안녕하세요. 안내 문자입니다."
    assert polled["phone_number"] in ("010-7777-0001", "010-7777-0002")
    assert polled["schedule_name"] == schedule["name"]
    assert polled["repeat_index"] == 1
    assert polled["max_call_duration_sec"] is None
    assert polled["device_id"] == "DEV-A"

    # 문자는 결과 보고도 전화와 같다(성공/실패).
    done = client.post(
        f"/api/tasks/{polled['id']}/result",
        json={"device_id": "DEV-A", "status": "done", "result": "문자 발송 완료"},
    )
    assert done.status_code == 200
    assert done.json()["task"]["status"] == "done"


def test_schedule_list_includes_sent_counts(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    schedule = make_schedule(
        client,
        type="sms",
        numbers=["010-8888-0001", "010-8888-0002"],
        repeat_count=1,
        total_calls=4,
        sms_body="보낸 회수 테스트",
        max_call_duration_sec=None,
        target_device_ids=["DEV-A"],
    )
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    run_tasks = client.get("/api/tasks", params={"schedule_id": schedule["id"]}).json()["tasks"]
    assert len(run_tasks) == 4
    first, second = run_tasks[0], run_tasks[1]
    client.post(f"/api/tasks/{first['id']}/result", json={"device_id": "DEV-A", "status": "done"})
    client.post(
        f"/api/tasks/{second['id']}/result",
        json={"device_id": "DEV-A", "status": "failed", "error": "통신사 오류"},
    )

    listed = next(s for s in client.get("/api/schedules").json()["schedules"] if s["id"] == schedule["id"])
    stats = listed["stats"]
    assert stats["done_count"] == 1
    assert stats["failed_count"] == 1
    assert stats["pending_count"] == 2
    assert stats["total_tasks"] == 4
    assert stats["run_count"] == 1
    assert stats["last_sent_at"]


def test_schedule_stats_endpoint_per_number_and_device(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    _register_device(client, "DEV-B", "갤럭시 B", "010-3333-4444")
    schedule = make_schedule(
        client,
        type="sms",
        numbers=["010-9999-0001", "010-9999-0002"],
        repeat_count=1,
        total_calls=2,
        sms_body="상세 집계",
        max_call_duration_sec=None,
        target_device_ids=["DEV-A", "DEV-B"],
    )
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    tasks = client.get("/api/tasks", params={"schedule_id": schedule["id"], "limit": 100}).json()["tasks"]
    # DEV-A 의 0001 은 성공, DEV-A 의 0002 는 대기, DEV-B 의 0001 은 실패
    by_key = {(task["device_id"], task["phone_number"]): task for task in tasks}
    assert len(by_key) == 4
    client.post(
        f"/api/tasks/{by_key[('DEV-A', '010-9999-0001')]['id']}/result",
        json={"device_id": "DEV-A", "status": "done"},
    )
    client.post(
        f"/api/tasks/{by_key[('DEV-B', '010-9999-0001')]['id']}/result",
        json={"device_id": "DEV-B", "status": "failed", "error": "전송 실패"},
    )

    detail = client.get(f"/api/schedules/{schedule['id']}/stats").json()
    assert detail["schedule_id"] == schedule["id"]
    assert detail["type"] == "sms"
    assert detail["done_count"] == 1
    assert detail["failed_count"] == 1
    assert detail["pending_count"] == 2
    assert detail["total_tasks"] == 4

    # 번호별: 등록 순서 그대로
    assert [entry["number"] for entry in detail["numbers"]] == ["010-9999-0001", "010-9999-0002"]
    by_number = {entry["number"]: entry for entry in detail["numbers"]}
    assert by_number["010-9999-0001"]["total_tasks"] == 2
    assert by_number["010-9999-0001"]["done_count"] == 1
    assert by_number["010-9999-0001"]["failed_count"] == 1
    assert by_number["010-9999-0001"]["last_sent_text"] != "-"
    assert by_number["010-9999-0002"]["pending_count"] == 2
    assert by_number["010-9999-0002"]["last_sent_at"] is None

    # 기기별: 이름·수신 번호가 붙는다
    by_device = {entry["device_id"]: entry for entry in detail["devices"]}
    assert set(by_device) == {"DEV-A", "DEV-B"}
    assert by_device["DEV-A"]["device_name"] == "갤럭시 A"
    assert by_device["DEV-A"]["phone_number"] == "010-1111-2222"
    assert by_device["DEV-A"]["done_count"] == 1
    assert by_device["DEV-A"]["pending_count"] == 1
    assert by_device["DEV-B"]["failed_count"] == 1
    assert by_device["DEV-B"]["phone_number"] == "010-3333-4444"

    assert client.get("/api/schedules/9999/stats").status_code == 404


def test_schedule_stats_excludes_deleted_tasks(client):
    schedule = make_schedule(client, numbers=["010-1212-0001"], repeat_count=1, start_at="2026-11-20T09:00")
    client.post(f"/api/schedules/{schedule['id']}/run-now")
    task = client.get("/api/tasks", params={"schedule_id": schedule["id"]}).json()["tasks"][0]
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200

    detail = client.get(f"/api/schedules/{schedule['id']}/stats").json()
    assert detail["total_tasks"] == 0
    assert detail["done_count"] == 0
    assert detail["numbers"][0]["number"] == "010-1212-0001"
    assert detail["numbers"][0]["total_tasks"] == 0


def test_global_stats_separates_sent_calls_and_sms(client):
    _register_device(client, "DEV-A", "갤럭시 A", "010-1111-2222")
    sms = make_schedule(
        client,
        type="sms",
        numbers=["010-5555-0001"],
        repeat_count=1,
        sms_body="발신 집계",
        max_call_duration_sec=None,
        target_device_ids=["DEV-A"],
        start_at="2026-11-20T09:00",
    )
    call = make_schedule(
        client,
        numbers=["010-5555-0002"],
        repeat_count=1,
        target_device_ids=["DEV-A"],
        start_at="2026-11-20T09:00",
    )
    client.post(f"/api/schedules/{sms['id']}/run-now")
    client.post(f"/api/schedules/{call['id']}/run-now")
    for task in client.get("/api/tasks", params={"limit": 100}).json()["tasks"]:
        client.post(f"/api/tasks/{task['id']}/result", json={"device_id": "DEV-A", "status": "done"})

    stats = client.get("/api/stats").json()
    assert stats["sent"]["sms_done"] == 1
    assert stats["sent"]["call_done"] == 1
    assert stats["sent"]["sms_failed"] == 0
    assert stats["sent"]["sms_today"] == 1
    assert stats["sent"]["call_today"] == 1
    # 수신 문자 수와 발신 문자 수는 따로 센다.
    assert stats["sms"]["today"] == 0
