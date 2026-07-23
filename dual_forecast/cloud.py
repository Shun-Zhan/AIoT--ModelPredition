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
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .schemas import DecisionContext, IrrigationDecision


SYSTEM_PROMPT = """你是智能灌溉系统的云端分析模块。只能依据用户提供的 JSON 数据回答。
农田档案可用于解释建议，但缺失字段必须明确视为未知，不能补充常识猜测。
weather.status 为 not_configured 时，不得声称知道天气、降雨、地理位置或天气预报。
灌溉动作只能返回一个严格 JSON 对象，字段必须是 schemaVersion、requestId、action、
durationSeconds、reasonCode、reason、confidence、expiresAt；action 只能为
START_WATERING、STOP_WATERING、NO_OP。不要使用 Markdown 代码围栏，不要添加额外字段。
数据不完整、传感器异常或没有明确必要时必须返回 NO_OP。云端不能直接控制硬件；动作仅可由本地安全层在人工确认或部署者显式启用自动模式后下发。"""


class CloudFailure(RuntimeError):
    pass


class CloudConfigurationFailure(CloudFailure):
    """The saved credential or configured model cannot be used."""


class CloudNetworkFailure(CloudFailure):
    """The cloud gateway cannot currently be reached; retain local config."""


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
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                raise CloudConfigurationFailure(f"gateway rejected credentials or model (HTTP {exc.code})") from exc
            raise CloudFailure(f"gateway request failed: HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise CloudNetworkFailure(f"gateway is unreachable: {type(exc).__name__}") from exc
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
        # Reasoning models occasionally invent ``0`` / ``null`` for fields
        # which must be unambiguous JSON values.  Give the model two values
        # that are safe only for this short-lived request and require it to
        # copy them exactly.  The local review still checks the request ID,
        # expiry, field schema, sensor validity and every irrigation limit.
        required_expiry = (datetime.now(timezone.utc) + timedelta(seconds=55)).isoformat()
        contract = (
            "输出合约（必须逐字遵守）：\n"
            f"- requestId 必须是 {context.requestId}\n"
            f"- expiresAt 必须是 {required_expiry}\n"
            "- action 为 NO_OP 或 STOP_WATERING 时，durationSeconds 必须是 JSON 的 null，绝不能是 0\n"
            "- action 为 START_WATERING 时，durationSeconds 必须是 1 到 60 的整数\n"
            "- confidence 必须是 0.0 到 1.0 之间的 JSON 数字，绝不能是 null\n"
            "- reasonCode 只能包含大写字母、数字和下划线\n"
            "只输出一个 JSON 对象；不要解释，不要 Markdown。"
        )
        call = self._call([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context.model_dump_json()},
            {"role": "user", "content": contract},
        ], max_tokens=600)
        # Some OpenAI-compatible models still wrap an otherwise valid object
        # in a Markdown fence or one sentence of explanation.  Accept only a
        # JSON *object* found in that wrapper, then keep the Pydantic schema
        # validation strict: no extra fields, no malformed actions and no
        # missing safety metadata can pass through this compatibility layer.
        decoder = json.JSONDecoder()
        validation_error: ValueError | None = None
        for offset, character in enumerate(call.content):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(call.content[offset:])
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            try:
                return IrrigationDecision.model_validate(candidate), call
            except ValueError as exc:
                validation_error = exc

        raise CloudFailure("model output is not the required irrigation JSON") from validation_error

    def chat(self, question: str, context: dict[str, Any]) -> CloudCall:
        return self._call([
            {"role": "system", "content": (
                "你是智能农田的分析顾问。只能根据 JSON 事实回答中文问题；不得编造天气、作物特性、地理位置或传感器读数。"
                "农田档案 status=not_configured 时，明确说该信息未配置。"
                "回答按四个短段组织：结论、数据依据、建议、局限性；引用数据时间范围与关键数值。"
                "灌溉建议只能是人工参考，必须注明最终开阀仍由本地安全审核和人工确认决定。"
            )},
            {"role": "user", "content": json.dumps({"question": question, "evidence": context}, ensure_ascii=False)},
        ], max_tokens=700)

    def health_check(self) -> CloudCall:
        """Verify endpoint, key and model without requesting an irrigation action."""
        return self._call([
            {"role": "user", "content": "Reply exactly OK."},
        ], max_tokens=8)


def new_request_id(prefix: str = "llm") -> str:
    return f"{prefix}-{uuid.uuid4()}"
