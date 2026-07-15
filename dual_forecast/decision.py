from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

import pandas as pd

from .config import Settings
from .schemas import (
    DecisionContext,
    DecisionResult,
    ForecastResponse,
    IrrigationAction,
    ModelIrrigationDecision,
)


SYSTEM_PROMPT = """你是智能灌溉控制器的决策模块。只能根据用户提供的 JSON 数据决策。
只返回一个 JSON 对象，不要使用 Markdown、代码围栏或额外解释。
返回字段必须严格为 schemaVersion、action、durationSeconds、reasonCode、reason、confidence。
action 只能是 START_WATERING、STOP_WATERING、NO_OP。
START_WATERING 必须提供 1 到 60 的整数 durationSeconds；其他动作必须为 null。
传感器不可靠、数据不足或没有明确浇水必要时选择 NO_OP。
reason 使用简短中文，reasonCode 使用大写英文和下划线。"""


@dataclass(frozen=True)
class GatewayCall:
    decision: ModelIrrigationDecision
    raw_output: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class GatewayFailure(RuntimeError):
    def __init__(self, message: str, *, raw_output: str | None = None, latency_ms: int | None = None):
        super().__init__(message)
        self.raw_output = raw_output
        self.latency_ms = latency_ms


class DecisionGateway(Protocol):
    def decide(self, context: DecisionContext) -> GatewayCall: ...


class VolcengineGatewayClient:
    def __init__(self, settings: Settings, *, api_key: str | None = None, client: Any | None = None):
        self.settings = settings
        self.api_key = api_key or os.getenv("VEI_API_KEY")
        if client is None and self.api_key:
            from openai import OpenAI

            client = OpenAI(
                base_url=settings.gateway_base_url,
                api_key=self.api_key,
                timeout=settings.gateway_timeout_seconds,
                max_retries=0,
            )
        self.client = client

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.client)

    def decide(self, context: DecisionContext) -> GatewayCall:
        if not self.configured:
            raise GatewayFailure("VEI_API_KEY is not configured")
        started = time.monotonic()
        raw_output: str | None = None
        try:
            completion = self.client.chat.completions.create(
                model=self.settings.gateway_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": context.model_dump_json()},
                ],
                max_tokens=800,
                stream=False,
            )
            raw_output = completion.choices[0].message.content
            if not isinstance(raw_output, str) or not raw_output.strip():
                raise ValueError("model returned empty content")
            decision = ModelIrrigationDecision.model_validate_json(raw_output)
            usage = getattr(completion, "usage", None)
            return GatewayCall(
                decision=decision,
                raw_output=raw_output,
                latency_ms=round((time.monotonic() - started) * 1000),
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
            )
        except GatewayFailure:
            raise
        except Exception as exc:
            raise GatewayFailure(
                f"gateway decision failed: {type(exc).__name__}",
                raw_output=raw_output,
                latency_ms=round((time.monotonic() - started) * 1000),
            ) from exc


class SimulatedActuator:
    def __init__(self, now: Callable[[], datetime] | None = None):
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._started_at: datetime | None = None
        self._ends_at: datetime | None = None

    def _refresh(self, now: datetime | None = None) -> None:
        now = now or self._now()
        if self._ends_at is not None and now >= self._ends_at:
            self._started_at = None
            self._ends_at = None

    @property
    def is_watering(self) -> bool:
        self._refresh()
        return self._ends_at is not None

    def state(self) -> dict[str, Any]:
        self._refresh()
        return {
            "mode": "simulated",
            "state": "WATERING" if self._ends_at else "OFF",
            "startedAt": self._started_at.isoformat() if self._started_at else None,
            "scheduledStopAt": self._ends_at.isoformat() if self._ends_at else None,
        }

    def apply(self, action: IrrigationAction, duration_seconds: int | None, now: datetime) -> None:
        self._refresh(now)
        if action == IrrigationAction.START_WATERING:
            if duration_seconds is None:
                raise ValueError("watering duration is required")
            self._started_at = now
            self._ends_at = now + timedelta(seconds=duration_seconds)
        elif action == IrrigationAction.STOP_WATERING:
            self._started_at = None
            self._ends_at = None


def _number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def build_decision_context(
    frame: pd.DataFrame,
    forecast: ForecastResponse,
    actuator: SimulatedActuator,
    settings: Settings,
    *,
    request_id: str | None = None,
    now: datetime | None = None,
) -> DecisionContext:
    if frame.empty or forecast.status != "ok":
        raise ValueError("a complete forecast and at least one snapshot are required")
    now = now or datetime.now(timezone.utc)
    request_id = request_id or str(uuid.uuid4())
    latest = frame.iloc[-1]
    cutoff = frame.index[-1] - timedelta(hours=1)
    hour = frame.loc[frame.index >= cutoff]
    soil_now = _number(latest.get("soil_moisture_percent"))
    soil_then = _number(hour.iloc[0].get("soil_moisture_percent")) if not hour.empty else None
    health = {
        "air": bool(latest.get("air_ok", pd.notna(latest.get("air_temp_c")))),
        "soil": bool(latest.get("soil_ok", pd.notna(latest.get("soil_moisture_percent")))),
        "wind": bool(latest.get("wind_ok", pd.notna(latest.get("wind_ms")))),
        "solar": pd.notna(latest.get("solar_wm2")),
        "pressure": bool((_number(latest.get("pressure_kpa")) or 0) > 0),
    }
    current = {
        "timestamp": frame.index[-1].isoformat(),
        "airTemperatureC": _number(latest.get("air_temp_c")),
        "airHumidityPercent": _number(latest.get("rh_percent")),
        "soilTemperatureC": _number(latest.get("soil_temp_c")),
        "soilMoisturePercent": soil_now,
        "windSpeedMs": _number(latest.get("wind_ms")),
        "solarRadiationWm2": _number(latest.get("solar_wm2")),
        "airPressureKpa": _number(latest.get("pressure_kpa")),
        "sensorHealth": health,
        "allSensorsValid": all(health.values()),
    }
    trends = {
        "windowMinutes": 60,
        "soilMoistureDeltaPercent": None if soil_now is None or soil_then is None else soil_now - soil_then,
        "airTemperatureMeanC": _number(hour.get("air_temp_c", pd.Series(dtype=float)).mean()),
        "airHumidityMeanPercent": _number(hour.get("rh_percent", pd.Series(dtype=float)).mean()),
    }
    forecast_data = {
        "status": forecast.status,
        "horizonMinutes": settings.forecast_steps * settings.sample_minutes,
        "points": [point.model_dump(mode="json") for point in forecast.forecast],
    }
    constraints = {
        "allowedActions": [action.value for action in IrrigationAction],
        "maxDurationSeconds": settings.max_watering_seconds,
        "cooldownMinutes": settings.watering_cooldown_minutes,
        "maxDailyWateringSeconds": settings.max_daily_watering_seconds,
        "triggerMoisturePercent": settings.irrigation_trigger_percent,
        "targetMoisturePercent": settings.irrigation_target_percent,
    }
    return DecisionContext(
        requestId=request_id,
        generatedAt=now,
        current=current,
        trends=trends,
        forecast=forecast_data,
        actuator=actuator.state(),
        constraints=constraints,
    )


class DecisionEngine:
    def __init__(
        self,
        settings: Settings,
        store: Any,
        *,
        gateway: DecisionGateway | None = None,
        actuator: SimulatedActuator | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.gateway = gateway or VolcengineGatewayClient(settings)
        self.actuator = actuator or SimulatedActuator(self._now)

    @property
    def gateway_configured(self) -> bool:
        return bool(getattr(self.gateway, "configured", True))

    def should_evaluate(
        self,
        *,
        now: datetime,
        previous_moisture: float | None,
        current_moisture: float | None,
        health_changed: bool,
    ) -> str | None:
        latest = self.store.latest_decision()
        if latest is None:
            return "first_complete_forecast"
        if health_changed:
            return "sensor_health_changed"
        if previous_moisture is not None and current_moisture is not None:
            thresholds = (self.settings.irrigation_trigger_percent, self.settings.irrigation_target_percent)
            if any((previous_moisture < value <= current_moisture) or (previous_moisture > value >= current_moisture) for value in thresholds):
                return "moisture_threshold_crossed"
        elapsed = now - latest.evaluatedAt
        if elapsed >= timedelta(minutes=self.settings.decision_interval_minutes):
            return "periodic"
        return None

    def _persist(self, result: DecisionResult, context: DecisionContext, raw_output: str | None) -> DecisionResult:
        self.store.save_decision(result, context.model_dump(mode="json"), raw_output)
        return result

    def _safe_result(
        self,
        context: DecisionContext,
        trigger: str,
        *,
        status: str,
        final_action: IrrigationAction,
        reason_code: str,
        reason: str,
        safety_reasons: list[str],
        execute: bool,
        latency_ms: int | None = None,
        raw_output: str | None = None,
    ) -> DecisionResult:
        now = self._now()
        executed = False
        if execute and final_action == IrrigationAction.STOP_WATERING and self.actuator.is_watering:
            self.actuator.apply(final_action, None, now)
            self.store.record_actuator_event(now, context.requestId, final_action, None, self.settings.actuator_mode)
            executed = True
        result = DecisionResult(
            requestId=context.requestId,
            evaluatedAt=now,
            trigger=trigger,
            status=status,
            finalAction=final_action,
            reasonCode=reason_code,
            reason=reason,
            safetyReasons=safety_reasons,
            executed=executed,
            actuatorMode=self.settings.actuator_mode,
            latencyMs=latency_ms,
        )
        return self._persist(result, context, raw_output)

    def evaluate(self, context: DecisionContext, *, trigger: str, execute: bool) -> DecisionResult:
        if self.store.decision_exists(context.requestId):
            existing = self.store.get_decision(context.requestId)
            if existing is None:
                raise RuntimeError("decision id exists but cannot be loaded")
            return existing

        sensor_valid = bool(context.current.get("allSensorsValid"))
        soil_moisture = context.current.get("soilMoisturePercent")
        if not sensor_valid:
            action = IrrigationAction.STOP_WATERING if self.actuator.is_watering else IrrigationAction.NO_OP
            return self._safe_result(
                context,
                trigger,
                status="safety_stop" if action == IrrigationAction.STOP_WATERING else "rejected",
                final_action=action,
                reason_code="SENSOR_UNRELIABLE",
                reason="传感器状态异常，禁止启动灌溉",
                safety_reasons=["one or more required sensors are invalid"],
                execute=execute,
            )
        if not self.settings.llm_enabled:
            return self._safe_result(
                context,
                trigger,
                status="disabled",
                final_action=IrrigationAction.NO_OP,
                reason_code="LLM_DISABLED",
                reason="大模型决策未启用",
                safety_reasons=["AIOT_LLM_ENABLED is false"],
                execute=False,
            )

        try:
            call = self.gateway.decide(context)
        except GatewayFailure as exc:
            action = IrrigationAction.STOP_WATERING if self.actuator.is_watering else IrrigationAction.NO_OP
            return self._safe_result(
                context,
                trigger,
                status="gateway_error",
                final_action=action,
                reason_code="GATEWAY_ERROR",
                reason="云端决策失败，已进入安全状态",
                safety_reasons=[str(exc)],
                execute=execute,
                latency_ms=exc.latency_ms,
                raw_output=exc.raw_output,
            )

        proposed = call.decision
        final_action = proposed.action
        duration = proposed.durationSeconds
        safety_reasons: list[str] = []
        if proposed.action == IrrigationAction.START_WATERING:
            if duration is None or duration > self.settings.max_watering_seconds:
                safety_reasons.append("watering duration exceeds the local limit")
            if soil_moisture is None or soil_moisture >= self.settings.irrigation_target_percent:
                safety_reasons.append("soil moisture is already at or above the target")
            last_start = self.store.last_watering_start()
            if last_start and self._now() - last_start < timedelta(minutes=self.settings.watering_cooldown_minutes):
                safety_reasons.append("watering cooldown is active")
            used = self.store.daily_watering_seconds(self._now(), self.settings.timezone)
            if duration is not None and used + duration > self.settings.max_daily_watering_seconds:
                safety_reasons.append("daily watering limit would be exceeded")
            if self.actuator.is_watering:
                safety_reasons.append("actuator is already watering")

        if safety_reasons:
            final_action = IrrigationAction.STOP_WATERING if self.actuator.is_watering else IrrigationAction.NO_OP
            duration = None

        executed = False
        status = "rejected" if safety_reasons else ("dry_run" if not execute else "no_action")
        now = self._now()
        if execute and not safety_reasons and proposed.action != IrrigationAction.NO_OP:
            self.actuator.apply(proposed.action, duration, now)
            self.store.record_actuator_event(now, context.requestId, proposed.action, duration, self.settings.actuator_mode)
            executed = True
            status = "executed"
        result = DecisionResult(
            requestId=context.requestId,
            evaluatedAt=now,
            trigger=trigger,
            status=status,
            proposedAction=proposed.action,
            finalAction=final_action,
            durationSeconds=duration,
            reasonCode=proposed.reasonCode,
            reason=proposed.reason,
            confidence=proposed.confidence,
            safetyReasons=safety_reasons,
            executed=executed,
            actuatorMode=self.settings.actuator_mode,
            latencyMs=call.latency_ms,
            promptTokens=call.prompt_tokens,
            completionTokens=call.completion_tokens,
        )
        return self._persist(result, context, call.raw_output)


class DemoGateway:
    configured = True

    def __init__(self, scenario: str):
        self.scenario = scenario

    def decide(self, context: DecisionContext) -> GatewayCall:
        if self.scenario == "dry":
            decision = ModelIrrigationDecision(
                schemaVersion="1.0",
                action=IrrigationAction.START_WATERING,
                durationSeconds=30,
                reasonCode="FORECAST_DRYING",
                reason="土壤湿度较低且预测继续下降",
                confidence=0.92,
            )
        else:
            decision = ModelIrrigationDecision(
                schemaVersion="1.0",
                action=IrrigationAction.NO_OP,
                durationSeconds=None,
                reasonCode="NO_ACTION_NEEDED",
                reason="当前湿度充足，无需浇水",
                confidence=0.95,
            )
        raw = decision.model_dump_json()
        return GatewayCall(decision=decision, raw_output=raw, latency_ms=1, prompt_tokens=100, completion_tokens=30)
