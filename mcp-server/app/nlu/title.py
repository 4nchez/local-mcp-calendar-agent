"""제목 추출.

시간 표현을 제거한 뒤 남은 문장에서 '무엇을' 하는 일정인지만 뽑아낸다.
동사/명령어(추가해줘, 잡아줘…)와 조사가 제목에 섞여 들어가는 것이
초기 버전에서 가장 흔한 오류였기 때문에 후처리를 단계로 분리했다.
"""
from __future__ import annotations

import re

from .datetime_parser import Span

# 의도를 나타내는 동사 — 제목에서 제거한다
INTENT_VERBS = [
    "추가해줘", "추가해", "추가", "등록해줘", "등록해", "등록",
    "만들어줘", "만들어", "생성해줘", "생성",
    "잡아줘", "잡아놔", "잡아", "넣어줘", "넣어놔", "넣어",
    "예약해줘", "예약해",
    "알려줘", "보여줘", "보여", "조회해줘", "조회", "확인해줘", "확인",
    "삭제해줘", "삭제해", "삭제", "취소해줘", "취소해", "취소",
    "지워줘", "지워", "빼줘", "빼",
    "변경해줘", "변경해", "변경", "수정해줘", "수정해", "수정",
    "바꿔줘", "바꿔", "옮겨줘", "옮겨", "미뤄줘", "미뤄", "당겨줘", "당겨",
    "해줘", "해주세요", "부탁해", "부탁", "좀",
]

# 일정 그 자체를 가리키는 일반 명사 — 제목이 비지 않는 선에서 제거
GENERIC_NOUNS = ["일정", "스케줄", "캘린더", "약속"]

PARTICLE_TAIL = re.compile(r"(에서|으로|에게|한테|에|을|를|은|는|이|가|와|과|랑|이랑|도|의|로)$")
NOISE = re.compile(r"[?!.,~]+$")

# 범위/보조 표현 — 시간 span에는 안 잡히지만 제목에 남으면 안 되는 단어들
RANGE_WORDS = ["에서부터", "부터", "까지", "동안", "사이", "즈음", "쯤에", "쯤", "경에", "께"]

# 조사를 떼고 남는 글자가 이보다 짧으면 떼지 않는다.
# ("회의"의 '의', "치과"의 '과'를 조사로 오인해 한 글자만 남기는 문제 방지)
MIN_TOKEN_LEN = 2


def _remove_spans(text: str, spans: list[Span]) -> str:
    if not spans:
        return text
    keep: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda s: s.start):
        if span.start < cursor:
            continue
        keep.append(text[cursor : span.start])
        cursor = span.end
    keep.append(text[cursor:])
    return " ".join(part.strip() for part in keep if part.strip())


def _strip_particles(token: str) -> str:
    prev = None
    while token and token != prev:
        prev = token
        token = NOISE.sub("", token).strip()
        stripped = PARTICLE_TAIL.sub("", token).strip()
        # 조사를 떼서 한 글자만 남으면 그건 조사가 아니라 명사의 일부다
        if stripped != token and len(stripped) >= MIN_TOKEN_LEN:
            token = stripped
        elif stripped == "" and len(token) <= 1:
            token = ""  # "에", "를" 처럼 조사 하나만 있는 토큰은 제거
    return token


def extract_title(text: str, time_spans: list[Span] | None = None, fallback: str = "일정") -> str:
    """문장에서 일정 제목을 추출한다."""
    body = _remove_spans(text, time_spans or [])

    for verb in sorted(INTENT_VERBS, key=len, reverse=True):
        body = body.replace(verb, " ")
    for word in RANGE_WORDS:
        body = body.replace(word, " ")

    words = [w for w in re.split(r"\s+", body) if w]
    words = [_strip_particles(w) for w in words]
    words = [w for w in words if w]

    if not words:
        return fallback

    # "면접 일정" → "면접" (일반명사만 남는 경우엔 남겨 둔다)
    meaningful = [w for w in words if w not in GENERIC_NOUNS]
    if meaningful:
        words = meaningful

    # 단어별로 이미 조사를 정리했으므로 여기서 다시 자르지 않는다.
    # (합친 문자열에 조사 규칙을 또 적용하면 "출근 회의" → "출근 회"가 된다)
    title = " ".join(words).strip()
    return title or fallback
