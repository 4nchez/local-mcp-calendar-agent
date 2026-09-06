"""In-Memory 일정 저장소.

현재는 프로세스 메모리(dict)를 사용한다.
StoreProtocol 형태로 좁혀 두었기 때문에 PostgreSQL 등으로 교체할 때
이 파일만 갈아끼우면 상위 계층(main.py)은 수정이 필요 없다.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import Schedule, ScheduleCreate, ScheduleUpdate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """naive datetime은 UTC로 간주해 비교 시 예외가 나지 않도록 맞춘다."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """반열린 구간 [start, end) 기준 겹침 판정.

    끝나는 시각과 시작 시각이 같은 경우(14:00 종료 / 14:00 시작)는 충돌이 아니다.
    """
    return _aware(a_start) < _aware(b_end) and _aware(b_start) < _aware(a_end)


class ScheduleStore:
    def __init__(self) -> None:
        self._items: dict[int, Schedule] = {}
        self._seq = 0
        self._lock = threading.RLock()

    # --- 기본 CRUD -------------------------------------------------
    def create(self, payload: ScheduleCreate) -> Schedule:
        with self._lock:
            self._seq += 1
            now = _now()
            item = Schedule(
                id=self._seq,
                created_at=now,
                updated_at=now,
                **payload.model_dump(),
            )
            self._items[item.id] = item
            return item

    def get(self, schedule_id: int) -> Optional[Schedule]:
        return self._items.get(schedule_id)

    def update(self, schedule_id: int, payload: ScheduleUpdate) -> Optional[Schedule]:
        with self._lock:
            item = self._items.get(schedule_id)
            if item is None:
                return None
            data = item.model_dump()
            data.update(payload.model_dump(exclude_unset=True, exclude_none=True))
            data["updated_at"] = _now()
            updated = Schedule(**data)
            if _aware(updated.end_at) <= _aware(updated.start_at):
                raise ValueError("end_at은 start_at보다 뒤여야 합니다.")
            self._items[schedule_id] = updated
            return updated

    def delete(self, schedule_id: int) -> Optional[Schedule]:
        with self._lock:
            return self._items.pop(schedule_id, None)

    # --- 조회 ------------------------------------------------------
    def list(
        self,
        start_from: Optional[datetime] = None,
        start_to: Optional[datetime] = None,
        keyword: Optional[str] = None,
    ) -> list[Schedule]:
        result: Iterable[Schedule] = self._items.values()
        if start_from is not None:
            result = [s for s in result if _aware(s.end_at) > _aware(start_from)]
        if start_to is not None:
            result = [s for s in result if _aware(s.start_at) < _aware(start_to)]
        if keyword:
            k = keyword.strip().lower()
            result = [s for s in result if k in s.title.lower()]
        return sorted(result, key=lambda s: _aware(s.start_at))

    # --- 충돌 검증 -------------------------------------------------
    def find_conflicts(
        self, start_at: datetime, end_at: datetime, exclude_id: Optional[int] = None
    ) -> list[Schedule]:
        found = [
            s
            for s in self._items.values()
            if s.id != exclude_id and overlaps(start_at, end_at, s.start_at, s.end_at)
        ]
        return sorted(found, key=lambda s: _aware(s.start_at))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._seq = 0
