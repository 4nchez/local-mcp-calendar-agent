#!/usr/bin/env bash

set -e
cd "$(dirname "$0")/.."

FAILED=0

# Python 명령 찾기
if command -v python3 >/dev/null 2>&1; then
  SYSTEM_PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
  SYSTEM_PYTHON="python"
else
  echo "Python을 찾을 수 없습니다."
  exit 1
fi

echo "System Python: $($SYSTEM_PYTHON --version)"

for svc in api-server mcp-server mcp-client; do
  echo ""
  echo "── $svc ──────────────────────────────"

  (
    cd "$svc"

    # Windows / Git Bash
    if [ -f ".venv/Scripts/python.exe" ]; then
      PY=".venv/Scripts/python.exe"

    # Linux / macOS
    elif [ -f ".venv/bin/python" ]; then
      PY=".venv/bin/python"

    # venv 없음
    else
      echo "가상환경 생성: $svc/.venv"
      "$SYSTEM_PYTHON" -m venv .venv

      if [ -f ".venv/Scripts/python.exe" ]; then
        PY=".venv/Scripts/python.exe"
      elif [ -f ".venv/bin/python" ]; then
        PY=".venv/bin/python"
      else
        echo "가상환경 생성에 실패했습니다."
        exit 1
      fi
    fi

    echo "Python: $("$PY" --version)"

    "$PY" -m pip install --upgrade pip

    if [ -f requirements.txt ]; then
      "$PY" -m pip install -r requirements.txt
    fi

    "$PY" -m pip install pytest

    "$PY" -m pytest -q
  ) || FAILED=1
done

echo ""

if [ $FAILED -eq 0 ]; then
  echo "전체 통과"
else
  echo "실패한 테스트가 있습니다"
  exit 1
fi
