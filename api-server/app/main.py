"""API Server — 일정 데이터의 CRUD와 비즈니스 로직(시간 충돌 검증)을 담당.

이 서버는 LLM/MCP를 전혀 모른다. 순수한 도메인 서버로 유지하는 것이
'AI 로직과 비즈니스 로직의 분리' 목표의 핵심이다.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .models import Schedule, ScheduleCreate, ScheduleUpdate
from .store import ScheduleStore

app = FastAPI(
    title="Schedule API Server",
    description="일정 CRUD + 시간 충돌 검증",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

store = ScheduleStore()


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "service": "api-server", "count": len(store.list())}


@app.get("/schedules", response_model=list[Schedule], tags=["schedules"])
def list_schedules(
    start_from: Optional[datetime] = Query(None, description="이 시각 이후에 끝나는 일정"),
    start_to: Optional[datetime] = Query(None, description="이 시각 이전에 시작하는 일정"),
    keyword: Optional[str] = Query(None, description="제목 부분 일치"),
) -> list[Schedule]:
    return store.list(start_from=start_from, start_to=start_to, keyword=keyword)


@app.get("/schedules/{schedule_id}", response_model=Schedule, tags=["schedules"])
def get_schedule(schedule_id: int) -> Schedule:
    item = store.get(schedule_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"일정 {schedule_id}을(를) 찾을 수 없습니다.")
    return item


@app.post(
    "/schedules",
    response_model=Schedule,
    status_code=status.HTTP_201_CREATED,
    tags=["schedules"],
)
def create_schedule(
    payload: ScheduleCreate,
    force: bool = Query(False, description="true면 시간 충돌을 무시하고 생성"),
) -> Schedule:
    assert payload.end_at is not None  # validator가 채워 준다
    if not force:
        conflicts = store.find_conflicts(payload.start_at, payload.end_at)
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "요청한 시간대에 이미 다른 일정이 있습니다.",
                    "conflicts": [c.model_dump(mode="json") for c in conflicts],
                },
            )
    return store.create(payload)


@app.patch("/schedules/{schedule_id}", response_model=Schedule, tags=["schedules"])
def update_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    force: bool = Query(False),
) -> Schedule:
    current = store.get(schedule_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"일정 {schedule_id}을(를) 찾을 수 없습니다.")

    new_start = payload.start_at or current.start_at
    new_end = payload.end_at or current.end_at
    if payload.start_at and not payload.end_at:
        # 시작 시각만 옮기면 기존 소요 시간을 유지한다.
        new_end = payload.start_at + (current.end_at - current.start_at)
        payload = payload.model_copy(update={"end_at": new_end})

    if not force:
        conflicts = store.find_conflicts(new_start, new_end, exclude_id=schedule_id)
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "변경하려는 시간대에 이미 다른 일정이 있습니다.",
                    "conflicts": [c.model_dump(mode="json") for c in conflicts],
                },
            )
    try:
        updated = store.update(schedule_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    assert updated is not None
    return updated


@app.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["schedules"])
def delete_schedule(schedule_id: int) -> Response:
    if store.delete(schedule_id) is None:
        raise HTTPException(status_code=404, detail=f"일정 {schedule_id}을(를) 찾을 수 없습니다.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/schedules/{schedule_id}/conflicts", response_model=list[Schedule], tags=["schedules"])
def conflicts_of(schedule_id: int) -> list[Schedule]:
    item = store.get(schedule_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"일정 {schedule_id}을(를) 찾을 수 없습니다.")
    return store.find_conflicts(item.start_at, item.end_at, exclude_id=schedule_id)


@app.post("/conflict-check", response_model=list[Schedule], tags=["schedules"])
def check_conflict(start_at: datetime, end_at: datetime, exclude_id: Optional[int] = None):
    return store.find_conflicts(start_at, end_at, exclude_id=exclude_id)


if os.getenv("ENABLE_RESET", "true").lower() == "true":

    @app.post("/reset", tags=["system"])
    def reset() -> dict:
        """데모/테스트용 초기화."""
        store.clear()
        return {"status": "cleared"}
