from datetime import date

from app.nlu.matcher import match_schedule

SCHEDULES = [
    {"id": 1, "title": "면접", "start_at": "2026-03-06T14:00:00+09:00"},
    {"id": 2, "title": "팀 회의", "start_at": "2026-03-06T16:00:00+09:00"},
    {"id": 3, "title": "치과 예약", "start_at": "2026-03-10T10:00:00+09:00"},
]


def test_exact_title_match():
    result = match_schedule(SCHEDULES, title_hint="면접")
    assert result.best["id"] == 1


def test_partial_title_match():
    result = match_schedule(SCHEDULES, title_hint="회의")
    assert result.best["id"] == 2


def test_date_narrows_candidates():
    result = match_schedule(SCHEDULES, title_hint="예약", date_hint=date(2026, 3, 10))
    assert result.best["id"] == 3


def test_unknown_title_is_rejected():
    """엉뚱한 제목이면 실행하지 않고 후보만 돌려준다 (오삭제 방지)."""
    result = match_schedule(SCHEDULES, title_hint="존재하지않는일정")
    assert result.best is None
    assert result.candidates


def test_single_candidate_without_hint():
    result = match_schedule(SCHEDULES[:1], title_hint="")
    assert result.best["id"] == 1


def test_empty_store():
    assert match_schedule([], title_hint="면접").best is None
