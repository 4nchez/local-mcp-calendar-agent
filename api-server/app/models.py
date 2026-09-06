"""일정 도메인 모델.

API Server는 AI/자연어에 대해 아무것도 모른다.
여기서 다루는 것은 이미 구조화가 끝난 '일정 데이터' 뿐이다.
(자연어 → 구조화는 MCP Server의 책임)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_DURATION_MINUTES = 60


class ScheduleBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    start_at: datetime
    end_at: Optional[datetime] = None
    location: Optional[str] = Field(None, max_length=200)
    memo: Optional[str] = Field(None, max_length=1000)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title은 비어 있을 수 없습니다.")
        return v


class ScheduleCreate(ScheduleBase):
    @model_validator(mode="after")
    def _fill_end(self) -> "ScheduleCreate":
        if self.end_at is None:
            self.end_at = self.start_at + timedelta(minutes=DEFAULT_DURATION_MINUTES)
        if self.end_at <= self.start_at:
            raise ValueError("end_at은 start_at보다 뒤여야 합니다.")
        return self


class ScheduleUpdate(BaseModel):
    """PATCH 용. 전달된 필드만 반영한다."""

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    location: Optional[str] = Field(None, max_length=200)
    memo: Optional[str] = Field(None, max_length=1000)


class Schedule(ScheduleBase):
    id: int
    end_at: datetime
    created_at: datetime
    updated_at: datetime


class ConflictInfo(BaseModel):
    message: str
    requested: dict
    conflicts: list[Schedule]
