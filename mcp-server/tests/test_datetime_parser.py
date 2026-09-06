"""시간 파싱 회귀 테스트.

파싱 규칙을 고칠 때마다 예전에 되던 표현이 깨지는 일이 반복돼서,
실제로 입력됐던 문장들을 그대로 케이스로 박아 두었다.
"""
from datetime import datetime

import pytest

from app.nlu.datetime_parser import KST, parse_query_range, parse_time_expression

# 2026-03-05 목요일 오전 10시 기준
BASE = datetime(2026, 3, 5, 10, 0, tzinfo=KST)


@pytest.mark.parametrize(
    "text, expected_start, expected_end",
    [
        ("내일 오후 2시에 면접 일정 추가", "2026-03-06 14:00", "2026-03-06 15:00"),
        ("내일 3시에 회의 잡아줘", "2026-03-06 15:00", "2026-03-06 16:00"),  # 1~6시는 오후로 해석
        ("오늘 오전 9시 스탠드업", "2026-03-05 09:00", "2026-03-05 10:00"),
        ("3월 12일 오전 10시 30분 치과", "2026-03-12 10:30", "2026-03-12 11:30"),
        ("다음주 월요일 저녁 7시 약속", "2026-03-09 19:00", "2026-03-09 20:00"),
        ("금요일 9시 출근 회의", "2026-03-06 09:00", "2026-03-06 10:00"),
        ("내일 14:30 팀 미팅", "2026-03-06 14:30", "2026-03-06 15:30"),
        ("3일 후 오후 5시 반 발표", "2026-03-08 17:30", "2026-03-08 18:30"),
        ("모레 점심 약속", "2026-03-07 12:00", "2026-03-07 13:00"),
    ],
)
def test_single_time(text, expected_start, expected_end):
    parsed = parse_time_expression(text, BASE)
    assert parsed.found
    assert parsed.start_at.strftime("%Y-%m-%d %H:%M") == expected_start
    assert parsed.end_at.strftime("%Y-%m-%d %H:%M") == expected_end


def test_range_expression():
    parsed = parse_time_expression("오늘 2시부터 4시까지 스터디", BASE)
    assert parsed.is_range
    assert parsed.start_at.hour == 14
    assert parsed.end_at.hour == 16


def test_duration_expression():
    parsed = parse_time_expression("내일 1시부터 2시간 동안 코드 리뷰", BASE)
    assert parsed.start_at.strftime("%H:%M") == "13:00"
    assert parsed.end_at.strftime("%H:%M") == "15:00"


def test_overnight_range():
    parsed = parse_time_expression("오늘 밤 11시부터 새벽 1시까지 작업", BASE)
    assert parsed.end_at > parsed.start_at
    assert parsed.end_at.day == parsed.start_at.day + 1


def test_date_only_is_all_day():
    parsed = parse_time_expression("모레 회의 추가해줘", BASE)
    assert parsed.all_day
    assert parsed.start_at.date().isoformat() == "2026-03-07"


def test_past_time_rolls_to_tomorrow():
    """날짜 없이 이미 지난 시각을 말하면 다음 날로 본다."""
    parsed = parse_time_expression("오전 9시 회의", BASE)  # 기준이 10시
    assert parsed.start_at.date().isoformat() == "2026-03-06"


def test_no_time_expression():
    assert not parse_time_expression("그냥 회의", BASE).found


@pytest.mark.parametrize(
    "text, label",
    [
        ("이번주 일정 알려줘", "이번 주"),
        ("다음주 일정 보여줘", "다음 주"),
        ("내일 뭐 있어?", "내일"),
        ("일정 알려줘", "앞으로 7일"),
    ],
)
def test_query_range_label(text, label):
    _, _, resolved = parse_query_range(text, BASE)
    assert resolved == label
