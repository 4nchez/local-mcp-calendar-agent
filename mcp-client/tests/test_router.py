"""Intent Router 테스트.

이 프로젝트에서 가장 중요한 회귀 테스트다.
LLM 없이도 아래 문장들이 항상 같은 Intent로 분류되어야 한다.
특히 '수정' 요청이 delete로 새는 순간 사용자의 일정이 사라지므로,
update/delete 경계는 반드시 고정되어 있어야 한다.
"""
import pytest

from app.router import (
    CANCEL,
    CONFIRM,
    CREATE,
    DELETE,
    HELP,
    LIST,
    UNKNOWN,
    UPDATE,
    normalize_llm_intent,
    route,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        # 등록
        ("내일 오후 2시에 면접 일정 추가해줘", CREATE),
        ("모레 10시 팀 회의 등록", CREATE),
        ("금요일 저녁 약속 잡아줘", CREATE),
        ("내일 3시 치과", CREATE),  # 동사 없이 시간 표현만
        # 조회
        ("이번주 일정 알려줘", LIST),
        ("내일 뭐 있어?", LIST),
        ("다음주 스케줄 보여줘", LIST),
        # 수정
        ("내일 면접을 4시로 변경해줘", UPDATE),
        ("팀 회의 시간 좀 바꿔줘", UPDATE),
        ("면접 다음주로 미뤄줘", UPDATE),
        ("회의를 30분 당겨줘", UPDATE),
        # 삭제
        ("내일 면접 취소해줘", DELETE),
        ("팀 회의 삭제", DELETE),
        ("그 일정 지워줘", DELETE),
        # 대화 흐름
        ("네", CONFIRM),
        ("그래도 등록해줘", CONFIRM),
        ("아니", CANCEL),
        ("뭐 할 수 있어?", HELP),
        ("음냐리", UNKNOWN),
    ],
)
def test_route(text, expected):
    assert route(text).intent == expected


def test_update_wins_over_delete():
    """수정 요청이 삭제로 분류되면 일정이 사라진다. 우선순위를 고정한다."""
    decision = route("면접 취소하고 4시로 변경해줘")
    assert decision.intent == UPDATE


def test_rule_is_deterministic():
    text = "내일 오후 2시에 면접 일정 추가해줘"
    results = {route(text).intent for _ in range(50)}
    assert results == {CREATE}


def test_decision_is_explainable():
    decision = route("이번주 일정 알려줘")
    assert decision.source == "rule"
    assert decision.reason and decision.matched


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("create_schedule", CREATE),
        ("  CREATE  ", CREATE),
        ("delete", DELETE),
        ("`list_schedules`", LIST),
        ("일정을 추가하겠습니다", UNKNOWN),  # 라벨이 아닌 응답은 신뢰하지 않는다
        ("drop_database", UNKNOWN),
    ],
)
def test_normalize_llm_intent(raw, expected):
    assert normalize_llm_intent(raw) == expected
