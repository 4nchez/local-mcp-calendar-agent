"""일정 매칭.

수정/삭제 요청은 "어떤 일정인지" 먼저 특정해야 한다.
제목 유사도 + 날짜 일치도로 점수를 매기고, 1등과 2등의 차이가 작으면
확정하지 않고 후보를 되돌려 사용자에게 되묻는다. (오삭제 방지)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Optional

TITLE_WEIGHT = 0.7
DATE_WEIGHT = 0.3
ACCEPT_THRESHOLD = 0.45
AMBIGUOUS_GAP = 0.12


@dataclass
class MatchResult:
    best: Optional[dict]
    score: float
    ambiguous: bool
    candidates: list[dict]


def _similarity(a: str, b: str) -> float:
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def _date_score(schedule_start: str, target: Optional[date]) -> float:
    if target is None:
        return 0.5  # 날짜 단서가 없으면 중립
    try:
        start = datetime.fromisoformat(schedule_start)
    except ValueError:
        return 0.0
    return 1.0 if start.date() == target else 0.0


def match_schedule(
    schedules: list[dict],
    title_hint: str = "",
    date_hint: Optional[date] = None,
) -> MatchResult:
    if not schedules:
        return MatchResult(None, 0.0, False, [])

    if not title_hint.strip() and len(schedules) == 1:
        # 제목 단서가 없어도 후보가 하나뿐이면 그것으로 확정한다
        return MatchResult(schedules[0], 0.6, False, schedules)

    scored: list[tuple[float, dict]] = []
    for s in schedules:
        title_score = _similarity(title_hint, s.get("title", "")) if title_hint else 0.4
        score = TITLE_WEIGHT * title_score + DATE_WEIGHT * _date_score(s.get("start_at", ""), date_hint)
        scored.append((round(score, 4), s))

    scored.sort(key=lambda x: (-x[0], x[1].get("start_at", "")))
    top_score, top = scored[0]

    if top_score < ACCEPT_THRESHOLD:
        return MatchResult(None, top_score, False, [s for _, s in scored[:5]])

    ambiguous = len(scored) > 1 and (top_score - scored[1][0]) < AMBIGUOUS_GAP
    return MatchResult(top, top_score, ambiguous, [s for _, s in scored[:5]])
