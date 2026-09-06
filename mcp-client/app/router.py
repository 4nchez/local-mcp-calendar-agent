"""Rule 기반 Intent Router.

초기 버전은 Intent 판단을 전부 LLM에 맡겼는데, 같은 문장에도 다른 Tool을 고르는
문제가 있었다. 특히 "수정" 요청을 delete + create로 처리해 원본 일정이 사라지는
치명적인 케이스가 있었다.

그래서 판단 순서를 뒤집었다.
    1) 규칙으로 잡히면 규칙을 따른다 (결정적, 재현 가능)
    2) 규칙이 애매할 때만 LLM에게 묻는다 (보조 판단)
    3) LLM도 실패하면 되묻는다 (임의 실행 금지)

우선순위도 규칙으로 고정한다. update 키워드는 delete/create보다 먼저 평가한다.
"수정해줘"가 삭제로 흘러가는 것을 구조적으로 막기 위해서다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CONFIRM = "confirm"
CANCEL = "cancel"
CREATE = "create_schedule"
LIST = "list_schedules"
UPDATE = "update_schedule"
DELETE = "delete_schedule"
HELP = "help"
UNKNOWN = "unknown"


@dataclass
class Rule:
    intent: str
    priority: int
    pattern: re.Pattern
    label: str


def _rule(intent: str, priority: int, source: str, label: str) -> Rule:
    return Rule(intent, priority, re.compile(source), label)


# 위에 있을수록 먼저 평가된다.
RULES: list[Rule] = [
    _rule(CONFIRM, 100, r"^\s*(네|넵|응|어|예|그래|좋아|맞아|진행|확인|ok|okay|yes|y)[.!]?\s*$", "긍정 응답"),
    _rule(CONFIRM, 100, r"(그대로\s?진행|그래도\s?(등록|추가|해|옮겨|저장)|강제로|무시하고)", "충돌 무시 확인"),
    _rule(CANCEL, 99, r"^\s*(아니|아뇨|아니요|취소할게|됐어|관둬|no|n)[.!]?\s*$", "부정 응답"),

    _rule(UPDATE, 90, r"(변경|수정|바꿔|바꾸|고쳐|옮겨|옮기|미뤄|미루|연기|당겨|당기|앞당)", "수정 키워드"),
    _rule(UPDATE, 85, r"(으로|로)\s*(해줘|해주라|잡아|바꿔)", "'~로 바꿔' 패턴"),

    _rule(DELETE, 80, r"(삭제|지워|지우|취소해|취소 해|없애|빼줘|빼주|캔슬|cancel)", "삭제 키워드"),

    _rule(LIST, 70, r"(알려줘|보여줘|보여주|조회|확인해|목록|리스트|뭐\s*(있|해야)|있어\??$|있나|남았|비어|한가|스케줄\s*좀)", "조회 키워드"),
    _rule(LIST, 65, r"(언제|몇\s?시)(야|지|인가|였)", "시각 질의"),

    # "뭐 할 수 있어?"가 조회로 새지 않도록 도움말을 조회보다 위에 둔다
    _rule(HELP, 75, r"(뭐\s?할\s?수\s?있|도움말|사용법|help|어떻게\s?(써|쓰|사용))", "도움말"),

    _rule(CREATE, 60, r"(추가|등록|잡아|잡자|만들어|생성|넣어|예약|저장|기록)", "등록 키워드"),
]

# 시간 표현이 있으면 '등록'으로 기울게 하는 보조 신호
TIME_HINT = re.compile(
    r"(오늘|내일|모레|글피|다음\s?주|담주|이번\s?주|\d{1,2}\s*월|\d{1,2}\s*일|"
    r"\d{1,2}\s*시|\d{1,2}:\d{2}|[월화수목금토일]요일|오전|오후|저녁|아침|점심)"
)


@dataclass
class IntentDecision:
    intent: str
    confidence: float
    source: str  # rule | llm | fallback
    matched: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 2),
            "source": self.source,
            "matched": self.matched,
            "reason": self.reason,
        }


def route(text: str) -> IntentDecision:
    """규칙만으로 Intent를 판단한다. 확신이 없으면 UNKNOWN을 돌려준다."""
    hits: list[Rule] = [r for r in RULES if r.pattern.search(text)]

    if hits:
        best = max(hits, key=lambda r: r.priority)
        same = [r for r in hits if r.intent == best.intent]
        # 서로 다른 의도의 규칙이 동시에 걸리면 확신도를 낮춘다
        conflicting = {r.intent for r in hits} - {best.intent}
        confidence = 0.95 if not conflicting else 0.75
        return IntentDecision(
            intent=best.intent,
            confidence=confidence,
            source="rule",
            matched=[r.label for r in same],
            reason=f"'{best.label}' 규칙 일치"
            + (f" (경합: {', '.join(sorted(conflicting))})" if conflicting else ""),
        )

    if TIME_HINT.search(text):
        # 동사 없이 "내일 3시 치과" 처럼 던지는 경우가 많다
        return IntentDecision(
            intent=CREATE,
            confidence=0.6,
            source="rule",
            matched=["시간 표현"],
            reason="동사는 없지만 시간 표현이 있어 등록으로 해석",
        )

    return IntentDecision(intent=UNKNOWN, confidence=0.0, source="rule", reason="일치하는 규칙 없음")


# LLM이 돌려줄 수 있는 값 화이트리스트. 이 밖의 응답은 신뢰하지 않는다.
ALLOWED_LLM_INTENTS = {CREATE, LIST, UPDATE, DELETE, HELP, UNKNOWN}


def normalize_llm_intent(raw: str) -> str:
    token = (raw or "").strip().lower()
    token = re.sub(r"[^a-z_]", "", token)
    aliases = {
        "create": CREATE,
        "add": CREATE,
        "list": LIST,
        "read": LIST,
        "search": LIST,
        "update": UPDATE,
        "modify": UPDATE,
        "delete": DELETE,
        "remove": DELETE,
    }
    token = aliases.get(token, token)
    return token if token in ALLOWED_LLM_INTENTS else UNKNOWN
