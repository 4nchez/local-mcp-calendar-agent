"""MCP Client — Agent Layer.

사용자 요청 하나가 처리되는 전체 흐름을 이 파일이 통제한다.

    입력 → Intent 분류(규칙 우선) → Tool 선택 → MCP 호출 → 응답 생성

핵심 설계는 "LLM에게 Tool 선택권을 주지 않는다"는 것이다.
어떤 Tool을 부를지는 Router가 결정하고, LLM은 분류 보조와 문장 생성만 맡는다.
그래서 같은 문장을 열 번 넣어도 같은 Tool이 실행된다.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import router as intent_router
from .llm import OllamaClient
from .mcp_gateway import McpGateway
from .router import (
    CANCEL,
    CONFIRM,
    CREATE,
    DELETE,
    HELP,
    LIST,
    UNKNOWN,
    UPDATE,
    IntentDecision,
)

API_BASE_URL = os.getenv("API_BASE_URL", "http://api-server:8000")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ollama 컨테이너가 늦게 뜨거나 재생성 중일 수 있으므로 넉넉히 기다린다.
    # 30회 x 10초 = 5분. 실패해도 첫 요청에서 로드되므로 문제는 없다.
    asyncio.create_task(llm.warmup(retries=30, delay=10.0))
    yield


app = FastAPI(title="MCP Client (Agent)", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

gateway = McpGateway()
llm = OllamaClient()

# Intent → Tool 매핑. 이 표에 없는 Intent는 절대 Tool을 호출하지 않는다.
TOOL_BY_INTENT = {
    CREATE: "create_schedule",
    LIST: "list_schedules",
    UPDATE: "update_schedule",
    DELETE: "delete_schedule",
}

HELP_TEXT = (
    "이렇게 말해 보세요.\n"
    "· 등록: 내일 오후 2시에 면접 일정 추가해줘\n"
    "· 조회: 이번주 일정 알려줘\n"
    "· 수정: 내일 면접을 4시로 옮겨줘\n"
    "· 삭제: 내일 면접 취소해줘"
)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    session_id: Optional[str] = None


class Trace:
    """대시보드의 실행 파이프라인 표시에 쓰이는 단계별 기록."""

    STAGES = [
        ("input", "입력 접수"),
        ("intent", "Intent 분류"),
        ("tool", "Tool 실행"),
        ("api", "API 처리"),
        ("reply", "응답 생성"),
    ]

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._marks: dict[str, float] = {}

    def start(self, stage: str) -> None:
        self._marks[stage] = time.perf_counter()

    def done(self, stage: str, status: str = "ok", detail: str = "") -> None:
        started = self._marks.pop(stage, None)
        label = dict(self.STAGES).get(stage, stage)
        self.entries.append(
            {
                "stage": stage,
                "label": label,
                "status": status,
                "detail": detail,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1) if started else None,
            }
        )


# 세션별 대기 중인 확인 요청 (시간 충돌 등)
SESSIONS: dict[str, dict] = {}


async def _fetch_upcoming(days: int = 14) -> list[dict]:
    now = datetime.now(timezone.utc)
    params = {
        "start_from": (now - timedelta(hours=12)).isoformat(),
        "start_to": (now + timedelta(days=days)).isoformat(),
    }
    try:
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=5) as client:
            res = await client.get("/schedules", params=params)
            res.raise_for_status()
            return res.json()
    except Exception:  # noqa: BLE001
        return []


async def _decide_intent(message: str, trace: Trace) -> IntentDecision:
    decision = intent_router.route(message)
    if decision.intent != UNKNOWN:
        trace.done("intent", "ok", f"{decision.intent} · {decision.reason}")
        return decision

    raw = await llm.classify_intent(message)
    if raw:
        intent = intent_router.normalize_llm_intent(raw)
        if intent != UNKNOWN:
            trace.done("intent", "ok", f"{intent} · LLM 보조 분류 (원문: {raw[:20]})")
            return IntentDecision(
                intent=intent,
                confidence=0.55,
                source="llm",
                matched=["LLM"],
                reason="규칙 미일치로 LLM에 위임",
            )
    trace.done("intent", "warn", "분류 실패 · 사용자에게 되물음")
    return IntentDecision(UNKNOWN, 0.0, "fallback", reason="규칙·LLM 모두 판단 불가")


def _resolve_confirmation(session: dict, decision: IntentDecision) -> Optional[tuple[str, dict]]:
    """확인 응답을 대기 중이던 작업으로 되돌린다."""
    pending = session.get("pending")
    if not pending:
        return None
    if decision.intent == CONFIRM:
        return pending["tool"], {**pending["arguments"], "force": True}
    return None


@app.post("/chat")
async def chat(req: ChatRequest) -> dict:
    session_id = req.session_id or str(uuid.uuid4())
    session = SESSIONS.setdefault(session_id, {"pending": None, "history": []})
    message = req.message.strip()

    trace = Trace()
    trace.start("input")
    trace.done("input", "ok", f"{len(message)}자")

    trace.start("intent")
    decision = await _decide_intent(message, trace)

    tool_calls: list[dict] = []
    tool_result: dict = {}
    reply: str

    # --- 확인 응답 처리 (충돌 무시 등) ---------------------------------
    forced = _resolve_confirmation(session, decision)

    if decision.intent == CANCEL and session.get("pending"):
        session["pending"] = None
        trace.done("tool", "skip", "사용자가 취소")
        trace.done("api", "skip", "-")
        trace.start("reply")
        reply = "요청을 취소했습니다."
        trace.done("reply", "ok", "템플릿")
        return _response(session_id, reply, decision, tool_calls, trace, await _fetch_upcoming())

    if decision.intent in (CONFIRM, CANCEL) and not forced:
        trace.done("tool", "skip", "대기 중인 작업 없음")
        trace.done("api", "skip", "-")
        trace.start("reply")
        reply = "확인할 작업이 없습니다. 무엇을 도와드릴까요?"
        trace.done("reply", "ok", "템플릿")
        return _response(session_id, reply, decision, tool_calls, trace, await _fetch_upcoming())

    if decision.intent == HELP:
        trace.done("tool", "skip", "도움말")
        trace.done("api", "skip", "-")
        trace.start("reply")
        trace.done("reply", "ok", "템플릿")
        return _response(session_id, HELP_TEXT, decision, tool_calls, trace, await _fetch_upcoming())

    if decision.intent == UNKNOWN:
        trace.done("tool", "skip", "Tool 미선택")
        trace.done("api", "skip", "-")
        trace.start("reply")
        reply = "요청을 이해하지 못했습니다.\n" + HELP_TEXT
        trace.done("reply", "ok", "템플릿")
        return _response(session_id, reply, decision, tool_calls, trace, await _fetch_upcoming())

    # --- Tool 실행 -----------------------------------------------------
    if forced:
        tool_name, arguments = forced
        session["pending"] = None
    else:
        tool_name = TOOL_BY_INTENT[decision.intent]
        arguments = {"text": message}

    trace.start("tool")
    started = time.perf_counter()
    try:
        tool_result = await gateway.call_tool(tool_name, arguments)
        ok = bool(tool_result.get("ok"))
        trace.done("tool", "ok" if ok else "warn", f"{tool_name}()")
    except Exception as exc:  # noqa: BLE001
        trace.done("tool", "error", f"{tool_name}() 실패")
        tool_result = {
            "ok": False,
            "reason": "mcp_unreachable",
            "message": f"도구 서버에 연결하지 못했습니다: {str(exc)[:120]}",
        }
        ok = False

    tool_calls.append(
        {
            "tool": tool_name,
            "arguments": arguments,
            "ok": ok,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "result": tool_result,
        }
    )

    trace.start("api")
    trace.done(
        "api",
        "ok" if ok else ("warn" if tool_result.get("reason") != "mcp_unreachable" else "error"),
        f"{tool_result.get('action', '-')} · {tool_result.get('reason', 'success')}",
    )

    if tool_result.get("needs_confirmation"):
        session["pending"] = {"tool": tool_name, "arguments": arguments}

    # --- 응답 생성 -----------------------------------------------------
    trace.start("reply")
    fallback = tool_result.get("message") or "처리를 완료했습니다."
    generated = await llm.compose_reply(message, tool_name, tool_result)
    if generated:
        reply = generated
        trace.done("reply", "ok", "LLM 생성")
    else:
        reply = fallback
        trace.done("reply", "warn", f"LLM 실패({llm.last_error}) · 템플릿 사용")

    session["history"] = (session["history"] + [message])[-10:]
    return _response(
        session_id, reply, decision, tool_calls, trace, await _fetch_upcoming(),
        needs_confirmation=bool(tool_result.get("needs_confirmation")),
    )


def _response(
    session_id: str,
    reply: str,
    decision: IntentDecision,
    tool_calls: list[dict],
    trace: Trace,
    schedules: list[dict],
    needs_confirmation: bool = False,
) -> dict:
    return {
        "session_id": session_id,
        "reply": reply,
        "intent": decision.to_dict(),
        "tool_calls": tool_calls,
        "trace": trace.entries,
        "schedules": schedules,
        "needs_confirmation": needs_confirmation,
    }


@app.get("/schedules")
async def schedules(days: int = 14) -> list[dict]:
    return await _fetch_upcoming(days)


@app.get("/tools")
async def tools() -> dict:
    try:
        return {"ok": True, "tools": await gateway.list_tools(refresh=True)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200], "tools": []}


@app.get("/health")
async def health() -> dict:
    async def api_health() -> dict:
        try:
            async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=3) as client:
                res = await client.get("/health")
                return {"status": "ok" if res.status_code == 200 else "down"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "down", "error": str(exc)[:80]}

    async def mcp_health() -> dict:
        try:
            tool_list = await gateway.list_tools(refresh=True)
            return {"status": "ok", "tools": [t["name"] for t in tool_list]}
        except Exception as exc:  # noqa: BLE001
            return {"status": "down", "error": str(exc)[:80]}

    api_state, mcp_state, llm_state = await asyncio.gather(
        api_health(), mcp_health(), llm.health()
    )
    overall = "ok" if api_state["status"] == "ok" and mcp_state["status"] == "ok" else "degraded"
    return {
        "status": overall,
        "service": "mcp-client",
        "components": {"api": api_state, "mcp": mcp_state, "llm": llm_state},
    }
