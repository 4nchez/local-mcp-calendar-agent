"""MCP Server — Tool 실행 전용 서버.

MCP Client(Agent)가 고른 Tool이 여기서 실행된다.
Tool 하나의 내부 흐름은 문서의 아키텍처 그대로다.

    자연어 파싱 → 시간 파싱 → 제목 추출 → 일정 매칭 → API 호출

이 서버는 "무엇을 할지" 판단하지 않는다. 판단은 Client의 Router가 하고,
여기서는 넘어온 문장을 구조화해서 실행만 한다.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .api_client import ApiError, ScheduleApiClient
from .nlu.datetime_parser import (
    DEFAULT_HOUR_WHEN_MISSING,
    format_korean,
    now_kst,
    parse_query_range,
    parse_time_expression,
)
from .nlu.matcher import match_schedule
from .nlu.title import extract_title

mcp = FastMCP(
    "schedule-tools",
    instructions="자연어 일정 요청을 실제 일정 API 실행으로 옮기는 도구 모음",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8000")),
    stateless_http=True,
    json_response=True,
)

api = ScheduleApiClient()


def _ok(action: str, message: str, **extra: Any) -> dict:
    return {"ok": True, "action": action, "message": message, **extra}


def _fail(action: str, reason: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "action": action, "reason": reason, "message": message, **extra}


def _summarize(schedule: dict) -> str:
    start = datetime.fromisoformat(schedule["start_at"])
    return f"{format_korean(start)} · {schedule['title']}"


# ---------------------------------------------------------------------------
# Tool 1. 일정 등록
# ---------------------------------------------------------------------------
@mcp.tool()
async def create_schedule(text: str, force: bool = False) -> dict:
    """자연어 문장에서 일정을 만들어 등록한다.

    Args:
        text: "내일 오후 2시에 면접 일정 추가" 같은 사용자 문장
        force: True면 시간이 겹쳐도 강제로 등록
    """
    parsed = parse_time_expression(text)
    if not parsed.found:
        return _fail(
            "create",
            "time_not_found",
            "언제인지 알 수 없습니다. '내일 오후 2시'처럼 시간을 함께 알려주세요.",
            parsed=parsed.to_dict(),
        )

    title = extract_title(text, parsed.spans)
    start_at = parsed.start_at
    end_at = parsed.end_at
    assumptions = list(parsed.notes)

    if parsed.all_day:
        start_at = start_at.replace(hour=DEFAULT_HOUR_WHEN_MISSING, minute=0)
        end_at = start_at + timedelta(hours=1)
        assumptions.append(f"시간이 없어 오전 {DEFAULT_HOUR_WHEN_MISSING}시로 잡았습니다.")

    try:
        created = await api.create(title=title, start_at=start_at, end_at=end_at, force=force)
    except ApiError as exc:
        if exc.status_code == 409:
            conflicts = exc.detail.get("conflicts", []) if isinstance(exc.detail, dict) else []
            return _fail(
                "create",
                "conflict",
                "그 시간에 이미 "
                + ", ".join(_summarize(c) for c in conflicts)
                + " 일정이 있습니다. 그래도 등록할까요?",
                conflicts=conflicts,
                pending={"text": text, "title": title, "start_at": start_at.isoformat()},
                needs_confirmation=True,
                parsed=parsed.to_dict(),
            )
        return _fail("create", "api_error", f"등록에 실패했습니다: {exc.detail}")

    return _ok(
        "create",
        f"{_summarize(created)} 일정을 등록했습니다.",
        schedule=created,
        parsed=parsed.to_dict(),
        assumptions=assumptions,
    )


# ---------------------------------------------------------------------------
# Tool 2. 일정 조회
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_schedules(text: str = "") -> dict:
    """기간이나 키워드로 일정을 조회한다.

    Args:
        text: "이번주 일정 알려줘", "내일 뭐 있어?" 같은 문장. 비우면 앞으로 7일.
    """
    start_from, start_to, label = parse_query_range(text)
    keyword_source = re.sub(r"(일정|스케줄|알려줘|보여줘|조회|확인|뭐|있어|있나|어때)", " ", text)
    keyword = extract_title(keyword_source, parse_time_expression(text).spans, fallback="")
    keyword = keyword if len(keyword) >= 2 else None

    try:
        items = await api.list(start_from=start_from, start_to=start_to, keyword=keyword)
    except ApiError as exc:
        return _fail("list", "api_error", f"조회에 실패했습니다: {exc.detail}")

    if not items:
        return _ok(
            "list",
            f"{label}에 등록된 일정이 없습니다.",
            schedules=[],
            range={"from": start_from.isoformat(), "to": start_to.isoformat(), "label": label},
        )

    return _ok(
        "list",
        f"{label} 일정 {len(items)}건: " + " / ".join(_summarize(s) for s in items[:10]),
        schedules=items,
        range={"from": start_from.isoformat(), "to": start_to.isoformat(), "label": label},
        keyword=keyword,
    )


# ---------------------------------------------------------------------------
# Tool 3. 일정 수정
# ---------------------------------------------------------------------------
_CHANGE_RE = re.compile(
    r"^(?P<target>.+?)\s*(?:을|를|)\s*(?P<new>[^,]+?)\s*(?:로|으로)\s*"
    r"(?:변경|수정|바꿔|바꾸|옮겨|옮기|미뤄|미루|당겨|당기)"
)


def _split_update(text: str) -> tuple[str, str]:
    """수정 요청을 '대상' + '바꿀 값'으로 나눈다.

    "내일 면접을 오후 5시로 옮겨줘" → ("내일 면접을", "오후 5시로 옮겨줘")

    조사 기반 정규식만 쓰면 "내일"의 '내'를 대상으로 잘라 버리는 문제가 있어서,
    시간 표현의 위치를 기준으로 자른다. 시간 표현이 하나뿐이면 그것이 '바꿀 값'이고
    나머지 문장이 '대상'이다.
    """
    parsed = parse_time_expression(text)
    spans = sorted(parsed.spans, key=lambda s: s.start)

    if len(spans) >= 2:
        cut = spans[-1].start
        return text[:cut].strip(), text[cut:].strip()

    if len(spans) == 1:
        span = spans[0]
        target = f"{text[: span.start]} {text[span.end :]}".strip()
        return target, text[span.start :].strip()

    # 시간 표현이 없는 경우(제목 변경 등)만 조사 패턴으로 시도
    m = _CHANGE_RE.search(text)
    if m:
        return m.group("target").strip(), m.group("new").strip()
    return text.strip(), ""


@mcp.tool()
async def update_schedule(text: str, force: bool = False) -> dict:
    """기존 일정의 시간이나 제목을 변경한다.

    Args:
        text: "내일 면접을 4시로 변경해줘" 같은 문장
        force: True면 변경 후 시간이 겹쳐도 강제 반영
    """
    target_part, new_part = _split_update(text)
    target_time = parse_time_expression(target_part)
    title_hint = extract_title(target_part, target_time.spans, fallback="")
    new_time = parse_time_expression(new_part) if new_part else None

    if new_time is None or not new_time.found:
        return _fail(
            "update",
            "new_time_not_found",
            "무엇으로 바꿀지 알 수 없습니다. '내일 회의를 오후 4시로 변경'처럼 말해 주세요.",
        )

    date_hint = target_time.start_at.date() if target_time.found else None
    search_from = now_kst() - timedelta(days=1)
    try:
        candidates = await api.list(start_from=search_from)
    except ApiError as exc:
        return _fail("update", "api_error", f"일정을 불러오지 못했습니다: {exc.detail}")

    matched = match_schedule(candidates, title_hint=title_hint, date_hint=date_hint)
    if matched.best is None:
        return _fail(
            "update",
            "not_found",
            f"'{title_hint or text}'에 해당하는 일정을 찾지 못했습니다.",
            candidates=matched.candidates,
        )
    if matched.ambiguous:
        return _fail(
            "update",
            "ambiguous",
            "비슷한 일정이 여러 개입니다. 어떤 일정인지 알려주세요: "
            + " / ".join(_summarize(c) for c in matched.candidates[:3]),
            candidates=matched.candidates,
            needs_confirmation=True,
        )

    target = matched.best
    old_start = datetime.fromisoformat(target["start_at"])
    old_end = datetime.fromisoformat(target["end_at"])
    duration = old_end - old_start

    new_start = new_time.start_at
    if new_time.all_day:
        # "다음주 월요일로 옮겨줘" → 날짜만 바꾸고 시각은 유지
        new_start = new_start.replace(hour=old_start.hour, minute=old_start.minute)
    new_end = new_time.end_at if new_time.is_range else new_start + duration

    try:
        updated = await api.update(
            target["id"], start_at=new_start, end_at=new_end, force=force
        )
    except ApiError as exc:
        if exc.status_code == 409:
            conflicts = exc.detail.get("conflicts", []) if isinstance(exc.detail, dict) else []
            return _fail(
                "update",
                "conflict",
                "옮기려는 시간에 " + ", ".join(_summarize(c) for c in conflicts) + " 일정이 있습니다. 그래도 옮길까요?",
                conflicts=conflicts,
                needs_confirmation=True,
            )
        return _fail("update", "api_error", f"수정에 실패했습니다: {exc.detail}")

    return _ok(
        "update",
        f"'{target['title']}' 일정을 {format_korean(old_start)}에서 {format_korean(new_start)}로 옮겼습니다.",
        schedule=updated,
        before=target,
        match_score=matched.score,
    )


# ---------------------------------------------------------------------------
# Tool 4. 일정 삭제
# ---------------------------------------------------------------------------
@mcp.tool()
async def delete_schedule(text: str, schedule_id: Optional[int] = None) -> dict:
    """일정을 삭제한다.

    Args:
        text: "내일 면접 취소해줘" 같은 문장
        schedule_id: 이미 특정된 일정 ID가 있으면 문장 해석을 건너뛴다
    """
    if schedule_id is not None:
        try:
            await api.delete(schedule_id)
        except ApiError as exc:
            return _fail("delete", "api_error", f"삭제에 실패했습니다: {exc.detail}")
        return _ok("delete", f"일정 {schedule_id}을(를) 삭제했습니다.", deleted_id=schedule_id)

    parsed = parse_time_expression(text)
    title_hint = extract_title(text, parsed.spans, fallback="")
    date_hint = parsed.start_at.date() if parsed.found else None

    try:
        candidates = await api.list(start_from=now_kst() - timedelta(days=1))
    except ApiError as exc:
        return _fail("delete", "api_error", f"일정을 불러오지 못했습니다: {exc.detail}")

    matched = match_schedule(candidates, title_hint=title_hint, date_hint=date_hint)
    if matched.best is None:
        return _fail(
            "delete",
            "not_found",
            f"'{title_hint or text}'에 해당하는 일정을 찾지 못했습니다.",
            candidates=matched.candidates,
        )
    if matched.ambiguous:
        return _fail(
            "delete",
            "ambiguous",
            "어떤 일정을 지울지 확실하지 않습니다: "
            + " / ".join(_summarize(c) for c in matched.candidates[:3]),
            candidates=matched.candidates,
            needs_confirmation=True,
        )

    target = matched.best
    try:
        await api.delete(target["id"])
    except ApiError as exc:
        return _fail("delete", "api_error", f"삭제에 실패했습니다: {exc.detail}")

    return _ok(
        "delete",
        f"{_summarize(target)} 일정을 삭제했습니다.",
        deleted=target,
        match_score=matched.score,
    )


# ---------------------------------------------------------------------------
# Tool 5. 파싱 결과 확인 (디버깅/데모용)
# ---------------------------------------------------------------------------
@mcp.tool()
async def parse_only(text: str) -> dict:
    """문장을 실행 없이 파싱만 해서 결과를 보여준다."""
    parsed = parse_time_expression(text)
    return _ok(
        "parse",
        "파싱 결과입니다.",
        parsed=parsed.to_dict(),
        title=extract_title(text, parsed.spans),
        now=now_kst().isoformat(),
    )


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
