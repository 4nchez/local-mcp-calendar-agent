"""Local LLM (Ollama) 클라이언트.

LLM에게 맡기는 일은 딱 두 가지다.
    1) 규칙이 판단하지 못한 문장의 Intent 보조 분류
    2) Tool 실행 결과를 자연스러운 한국어 문장으로 다듬기

일정 데이터를 만들거나 Tool을 직접 고르게 하지 않는다.
LLM이 죽어 있어도 시스템은 템플릿 응답으로 계속 동작한다.

다만 "조용히 폴백"은 장애를 감춘다. 실패 사유를 last_error에 남겨
trace의 detail로 노출한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional, Union

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").lower() == "true"

INTENT_SYSTEM = """너는 일정 관리 비서의 의도 분류기다.
사용자 문장을 아래 라벨 중 하나로만 분류해라.

create_schedule: 새 일정을 추가/등록
list_schedules: 일정을 조회/확인
update_schedule: 기존 일정의 시간이나 내용을 변경
delete_schedule: 기존 일정을 삭제/취소
help: 사용법 질문
unknown: 위 어디에도 해당하지 않음

규칙: 라벨 하나만 출력한다. 설명, 문장부호, 따옴표를 붙이지 않는다."""

REPLY_SYSTEM = """너는 한국어 일정 관리 비서다.
도구 실행 결과(JSON)를 사용자에게 한두 문장으로 전달해라.

규칙:
- 결과에 있는 사실만 말한다. 날짜, 시간, 제목을 새로 지어내지 않는다.
- 존댓말로 간결하게 쓴다. 인사말이나 사족을 붙이지 않는다.
- 실패한 결과라면 이유와 다음에 무엇을 하면 되는지 알려준다.
- JSON이나 코드 블록을 그대로 출력하지 않는다."""


def keep_alive_value() -> Union[str, int]:
    """Ollama의 keep_alive 값을 만든다.

    '30m' 같은 duration은 문자열로, 무기한을 뜻하는 -1은 숫자로 보내야 한다.
    문자열 '-1'을 그대로 넘기면 Ollama가 duration 파싱에 실패해 400을 돌려준다.
    """
    raw = os.getenv("LLM_KEEP_ALIVE", "30m").strip()
    try:
        return int(raw)
    except ValueError:
        return raw


class OllamaClient:
    def __init__(self, host: str = OLLAMA_HOST, model: str = OLLAMA_MODEL) -> None:
        self.host = host
        self.model = model
        self.last_error: Optional[str] = None

    async def _chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 200,
        temperature: float = 0.2,
    ) -> Optional[str]:
        if not LLM_ENABLED:
            self.last_error = "LLM 비활성화"
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "keep_alive": keep_alive_value(),
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }

        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
                res = await client.post(f"{self.host}/api/chat", json=payload)
        except httpx.TimeoutException:
            self.last_error = f"{LLM_TIMEOUT:.0f}초 타임아웃"
            logger.warning("LLM 타임아웃: %ss", LLM_TIMEOUT)
            return None
        except Exception as exc:  # noqa: BLE001 - LLM 장애는 치명적이지 않아야 한다
            self.last_error = f"연결 실패: {type(exc).__name__}"
            logger.warning("LLM 연결 실패: %s", exc)
            return None

        if res.status_code != 200:
            # 400은 대부분 payload 문제다. 본문을 남겨 두지 않으면
            # "LLM 미응답"으로만 보여서 원인을 찾는 데 오래 걸린다.
            detail = res.text[:200].replace("\n", " ")
            self.last_error = f"HTTP {res.status_code}: {detail}"
            logger.warning("LLM 오류 응답 %s: %s", res.status_code, detail)
            return None

        try:
            content = (res.json().get("message") or {}).get("content", "").strip()
        except json.JSONDecodeError:
            self.last_error = "응답 파싱 실패"
            return None

        if not content:
            self.last_error = "빈 응답"
            return None

        self.last_error = None
        return content

    async def classify_intent(self, text: str) -> Optional[str]:
        return await self._chat(INTENT_SYSTEM, text, max_tokens=10, temperature=0.0)

    async def compose_reply(self, user_text: str, intent: str, tool_result: dict) -> Optional[str]:
        compact = json.dumps(tool_result, ensure_ascii=False)[:1500]
        prompt = f"사용자 요청: {user_text}\n실행한 도구: {intent}\n실행 결과: {compact}"
        return await self._chat(REPLY_SYSTEM, prompt, max_tokens=220, temperature=0.3)

    async def warmup(self, retries: int = 10, delay: float = 5.0) -> None:
        """기동 직후 모델을 메모리에 올려 첫 요청의 지연을 없앤다.

        ollama 컨테이너가 아직 요청을 받을 준비가 안 됐을 수 있으므로 재시도한다.
        (컨테이너가 뜬 것과 서비스가 준비된 것은 다르다)
        끝내 실패해도 시스템은 정상 동작하므로 조용히 포기한다.
        """
        if not LLM_ENABLED:
            return

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "keep_alive": keep_alive_value(),
            "options": {"num_predict": 1},
        }

        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    res = await client.post(f"{self.host}/api/chat", json=payload)
                if res.status_code == 200:
                    logger.info("LLM 예열 완료 (%s, %d회 시도)", self.model, attempt)
                    return
                logger.warning(
                    "LLM 예열 실패 %d/%d — HTTP %s: %s",
                    attempt, retries, res.status_code, res.text[:200],
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("LLM 예열 대기 %d/%d — %s", attempt, retries, exc)
            await asyncio.sleep(delay)

        logger.warning("LLM 예열을 포기했습니다. 첫 요청에서 모델이 로드됩니다.")

    async def health(self) -> dict:
        if not LLM_ENABLED:
            return {"status": "disabled", "model": self.model}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.get(f"{self.host}/api/tags")
                res.raise_for_status()
                models = [m.get("name") for m in res.json().get("models", [])]
        except Exception as exc:  # noqa: BLE001
            return {"status": "down", "model": self.model, "error": str(exc)[:120]}

        ready = any((m or "").startswith(self.model.split(":")[0]) for m in models)
        state = {
            "status": "ok" if ready else "model_missing",
            "model": self.model,
            "available": models,
        }
        if self.last_error:
            state["last_error"] = self.last_error
        return state