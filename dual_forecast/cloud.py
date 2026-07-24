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
只有 constraints.edgeRisk.riskLevel 为 IRRIGATION_CANDIDATE 时才可建议 START_WATERING；
数据不完整、传感器异常或没有明确必要时必须返回 NO_OP。
action 表示“基于环境数据给出的灌溉建议”，不是直接控制硬件的命令。是否需要人工确认、
是否启用自动模式以及云端没有硬件控制权，都不得影响 action，也不得作为 reasonCode 或 reason。
例如：环境与预测支持灌溉时，即使自动模式关闭，也应返回 START_WATERING，之后由本地安全层决定是否下发。
NO_OP 的原因必须是明确的环境、传感器、预测或灌溉必要性依据，不能是执行权限或确认流程。"""


_GOVERNANCE_REASON_CODE_MARKERS = (
    "CANNOT_DIRECT_CONTROL",
    "DIRECT_HARDWARE",
    "HARDWARE_CONTROL",
    "HARDWARE_PERMISSION",
    "MANUAL_CONFIRM",
    "AUTO_MODE",
    "AUTOMATIC_MODE",
    "AUTHORIZATION_REQUIRED",
)
_GOVERNANCE_REASON_TEXT_MARKERS = (
    "云端不允许直接控制",
    "云端不能直接控制",
    "无权直接控制",
    "需要人工确认或",
    "人工确认或部署者",
    "启用自动模式后下发",
    "cannot directly control hardware",
    "manual confirmation or automatic mode",
)


def is_execution_governance_reason(*, action: str, reason_code: str, reason: str) -> bool:
    """Detect a recommendation that incorrectly uses execution governance."""
    if action != "NO_OP":
        return False
    code = reason_code.upper()
    reason_text = reason.lower()
    return (
        any(marker in code for marker in _GOVERNANCE_REASON_CODE_MARKERS)
        or any(marker.lower() in reason_text for marker in _GOVERNANCE_REASON_TEXT_MARKERS)
    )


def _is_execution_governance_no_op(decision: IrrigationDecision) -> bool:
    """Reject a NO_OP that confuses recommendation with execution authority."""
    return is_execution_governance_reason(
        action=decision.action.value,
        reason_code=decision.reasonCode,
        reason=decision.reason,
    )


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
            "- action 只表示是否建议灌溉；人工确认、自动模式和硬件控制权限不得作为 NO_OP 的理由\n"
            "只输出一个 JSON 对象；不要解释，不要 Markdown。"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context.model_dump_json()},
            {"role": "user", "content": contract},
        ]
        last_validation_error: ValueError | None = None
        for attempt in range(2):
            call = self._call(messages, max_tokens=600)
            # Some OpenAI-compatible models still wrap an otherwise valid
            # object in Markdown. Accept the object but keep schema validation
            # strict, then separately reject recommendation/authority confusion.
            decoder = json.JSONDecoder()
            decision: IrrigationDecision | None = None
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
                    decision = IrrigationDecision.model_validate(candidate)
                    break
                except ValueError as exc:
                    last_validation_error = exc
            if decision is None:
                raise CloudFailure("model output is not the required irrigation JSON") from last_validation_error
            if not _is_execution_governance_no_op(decision):
                return decision, call
            if attempt == 0:
                messages.extend([
                    {"role": "assistant", "content": call.content},
                    {"role": "user", "content": (
                        "上一回答无效：你把云端硬件权限、人工确认或自动模式当成了 NO_OP 的理由。"
                        "请重新只根据 current、trends、forecast 和 constraints.edgeRisk 判断是否建议灌溉。"
                        "action 是建议而非硬件命令；若环境依据支持灌溉，应返回 START_WATERING，"
                        "本地安全层会另行决定是否执行。NO_OP 必须给出具体的数据或环境依据。"
                        "仍须遵守原输出合约，只输出 JSON。"
                    )},
                ])
                continue
            raise CloudFailure("model used execution authority as the irrigation recommendation reason")

        raise CloudFailure("model did not return a usable irrigation recommendation")

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
