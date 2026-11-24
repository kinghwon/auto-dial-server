"""서버 전역 설정.

.env 파일(또는 환경변수)에서 값을 읽어옵니다. 별도 라이브러리 없이
아래 _load_env() 가 server/.env 를 파싱합니다.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """server/.env 파일이 있으면 환경변수로 읽어들인다(이미 설정된 값 우선)."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()

DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data"))
DB_PATH = Path(os.getenv("DB_PATH") or (DATA_DIR / "autodialer.db"))
# MMS 첨부(사진)를 잠시 보관하는 곳. 재전송·재시도 때 파일을 다시 읽는다.
MEDIA_DIR = Path(os.getenv("MEDIA_DIR") or (DATA_DIR / "media"))

# 안드로이드 앱 APK 배포 폴더. 앱별 하위 폴더(app_id)에 latest.json + APK 를 둔다.
APK_DIR = Path(os.getenv("APK_DIR") or (DATA_DIR / "apks"))
# 앱이 돌려주는 download_url 에 쓸 공개 주소. 비어 있으면 요청 주소를 그대로 쓴다.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
# 업로드 허용 APK 최대 크기(바이트)
MAX_APK_BYTES = int(os.getenv("MAX_APK_BYTES", str(300 * 1024 * 1024)))

TZ_NAME = os.getenv("APP_TZ", "Asia/Seoul")
TZ = ZoneInfo(TZ_NAME)

# 대시보드/앱 공통 API 키. 비어 있으면 인증 없이 동작한다(내부망 전용).
API_KEY = (os.getenv("SERVER_API_KEY") or "").strip()

# 이 시간(초) 안에 하트비트가 없으면 오프라인으로 표시
ONLINE_TIMEOUT_SEC = int(os.getenv("ONLINE_TIMEOUT_SEC", "90"))

# 스케줄러 사용 여부(테스트에서는 0 으로 끈다)
SCHEDULER_ENABLED = (os.getenv("SCHEDULER_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no", "off")

# 스케줄러가 만기된 스케줄을 확인하는 주기(초)
SCHEDULER_INTERVAL_SEC = int(os.getenv("SCHEDULER_INTERVAL_SEC", "10"))

# 기기가 가져간 작업이 이 시간(초) 넘게 응답이 없으면 회수한다
TASK_TIMEOUT_SEC = int(os.getenv("TASK_TIMEOUT_SEC", "900"))

# 앱이 서버에 묻는 기본 주기(초) - 앱이 자체 설정을 쓰면 그 값이 우선
DEFAULT_POLL_INTERVAL_SEC = int(os.getenv("DEFAULT_POLL_INTERVAL_SEC", "5"))


# 텔레그램 전송 재시도 최대 횟수
TELEGRAM_MAX_ATTEMPTS = int(os.getenv("TELEGRAM_MAX_ATTEMPTS", "3"))

# MMS 첨부 한 건 최대 크기(바이트). 앱이 1.5MB 넘는 사진은 줄여서 보내지만 서버에서도 막는다.
MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_BYTES", str(6 * 1024 * 1024)))
# 한 문자에 붙일 수 있는 첨부 개수
MAX_SMS_ATTACHMENTS = 10

DEFAULT_CALL_MAX_SEC = 30
MAX_CALL_MAX_SEC = 3600
MAX_REPEAT_COUNT = 1000
# 한 스케줄 1회 실행에서 만들 수 있는 최대 발신 건수 (총 발신 횟수 상한)
MAX_TOTAL_CALLS = 5000

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
