#!/usr/bin/env bash
# 자동 발신 서버 실행 스크립트
#   ./run.sh            → 0.0.0.0:8000 으로 실행
#   PORT=9000 ./run.sh  → 포트 변경
set -euo pipefail

cd "$(dirname "$0")"

PY=${PYTHON:-python3}
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "▶ 가상환경을 만듭니다 ($VENV) ..."
  "$PY" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

if ! python -c "import fastapi, uvicorn, apscheduler, httpx" >/dev/null 2>&1; then
  echo "▶ 패키지를 설치합니다 ..."
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "▶ .env 가 없어 .env.example 을 복사합니다."
  cp .env.example .env
fi

HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8000}

echo ""
echo "  ┌────────────────────────────────────────────┐"
echo "  │  자동 발신 관리 서버                        │"
echo "  │  대시보드: http://localhost:${PORT}"
echo "  │  폰에서 접속: http://<이 PC의 IP>:${PORT}"
echo "  └────────────────────────────────────────────┘"
echo ""

exec uvicorn app.main:app --host "$HOST" --port "$PORT" --proxy-headers
