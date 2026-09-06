import pytest

from app.nlu.datetime_parser import parse_time_expression
from app.nlu.title import extract_title


@pytest.mark.parametrize(
    "text, expected",
    [
        ("내일 오후 2시에 면접 일정 추가", "면접"),
        ("내일 3시에 회의 잡아줘", "회의"),          # '회의'의 '의'를 조사로 오인하면 안 된다
        ("금요일 9시에 출근 회의", "출근 회의"),
        ("3월 12일 10시에 치과 예약 등록해줘", "치과 예약"),  # '치과'의 '과'도 마찬가지
        ("오늘 2시부터 4시까지 스터디 등록해줘", "스터디"),
        ("내일 14:30 팀 미팅 등록", "팀 미팅"),
        ("모레 스터디 모임 추가해줘", "스터디 모임"),
    ],
)
def test_extract_title(text, expected):
    spans = parse_time_expression(text).spans
    assert extract_title(text, spans) == expected


def test_fallback_when_empty():
    spans = parse_time_expression("내일 3시에 추가해줘").spans
    assert extract_title("내일 3시에 추가해줘", spans) == "일정"
