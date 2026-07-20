"""Cloud LLM adapter for the offline-first irrigation system.

The implementation uses the OpenAI-compatible HTTP shape exposed by the
Volcengine gateway, but keeps the transport in this module so another
provider can be added without changing the safety layer.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .schemas import DecisionContext, IrrigationDecision


SYSTEM_PROMPT = """你是智能灌溉系统的云端分析模块。只能依据用户提供的 JSON 数据回答。
灌溉动作只能返回一个严格 JSON 对象，字段必须是 schemaVersion、requestId、action、
durationSeconds、reasonCode、reason、confidence、expiresAt；action 只能为
START_WATERING、STOP_WATERING、NO_OP。不要使用 Markdown 代码围栏，不要添加额外字段。
数据不完整、传感器异常或没有明确必要时必须返回 NO_OP。云端不能直接控制硬件，动作会经过本地安全审核。"""


class CloudFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudCall:
    content: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class OpenAICompatibleGateway:
    """Small stdlib-only client for a Volcengine/OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings, *, api_key: str | None = None):
        self.settings = settings
        self.api_key = api_key or os.getenv("VEI_API_KEY")

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.llm_enabled
            and self.api_key
            and self.settings.gateway_base_url
            and self.settings.gateway_model
        )

    def _call(self, messages: list[dict[str, str]], *, max_tokens: int) -> CloudCall:
        if not self.configured:
            raise CloudFailure(
                "cloud LLM is disabled or VEI_API_KEY/VEI_BASE_URL/VEI_MODEL is not configured"
            )
        url = self.settings.gateway_base_url.rstrip("/") + "/chat/completions"
        payload = json.dumps({
            "model": self.settings.gateway_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.settings.gateway_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise CloudFailure(f"gateway request failed: {type(exc).__name__}") from exc
        try:
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage") or {}
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty model content")
            return CloudCall(
                content=content.strip(),
                latency_ms=round((time.monotonic() - started) * 1000),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise CloudFailure("gateway response has no usable message") from exc

    def irrigation_decision(self, context: DecisionContext) -> tuple[IrrigationDecision, CloudCall]:
        call = self._call([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context.model_dump_json()},
        ], max_tokens=600)
        try:
            decision = IrrigationDecision.model_validate_json(call.content)
        except ValueError as exc:
            raise CloudFailure("model output is not the required irrigation JSON") from exc
        return decision, call

    def chat(self, question: str, context: dict[str, Any]) -> CloudCall:
        return self._call([
            {"role": "system", "content": "只能根据 JSON 事实回答中文问题，引用数据时间范围；无法从数据得出时明确说数据不足。"},
            {"role": "user", "content": json.dumps({"question": question, "evidence": context}, ensure_ascii=False)},
        ], max_tokens=700)


def new_request_id(prefix: str = "llm") -> str:
    return f"{prefix}-{uuid.uuid4()}"
