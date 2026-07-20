"""Local context construction and safety review for cloud suggestions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .cloud import CloudCall, CloudFailure, OpenAICompatibleGateway, new_request_id
from .config import Settings
from .edge import RiskAssessment, assess_environment
from .schemas import DecisionContext, DecisionResult, IrrigationAction, IrrigationDecision
from .storage import Store


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "item"):
        return value.item()
    return str(value)


class IrrigationService:
    def __init__(self, store: Store, settings: Settings, gateway: OpenAICompatibleGateway | None = None):
        self.store = store
        self.settings = settings
        self.gateway = gateway or OpenAICompatibleGateway(settings)

    @property
    def last_device_state(self) -> dict[str, Any]:
        """Read persisted ACK state so API workers and the serial process agree."""
        return self.store.latest_actuator_state(self.settings.actuator_mode)

    def assess_edge(self, current: dict[str, Any] | None = None) -> RiskAssessment:
        """Evaluate local risk and persist its event candidates without cloud I/O."""
        current = current or self.store.latest_live_snapshot() or self.store.latest_snapshot() or {}
        forecast = self.store.latest_forecast()
        assessment = assess_environment(
            current, forecast.model_dump(mode="json") if forecast else {"status": "warming_up", "forecast": []},
            self.settings, actuator=self.last_device_state,
        )
        active_codes = {event.code for event in assessment.events}
        for event in assessment.events:
            self.store.record_environment_event(
                event.code, event.severity, event.message, event.evidence, event.recommended_action,
                cooldown_seconds=self.settings.event_cooldown_seconds,
            )
            # Preserve the previously documented anomaly API/table for existing
            # users while new consumers get richer /v1/events rows.
            self.store.record_anomaly(event.code, event.severity, event.message, event.evidence,
                                      cooldown_seconds=self.settings.event_cooldown_seconds)
        self.store.resolve_environment_event_codes_not_active(
            {"SENSOR_FAILURE", "DATA_INTERRUPTION", "SOIL_ABNORMALLY_DRY", "HIGH_EVAPOTRANSPIRATION_RISK", "NIGHT_STABLE"},
            active_codes,
        )
        return assessment

    def detect_anomalies(self, current: dict[str, Any]) -> list[dict[str, Any]]:
        assessment = self.assess_edge(current)
        return [{"code": event.code, "severity": event.severity, "message": event.message,
                 "details": event.evidence} for event in assessment.events]

    def record_data_interruption_if_needed(self) -> None:
        current = self.store.latest_live_snapshot()
        if not current:
            self.store.record_environment_event("DATA_INTERRUPTION", "high", "尚未收到 ESP32 实时数据", {},
                                                "检查 USB 串口链路；保持阀门安全关闭",
                                                cooldown_seconds=self.settings.event_cooldown_seconds)
            return
        self.assess_edge(current)

    def record_valve_execution_failures(self) -> None:
        """Persist missing ACKs; receiver records reject/state failures immediately."""
        for missing in self.store.commands_without_ack(self.settings.actuator_ack_timeout_seconds):
            self.store.record_environment_event(
                "VALVE_EXECUTION_FAILURE", "high", "水阀命令超时未收到 ESP32 ACK",
                {"requestId": missing["requestId"], "sentAt": missing["sentAt"], "command": missing["command"]},
                "检查 USB 串口与继电器；在确认状态前不要重复开阀",
                cooldown_seconds=self.settings.event_cooldown_seconds,
            )

    def current_context(self, request_id: str | None = None) -> DecisionContext:
        current = self.store.latest_live_snapshot() or self.store.latest_snapshot() or {}
        frame = self.store.recent_frame(limit=720)
        trends: dict[str, Any] = {"samples": int(len(frame)), "windows": {}}
        if not frame.empty:
            end = frame.index.max()
            for label, hours in (("last1Hour", 1), ("last24Hours", 24)):
                window = frame.loc[frame.index >= end - pd.Timedelta(hours=hours)]
                summary: dict[str, Any] = {"samples": int(len(window))}
                for column in ("air_temp_c", "rh_percent", "soil_temp_c", "soil_moisture_percent", "wind_ms", "solar_wm2", "pressure_kpa"):
                    if column in window:
                        series = pd.to_numeric(window[column], errors="coerce").dropna()
                        if not series.empty:
                            summary[column] = {"latest": _json_value(series.iloc[-1]), "mean": round(float(series.mean()), 3), "min": round(float(series.min()), 3), "max": round(float(series.max()), 3)}
                trends["windows"][label] = summary
            trends["dataStart"] = frame.index.min().isoformat()
            trends["dataEnd"] = frame.index.max().isoformat()
        forecast = self.store.latest_forecast()
        forecast_data = forecast.model_dump(mode="json") if forecast else {"status": "warming_up", "forecast": []}
        received_at = current.get("receivedAt")
        fresh = False
        if received_at:
            received = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            fresh = datetime.now(timezone.utc) - received <= timedelta(seconds=self.settings.data_stale_seconds)
        all_valid = bool(current and fresh and current.get("airOk") and current.get("soilOk") and
                         current.get("windOk") and current.get("solarOk") and
                         current.get("airPressureHpa", 0) > 0)
        current = dict(current)
        current["allSensorsValid"] = all_valid
        current["fresh"] = fresh
        edge = self.assess_edge(current)
        anomalies = [{"code": event.code, "severity": event.severity, "message": event.message,
                      "details": event.evidence} for event in edge.events]
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        return DecisionContext(
            schemaVersion="1.0",
            requestId=request_id or new_request_id("analysis"),
            generatedAt=datetime.now(timezone.utc),
            current=current,
            trends=trends,
            forecast=forecast_data,
            actuator=self.last_device_state,
            constraints={
                "maxWateringSeconds": self.settings.max_watering_seconds,
                "triggerPercent": self.settings.irrigation_trigger_percent,
                "targetPercent": self.settings.irrigation_target_percent,
                "cloudNeverDirectlyControlsGPIO": True,
                "activeAnomalies": anomalies,
                "recentAnomalies": self.store.anomaly_rows(limit=20),
                "wateringLast7Days": self.store.actuator_summary(seven_days_ago),
                "recentReviewedDecisions": self.store.recent_decisions(limit=20),
                "edgeRisk": edge.to_dict(),
            },
        )

    def evaluate(self, decision: IrrigationDecision, context: DecisionContext, *, trigger: str,
                 call: CloudCall | None = None, raw_output: str | None = None) -> DecisionResult:
        existing = self.store.get_decision(decision.requestId)
        if existing:
            return existing
        now = datetime.now(timezone.utc)
        reasons: list[str] = []
        if decision.expiresAt <= now:
            reasons.append("decision has expired")
        if decision.expiresAt > now + timedelta(seconds=60):
            reasons.append("decision expiry exceeds local limit")
        if decision.requestId != context.requestId:
            reasons.append("model requestId does not match local requestId")
        if decision.action == IrrigationAction.START_WATERING:
            moisture = context.current.get("soil", {}).get("moisturePercent") if isinstance(context.current.get("soil"), dict) else None
            if not context.current.get("allSensorsValid"):
                reasons.append("required sensor data is incomplete")
            if moisture is None or float(moisture) >= self.settings.irrigation_target_percent:
                reasons.append("soil moisture is already at or above target")
            if decision.durationSeconds is None or decision.durationSeconds > self.settings.max_watering_seconds:
                reasons.append("duration exceeds local limit")
            if self.last_device_state.get("state") == "OPEN":
                reasons.append("valve is already open")
            since = now - timedelta(minutes=self.settings.watering_cooldown_minutes)
            if self.store.watering_totals(since) > 0:
                reasons.append("watering cooldown is active")
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self.store.watering_totals(day_start) + int(decision.durationSeconds or 0) > self.settings.max_daily_watering_seconds:
                reasons.append("daily watering limit would be exceeded")
        if decision.confidence < 0.5 and decision.action == IrrigationAction.START_WATERING:
            reasons.append("model confidence is below local threshold")
        accepted = not reasons
        status = "awaiting_confirmation" if accepted and decision.action != IrrigationAction.NO_OP else ("suggested" if accepted else "rejected")
        result = DecisionResult(
            requestId=decision.requestId, evaluatedAt=now, trigger=trigger, status=status,
            proposedAction=decision.action, finalAction=decision.action if accepted else IrrigationAction.NO_OP,
            durationSeconds=decision.durationSeconds if accepted else None,
            reasonCode=decision.reasonCode, reason=decision.reason, confidence=decision.confidence,
            safetyReasons=reasons, latencyMs=call.latency_ms if call else None,
            promptTokens=call.prompt_tokens if call else None,
            completionTokens=call.completion_tokens if call else None,
            expiresAt=decision.expiresAt,
        )
        self.store.save_decision(result, context.model_dump(mode="json"), raw_output)
        return result

    def analyze(self, *, trigger: str = "manual") -> DecisionResult:
        context = self.current_context()
        if not self.settings.llm_enabled:
            result = DecisionResult(
                requestId=context.requestId, evaluatedAt=datetime.now(timezone.utc), trigger=trigger,
                status="disabled", finalAction=IrrigationAction.NO_OP, reasonCode="LLM_DISABLED",
                reason="云端大模型未启用；本地采集、预测和安全规则仍正常运行。",
                safetyReasons=["AIOT_LLM_ENABLED is false"],
            )
            self.store.save_decision(result, context.model_dump(mode="json"), None)
            return result
        try:
            decision, call = self.gateway.irrigation_decision(context)
        except CloudFailure as exc:
            result = DecisionResult(
                requestId=context.requestId, evaluatedAt=datetime.now(timezone.utc), trigger=trigger,
                status="gateway_error", finalAction=IrrigationAction.NO_OP, reasonCode="GATEWAY_ERROR",
                reason="云端调用失败，继续使用本地离线主干。", safetyReasons=[str(exc)],
            )
            self.store.save_llm_call(context.requestId, "irrigation", context.model_dump(mode="json"), error=str(exc))
            self.store.save_decision(result, context.model_dump(mode="json"), None)
            return result
        self.store.save_llm_call(context.requestId, "irrigation", context.model_dump(mode="json"),
                                 response=decision.model_dump(mode="json"), latency_ms=call.latency_ms,
                                 prompt_tokens=call.prompt_tokens, completion_tokens=call.completion_tokens)
        return self.evaluate(decision, context, trigger=trigger, call=call, raw_output=call.content)

    def confirm(self, request_id: str) -> DecisionResult:
        result = self.store.get_decision(request_id)
        if result is None:
            raise KeyError(request_id)
        if result.status != "awaiting_confirmation":
            return result
        current = self.current_context(request_id)
        reject_reasons: list[str] = []
        if result.expiresAt is None or result.expiresAt <= datetime.now(timezone.utc):
            reject_reasons.append("suggestion expired before human confirmation")
        if result.finalAction == IrrigationAction.START_WATERING:
            if not current.current.get("allSensorsValid"):
                reject_reasons.append("current sensor data is incomplete or stale")
            soil = current.current.get("soil") or {}
            moisture = soil.get("moisturePercent") if isinstance(soil, dict) else None
            if moisture is None or float(moisture) >= self.settings.irrigation_target_percent:
                reject_reasons.append("current soil moisture is at or above target")
            if self.last_device_state.get("state") == "OPEN":
                reject_reasons.append("valve is already open")
            since = datetime.now(timezone.utc) - timedelta(minutes=self.settings.watering_cooldown_minutes)
            if self.store.watering_totals(since) > 0:
                reject_reasons.append("watering cooldown is active")
            day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            if (self.store.watering_totals(day_start) + int(result.durationSeconds or 0)
                    > self.settings.max_daily_watering_seconds):
                reject_reasons.append("daily watering limit would be exceeded")
        if reject_reasons:
            rejected = result.model_copy(update={
                "status": "rejected_on_confirmation", "finalAction": IrrigationAction.NO_OP,
                "durationSeconds": None, "safetyReasons": result.safetyReasons + reject_reasons,
            })
            self.store.save_decision(rejected, current.model_dump(mode="json"), result.reason)
            return rejected
        expires = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        command = {
            "schemaVersion": "1.0", "requestId": result.requestId,
            "action": result.finalAction.value, "durationSeconds": result.durationSeconds,
            "reasonCode": result.reasonCode, "reason": "human-confirmed: " + result.reason,
            "confidence": result.confidence or 0.0, "expiresAt": expires,
            "ttlSeconds": 30,
        }
        queued = self.store.enqueue_command(command)
        if not queued:
            return result
        updated = result.model_copy(update={"status": "confirmed_waiting_device", "humanConfirmed": True})
        self.store.save_decision(updated, {"confirmed": True, "command": command}, result.reason)
        return updated

    def chat(self, question: str) -> dict[str, Any]:
        context = self.current_context("chat-" + new_request_id())
        evidence = {
            "current": context.current, "trends": context.trends, "forecast": context.forecast,
            "wateringLast7Days": self.store.actuator_summary(datetime.now(timezone.utc) - timedelta(days=7)),
            "anomalies": self.store.anomaly_rows(limit=20),
        }
        data_range = {"start": context.trends.get("dataStart"), "end": context.trends.get("dataEnd"), "samples": context.trends.get("samples", 0)}
        if not self.settings.llm_enabled:
            answer = "本地离线模式：已读取当前传感器、历史趋势和预测状态；启用 AIOT_LLM_ENABLED 后可让云端大模型生成自然语言分析。"
            return {"answer": answer, "dataRange": data_range, "evidence": ["当前数据与 SQLite 历史趋势已纳入上下文"], "llmUsed": False}
        try:
            call = self.gateway.chat(question, evidence)
            self.store.save_llm_call(context.requestId, "chat", evidence, response={"answer": call.content},
                                     latency_ms=call.latency_ms, prompt_tokens=call.prompt_tokens, completion_tokens=call.completion_tokens)
            return {"answer": call.content, "dataRange": data_range, "evidence": ["current", "trends", "forecast"], "llmUsed": True}
        except CloudFailure as exc:
            self.store.save_llm_call(context.requestId, "chat", evidence, error=str(exc))
            return {"answer": "云端暂不可用；本地数据仍正常保存，暂不能生成云端自然语言分析。", "dataRange": data_range, "evidence": [str(exc)], "llmUsed": False}
