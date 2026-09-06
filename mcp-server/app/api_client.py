"""API Server 호출 클라이언트.

MCP Server는 DB를 직접 만지지 않는다. 항상 API Server를 통한다.
(비즈니스 로직이 두 곳에 흩어지는 것을 막기 위한 제약)
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

import httpx

API_BASE_URL = os.getenv("API_BASE_URL", "http://api-server:8000")
TIMEOUT = float(os.getenv("API_TIMEOUT", "10"))


class ApiError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API {status_code}: {detail}")


class ScheduleApiClient:
    def __init__(self, base_url: str = API_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=TIMEOUT) as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:  # noqa: BLE001
                detail = response.text
            raise ApiError(response.status_code, detail)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def health(self) -> dict:
        return await self._request("GET", "/health")

    async def create(
        self,
        title: str,
        start_at: datetime,
        end_at: Optional[datetime] = None,
        force: bool = False,
        **extra,
    ) -> dict:
        payload = {
            "title": title,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat() if end_at else None,
            **extra,
        }
        return await self._request("POST", "/schedules", json=payload, params={"force": force})

    async def list(
        self,
        start_from: Optional[datetime] = None,
        start_to: Optional[datetime] = None,
        keyword: Optional[str] = None,
    ) -> list[dict]:
        params: dict[str, str] = {}
        if start_from:
            params["start_from"] = start_from.isoformat()
        if start_to:
            params["start_to"] = start_to.isoformat()
        if keyword:
            params["keyword"] = keyword
        return await self._request("GET", "/schedules", params=params) or []

    async def update(self, schedule_id: int, force: bool = False, **fields) -> dict:
        payload = {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in fields.items()
            if v is not None
        }
        return await self._request(
            "PATCH", f"/schedules/{schedule_id}", json=payload, params={"force": force}
        )

    async def delete(self, schedule_id: int) -> None:
        await self._request("DELETE", f"/schedules/{schedule_id}")
