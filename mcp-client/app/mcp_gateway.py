"""MCP Server와의 통신 계층.

MCP 표준 SDK(streamable-http)를 사용한다. 서버가 stateless 모드이므로
요청마다 세션을 새로 열고 닫는다. Tool 목록은 캐시해 둔다.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8000/mcp")


class McpGateway:
    def __init__(self, url: str = MCP_SERVER_URL) -> None:
        self.url = url
        self._tools_cache: Optional[list[dict]] = None

    async def list_tools(self, refresh: bool = False) -> list[dict]:
        if self._tools_cache is not None and not refresh:
            return self._tools_cache
        async with streamablehttp_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
        self._tools_cache = [
            {
                "name": t.name,
                "description": (t.description or "").strip().split("\n")[0],
                "input_schema": t.inputSchema,
            }
            for t in result.tools
        ]
        return self._tools_cache

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        async with streamablehttp_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
        return self._unwrap(result)

    @staticmethod
    def _unwrap(result: Any) -> dict:
        """MCP CallToolResult → 평범한 dict."""
        if getattr(result, "isError", False):
            text = _first_text(result)
            return {"ok": False, "reason": "tool_error", "message": text or "도구 실행에 실패했습니다."}

        text = _first_text(result)
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            inner = structured.get("result", structured)
            if isinstance(inner, dict):
                return inner

        return {"ok": True, "message": text or "", "raw": True}


def _first_text(result: Any) -> str:
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""
