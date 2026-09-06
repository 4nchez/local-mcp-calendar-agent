#!/usr/bin/env bash
# 기동된 스택에 실제 문장을 순서대로 넣어 흐름을 확인한다.
set -e
HOST=${HOST:-http://localhost:${MCP_CLIENT_PORT:-8003}}
SESSION="smoke-$$"

say() {
  echo ""
  echo "사용자 > $1"
  curl -s -X POST "$HOST/chat" \
    -H 'Content-Type: application/json' \
    -d "{\"message\":\"$1\",\"session_id\":\"$SESSION\"}" \
  | python3 -c "
import json,sys
d = json.load(sys.stdin)
calls = ', '.join(f\"{c['tool']}({c['duration_ms']}ms)\" for c in d['tool_calls']) or '없음'
print('에이전트 >', d['reply'].replace(chr(10), ' '))
print(f\"          intent={d['intent']['intent']} ({d['intent']['source']}) / tool={calls}\")
"
}

echo "대상: $HOST"
curl -sf "$HOST/health" > /dev/null || { echo "서버에 연결할 수 없습니다. make up 후 다시 시도하세요."; exit 1; }

say "내일 오후 2시에 면접 일정 추가해줘"
say "내일 오후 2시 30분에 팀 회의 잡아줘"
say "네"
say "이번주 일정 알려줘"
say "내일 면접을 오후 5시로 옮겨줘"
say "내일 면접 취소해줘"
say "내일 일정 알려줘"
