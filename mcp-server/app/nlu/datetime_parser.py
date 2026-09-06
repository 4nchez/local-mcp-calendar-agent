"""한국어 날짜/시간 파싱.

설계 원칙
---------
"내일 3시" 같은 표현을 datetime으로 바꾸는 일은 LLM에게 맡기지 않는다.
LLM은 확률적이라 같은 문장에도 다른 값을 내놓을 수 있기 때문에,
1순위로 규칙(정규식) 파서를 돌리고, 규칙이 실패한 표현만 dateparser로 넘긴다.

반환값에는 매칭된 구간(span)이 함께 담긴다.
제목 추출기가 "문장에서 시간 표현만 제거"할 수 있어야 하기 때문이다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

try:  # dateparser는 폴백 전용이라 없어도 규칙 파서는 동작한다.
    import dateparser
except ImportError:  # pragma: no cover
    dateparser = None

KST = ZoneInfo(os.getenv("TZ", "Asia/Seoul"))

DEFAULT_DURATION = timedelta(hours=1)
DEFAULT_HOUR_WHEN_MISSING = 9  # 시간이 없는 생성 요청의 기본값

WEEKDAY_INDEX = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}

# 시간대 힌트 단어 → 기준 시각
DAYPART_HOUR = {
    "새벽": 6,
    "아침": 9,
    "오전": 9,
    "점심": 12,
    "정오": 12,
    "낮": 13,
    "오후": 14,
    "저녁": 18,
    "밤": 20,
    "자정": 0,
}
PM_HINT_WORDS = ("오후", "저녁", "밤", "점심", "낮", "퇴근")
AM_HINT_WORDS = ("오전", "아침", "새벽", "출근")


@dataclass
class Span:
    start: int
    end: int
    text: str


@dataclass
class ParsedTime:
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    all_day: bool = False
    is_range: bool = False
    spans: list[Span] = field(default_factory=list)
    source: str = "none"  # rule | dateparser | none
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.start_at is not None

    def to_dict(self) -> dict:
        return {
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "all_day": self.all_day,
            "is_range": self.is_range,
            "matched": [s.text for s in self.spans],
            "source": self.source,
            "notes": self.notes,
        }


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def _combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=KST)


# ---------------------------------------------------------------------------
# 날짜 파싱
# ---------------------------------------------------------------------------

_RELATIVE_DAYS = {
    "그저께": -2,
    "그제": -2,
    "어제": -1,
    "어저께": -1,
    "오늘": 0,
    "금일": 0,
    "당일": 0,
    "내일": 1,
    "낼": 1,
    "익일": 1,
    "모레": 2,
    "내일모레": 2,
    "글피": 3,
}

_WEEK_OFFSET = {
    "지난주": -1,
    "저번주": -1,
    "이번주": 0,
    "금주": 0,
    "이번 주": 0,
    "다음주": 1,
    "담주": 1,
    "다음 주": 1,
    "차주": 1,
}


def _find_date(text: str, base: datetime) -> tuple[Optional[date], list[Span]]:
    """문장에서 날짜 표현 하나를 찾는다. 가장 구체적인 패턴부터 시도."""
    today = base.date()

    # 2026년 3월 5일 / 2026-03-05 / 2026.3.5
    m = re.search(r"(\d{4})\s*[년\-./]\s*(\d{1,2})\s*[월\-./]\s*(\d{1,2})\s*일?", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), [
                Span(m.start(), m.end(), m.group(0))
            ]
        except ValueError:
            pass

    # 3월 5일
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year
        try:
            candidate = date(year, month, day)
            if candidate < today - timedelta(days=180):
                candidate = date(year + 1, month, day)
            return candidate, [Span(m.start(), m.end(), m.group(0))]
        except ValueError:
            pass

    # 3/5 (숫자/숫자) — 시간 표기(3:5)와 구분하기 위해 슬래시만 허용
    m = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
    if m:
        try:
            candidate = date(today.year, int(m.group(1)), int(m.group(2)))
            if candidate < today - timedelta(days=180):
                candidate = candidate.replace(year=today.year + 1)
            return candidate, [Span(m.start(), m.end(), m.group(0))]
        except ValueError:
            pass

    # (다음주) 금요일
    m = re.search(r"(지난\s?주|저번\s?주|이번\s?주|금주|다음\s?주|담주|차주)?\s*([월화수목금토일])\s*요일", text)
    if m:
        week_word = re.sub(r"\s", "", m.group(1) or "")
        offset_week = _WEEK_OFFSET.get(week_word, None)
        target_wd = WEEKDAY_INDEX[m.group(2)]
        if offset_week is None:
            # "금요일"만 있으면 오늘 포함 가장 가까운 다음 해당 요일
            delta = (target_wd - today.weekday()) % 7
            resolved = today + timedelta(days=delta)
        else:
            monday = today - timedelta(days=today.weekday())
            resolved = monday + timedelta(weeks=offset_week, days=target_wd)
        return resolved, [Span(m.start(), m.end(), m.group(0))]

    # 3일 후 / 2주 뒤
    m = re.search(r"(\d{1,3})\s*(일|주|개월|달)\s*(후|뒤|이내|있다가)", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"일": 1, "주": 7, "개월": 30, "달": 30}[unit] * n
        return today + timedelta(days=days), [Span(m.start(), m.end(), m.group(0))]

    # 오늘 / 내일 / 모레 ...
    for word, offset in sorted(_RELATIVE_DAYS.items(), key=lambda kv: -len(kv[0])):
        m = re.search(word, text)
        if m:
            return today + timedelta(days=offset), [Span(m.start(), m.end(), m.group(0))]

    # 이번주 / 다음주 (요일 없이)
    m = re.search(r"(이번\s?주|금주|다음\s?주|담주|차주|지난\s?주|저번\s?주)", text)
    if m:
        week_word = re.sub(r"\s", "", m.group(1))
        monday = today - timedelta(days=today.weekday())
        return monday + timedelta(weeks=_WEEK_OFFSET.get(week_word, 0)), [
            Span(m.start(), m.end(), m.group(0))
        ]

    return None, []


# ---------------------------------------------------------------------------
# 시간 파싱
# ---------------------------------------------------------------------------


@dataclass
class _TimeToken:
    hour: int
    minute: int
    explicit_meridiem: bool
    span: Span


# 앞의 공백까지 매칭에 넣으면 소요시간 span과 어긋나므로 \s*는 meridiem 그룹 안에 둔다
_TIME_RE = re.compile(
    r"(?:(?P<meridiem>오전|오후|아침|점심|저녁|새벽|밤|낮)\s*)?"
    r"(?P<hour>\d{1,2})\s*(?:시|:)\s*"
    r"(?P<minute>반|\d{1,2}\s*분|\d{2})?"
)
_DAYPART_ONLY_RE = re.compile(r"(정오|자정|아침|점심|저녁|새벽|밤)")


def _resolve_hour(hour: int, meridiem: Optional[str], context: str) -> tuple[int, bool]:
    """12시간제 표기를 24시간제로 변환.

    명시적인 오전/오후가 없으면 문맥 단어 → 숫자 휴리스틱 순으로 판단한다.
    (1~6시는 한국어 대화에서 대부분 오후를 의미한다)
    """
    if meridiem in ("오후", "저녁", "밤", "점심", "낮"):
        return (hour % 12) + 12 if hour != 12 else 12, True
    if meridiem in ("오전", "아침", "새벽"):
        return hour % 12, True
    if any(w in context for w in PM_HINT_WORDS):
        return (hour % 12) + 12 if hour != 12 else 12, True
    if any(w in context for w in AM_HINT_WORDS):
        return hour % 12, True
    if 1 <= hour <= 6:
        return hour + 12, False
    return hour, False


def _find_time_tokens(text: str) -> list[_TimeToken]:
    tokens: list[_TimeToken] = []
    for m in _TIME_RE.finditer(text):
        raw_hour = int(m.group("hour"))
        if raw_hour > 24:
            continue
        minute_raw = (m.group("minute") or "").strip()
        if minute_raw == "반":
            minute = 30
        elif minute_raw:
            minute = int(re.sub(r"\D", "", minute_raw) or 0)
        else:
            minute = 0
        if minute > 59:
            continue
        hour, explicit = _resolve_hour(raw_hour, m.group("meridiem"), text)
        hour = min(hour, 23)
        tokens.append(
            _TimeToken(hour, minute, explicit, Span(m.start(), m.end(), m.group(0).strip()))
        )

    if not tokens:
        m = _DAYPART_ONLY_RE.search(text)
        if m:
            word = m.group(1)
            tokens.append(
                _TimeToken(DAYPART_HOUR[word], 0, True, Span(m.start(), m.end(), word))
            )
    return tokens


# "2시간"은 그 자체로 소요 시간이지만, "10시 30분"의 '30분'은 아니다.
# 그래서 '분'에는 동안/간/짜리 같은 표지를 반드시 요구한다.
_DURATION_RE = re.compile(
    r"(?P<hours>\d{1,3})\s*시간(?:\s*(?:동안|짜리))?"
    r"|(?P<minutes>\d{1,3})\s*분\s*(?:동안|간|짜리)"
)


def _find_duration(text: str) -> tuple[Optional[timedelta], list[Span]]:
    total = timedelta()
    spans: list[Span] = []
    for m in _DURATION_RE.finditer(text):
        if m.group("hours"):
            total += timedelta(hours=int(m.group("hours")))
        else:
            total += timedelta(minutes=int(m.group("minutes")))
        spans.append(Span(m.start(), m.end(), m.group(0)))
    return (total if total else None), spans


_RANGE_HINT = re.compile(r"(부터|까지|에서|~|-|–|→)")


# ---------------------------------------------------------------------------
# 통합 파서
# ---------------------------------------------------------------------------


def parse_time_expression(text: str, base: Optional[datetime] = None) -> ParsedTime:
    """문장에서 시작/종료 시각을 추출한다."""
    base = base or now_kst()
    result = ParsedTime()

    target_date, date_spans = _find_date(text, base)
    tokens = _find_time_tokens(text)
    duration, duration_spans = _find_duration(text)

    # "3시간 동안"의 '3시간'이 시간 토큰으로도 잡히는 경우 제거
    if duration_spans:
        dur_ranges = [(s.start, s.end) for s in duration_spans]
        tokens = [
            t
            for t in tokens
            if not any(t.span.start < b and a < t.span.end for a, b in dur_ranges)
        ]

    if not target_date and not tokens:
        return _dateparser_fallback(text, base)

    result.source = "rule"
    result.spans = list(date_spans) + [t.span for t in tokens] + list(duration_spans)

    day = target_date or base.date()

    if not tokens:
        # 날짜만 있는 경우 → 하루 전체
        result.all_day = True
        result.start_at = _combine(day, time(0, 0))
        result.end_at = _combine(day, time(23, 59, 59))
        return result

    first = tokens[0]
    start_at = _combine(day, time(first.hour, first.minute))

    # 날짜 표현이 없고 이미 지난 시각이면 다음 날로 본다 (dateparser의 PREFER future와 동일)
    if target_date is None and start_at < base:
        start_at += timedelta(days=1)
        result.notes.append("시각이 이미 지나 다음 날로 해석했습니다.")

    end_at: Optional[datetime] = None
    between = ""
    if len(tokens) >= 2:
        between = text[tokens[0].span.end : tokens[1].span.start]
    if len(tokens) >= 2 and _RANGE_HINT.search(between):
        second = tokens[1]
        end_at = _combine(start_at.date(), time(second.hour, second.minute))
        if end_at <= start_at:  # 22시부터 1시까지 → 다음 날
            end_at += timedelta(days=1)
        result.is_range = True
    elif duration:
        end_at = start_at + duration
    else:
        end_at = start_at + DEFAULT_DURATION

    result.start_at = start_at
    result.end_at = end_at
    return result


def _dateparser_fallback(text: str, base: datetime) -> ParsedTime:
    """규칙으로 못 잡은 표현만 dateparser에 넘긴다."""
    result = ParsedTime()
    if dateparser is None:
        return result
    parsed = dateparser.parse(
        text,
        languages=["ko"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": base.replace(tzinfo=None),
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if parsed is None:
        return result
    start = parsed.replace(tzinfo=KST)
    result.start_at = start
    result.end_at = start + DEFAULT_DURATION
    result.source = "dateparser"
    result.notes.append("규칙 파서가 실패해 dateparser로 해석했습니다.")
    return result


def parse_query_range(text: str, base: Optional[datetime] = None) -> tuple[datetime, datetime, str]:
    """조회용 기간 파싱. (start, end, 사람이 읽을 라벨)"""
    base = base or now_kst()
    today = base.date()

    if re.search(r"이번\s?주|금주", text):
        monday = today - timedelta(days=today.weekday())
        return _combine(monday, time.min), _combine(monday + timedelta(days=7), time.min), "이번 주"
    if re.search(r"다음\s?주|담주|차주", text):
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        return _combine(monday, time.min), _combine(monday + timedelta(days=7), time.min), "다음 주"
    if re.search(r"이번\s?달|이달|한\s?달", text):
        first = today.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return _combine(first, time.min), _combine(nxt, time.min), "이번 달"

    parsed = parse_time_expression(text, base)
    if parsed.found:
        assert parsed.start_at is not None
        day = parsed.start_at.date()
        label = day.strftime("%m월 %d일")
        if day == today:
            label = "오늘"
        elif day == today + timedelta(days=1):
            label = "내일"
        return _combine(day, time.min), _combine(day + timedelta(days=1), time.min), label

    # 아무 단서가 없으면 오늘부터 7일
    return (
        _combine(today, time.min),
        _combine(today + timedelta(days=7), time.min),
        "앞으로 7일",
    )


def format_korean(dt: datetime) -> str:
    """2026년 3월 5일 (목) 오후 2시 30분 형태로 출력."""
    weekday = "월화수목금토일"[dt.weekday()]
    meridiem = "오전" if dt.hour < 12 else "오후"
    hour12 = dt.hour % 12 or 12
    minute = f" {dt.minute}분" if dt.minute else ""
    return f"{dt.month}월 {dt.day}일({weekday}) {meridiem} {hour12}시{minute}"
