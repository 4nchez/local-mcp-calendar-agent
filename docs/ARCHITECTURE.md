# 아키텍처

## 컴포넌트 구성

![시스템 아키텍처](./architecture.png)

| 컨테이너 | 역할 | 기술 | 포트 |
|---|---|---|---|
| `dashboard` | 사용자 입력, AI 응답, Tool 실행 로그 표시 | nginx + Vanilla JS | 3000 |
| `mcp-client` | **Agent.** Intent를 분류하고 Tool을 선택·호출 | FastAPI + MCP SDK | 8003 |
| `mcp-server` | **Tool.** 자연어를 구조화하고 API를 실행 | MCP SDK (streamable-http) | 8002 |
| `api-server` | 일정 CRUD와 시간 충돌 검증 | FastAPI + In-Memory | 8001 |
| `ollama` | 로컬 LLM. 보조 분류와 응답 문장 생성 | Ollama | 11434 |

## 요청 흐름

```
사용자: "내일 오후 2시에 면접 일정 추가해줘"

Dashboard  ─POST /api/chat─▶  MCP Client
                              ├ 1. Rule Router로 Intent 판정 → create_schedule (0.95)
                              │    규칙이 실패할 때만 LLM에 보조 분류를 요청
                              ├ 2. TOOL_BY_INTENT 매핑으로 Tool 확정
                              └─MCP call_tool─▶ MCP Server
                                                ├ 3. 시간 파싱   "내일 오후 2시" → 2026-03-06T14:00+09:00
                                                ├ 4. 제목 추출   "면접"
                                                ├ 5. 일정 매칭   (수정·삭제일 때만)
                                                └─POST /schedules─▶ API Server
                                                                    ├ 6. 시간 충돌 검증
                                                                    └ 7. 저장
                              ◀─────────── 실행 결과(JSON) ───────────
                              └ 8. LLM으로 응답 문장 생성 (실패 시 템플릿)
Dashboard  ◀─────── reply + intent + tool_calls + trace ───────
```

Dashboard는 API Server를 직접 호출하지 않는다. nginx가 여는 경로는 `/api/` → `mcp-client` 하나뿐이며,
모든 요청은 Agent를 거친다. 데이터 접근 경로를 하나로 고정해 두면 Agent를 우회한 상태 변경이 생기지 않는다.

## 각 계층이 모르는 것

계층 분리는 "무엇을 아는가"가 아니라 **"무엇을 모르는가"** 로 정의했다.

- `api-server`는 LLM과 MCP를 모른다. AI를 전부 걷어내도 일정 서버로서 단독 동작한다.
- `mcp-server`는 Intent를 모른다. 어떤 Tool을 부를지 판단하지 않고, 넘어온 문장을 구조화해 실행만 한다.
- `mcp-client`는 일정 데이터 모델을 모른다. Tool의 입출력 스키마만 알고 도메인 규칙은 하위 계층에 맡긴다.
- `dashboard`는 MCP를 모른다. `/api/chat` 하나만 호출한다.

## Intent 라우팅 정책

```
사용자 입력
   │
   ├─ 규칙 일치? ──── yes ──▶ 해당 Intent 확정 (confidence 0.95)
   │                          우선순위: confirm > update > delete > help > list > create
   │
   ├─ 시간 표현만 존재? ─ yes ─▶ create로 해석 (confidence 0.6)
   │                            "내일 3시 치과"처럼 동사가 없는 입력
   │
   └─ no ──▶ LLM 보조 분류 ──▶ 화이트리스트 통과? ─ yes ─▶ Intent 확정 (confidence 0.55)
                                                  └ no  ─▶ 되묻기 (Tool 미실행)
```

우선순위에서 `update`를 `delete`보다 위에 둔 것이 핵심이다.
"면접 취소하고 4시로 변경해줘"처럼 두 키워드가 함께 등장할 때 삭제가 이기면 사용자의 일정이 사라진다.
이 경계는 `mcp-client/tests/test_router.py`에 회귀 테스트로 고정해 두었다.

LLM이 무엇을 반환하든 `ALLOWED_LLM_INTENTS` 화이트리스트를 통과하지 못하면 UNKNOWN으로 떨어진다.
LLM은 Tool을 "고를" 수 없고, 정해진 라벨 중 하나를 "제안"할 수 있을 뿐이다.

## 자연어 → 파라미터 변환

| 단계 | 담당 모듈 | 예시 |
|---|---|---|
| 시간 파싱 | `nlu/datetime_parser.py` | "내일 오후 2시" → `2026-03-06T14:00+09:00` |
| 제목 추출 | `nlu/title.py` | "내일 오후 2시에 면접 일정 추가" → `면접` |
| 일정 매칭 | `nlu/matcher.py` | "면접 취소" → 기존 일정 #1 (score 0.78) |

파싱은 **규칙 우선, dateparser 폴백** 구조다. 규칙 파서가 매칭된 구간(span)을 함께 반환하기 때문에,
제목 추출기가 "문장에서 시간 표현만 정확히 도려낸" 나머지를 다룰 수 있다.

주요 해석 규칙:

- 오전/오후가 없는 1~6시는 오후로 해석한다 ("3시에 회의" → 15:00)
- 문맥 단어가 있으면 그것을 우선한다 ("저녁 7시" → 19:00, "아침 8시" → 08:00)
- 날짜 없이 이미 지난 시각을 말하면 다음 날로 본다
- 종료 시각이 시작보다 이르면 자정을 넘긴 것으로 본다 ("밤 11시부터 새벽 1시까지")
- 날짜만 있으면 하루 전체(all-day)로 두고, 등록 시에는 오전 9시로 가정한 뒤 그 가정을 응답에 명시한다

## 실패 시 동작

| 상황 | 동작 |
|---|---|
| LLM 미기동 / 모델 없음 | 규칙 라우팅 + 템플릿 응답으로 정상 동작. 상태 표시등만 노란색 |
| 시간 충돌 | 저장하지 않고 되묻는다. "네"로 답하면 `force=true`로 재실행 |
| 수정·삭제 대상이 모호 | 후보를 제시하고 실행하지 않는다 (오삭제 방지) |
| 시간 표현 없음 | 등록하지 않고 시간을 되묻는다 |
| MCP Server 다운 | Tool 실행 단계에서 오류를 표시하고 대화는 계속 가능 |

## 데이터 모델

```python
Schedule:
    id: int
    title: str
    start_at: datetime   # timezone-aware (Asia/Seoul)
    end_at: datetime     # 미지정 시 start_at + 1시간
    location: str | None
    memo: str | None
    created_at / updated_at: datetime
```

충돌 판정은 반열린 구간 `[start, end)` 기준이다.
14:00~15:00과 15:00~16:00은 겹치지 않는다.

저장소는 `ScheduleStore` 클래스 뒤에 감춰 두었다. PostgreSQL로 옮길 때
`store.py`만 교체하면 되고 상위 계층은 수정하지 않아도 된다.

## MCP 전송 방식

컨테이너가 분리되어 있으므로 stdio가 아닌 **streamable-http**를 사용한다.
`mcp-server`는 `stateless_http=True`로 동작하고, `mcp-client`는 요청마다 세션을 새로 연다.
세션 상태를 서버에 두지 않기 때문에 MCP Server를 여러 벌로 늘려도 그대로 동작한다.
