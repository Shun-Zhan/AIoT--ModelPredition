"""Deterministic local edge-risk assessment for the computer gateway.

The ESP32-S3 remains a real-time collector and safety controller.  This
module runs on the local computer, combining its latest sensor snapshot,
historical-model forecast and valve state into explainable rules.  It never
opens a valve and it never calls the cloud.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from math import ceil
from typing import Any

from .config import Settings


class SamplingMode(StrEnum):
    DEBUG = "DEBUG"
    IRRIGATION_MONITORING = "IRRIGATION_MONITORING"
    NORMAL_MONITORING = "NORMAL_MONITORING"
    NIGHT_ECO = "NIGHT_ECO"


SAMPLING_INTERVALS_MS: dict[SamplingMode, int] = {
    SamplingMode.DEBUG: 2_000,
    SamplingMode.IRRIGATION_MONITORING: 5_000,
    SamplingMode.NORMAL_MONITORING: 60_000,
    SamplingMode.NIGHT_ECO: 600_000,
}


@dataclass(frozen=True)
class EventAssessment:
    code: str
    severity: str
    message: str
    evidence: dict[str, Any]
    recommended_action: str


@dataclass(frozen=True)
class RiskAssessment:
    risk_level: str
    risk_score: int
    reasons: list[str]
    data_freshness: dict[str, Any]
    recommended_sampling_mode: SamplingMode
    recommended_read_interval_ms: int
    recommended_cloud_analysis: bool
    irrigation_candidate: dict[str, Any]
    events: list[EventAssessment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "riskLevel": self.risk_level,
            "riskScore": self.risk_score,
            "reasons": self.reasons,
            "dataFreshness": self.data_freshness,
            "recommendedSamplingMode": self.recommended_sampling_mode.value,
            "recommendedReadIntervalMs": self.recommended_read_interval_ms,
            "recommendedCloudAnalysis": self.recommended_cloud_analysis,
            "irrigationCandidate": self.irrigation_candidate,
        }


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _forecast_evidence(forecast: dict[str, Any], settings: Settings) -> dict[str, Any]:
    points = forecast.get("forecast") if isinstance(forecast, dict) else None
    status = forecast.get("status", "unavailable") if isinstance(forecast, dict) else "unavailable"
    required_points = max(1, ceil(60 / settings.sample_minutes))
    available_points = len(points) if isinstance(points, list) else 0
    base = {
        "available": available_points > 0,
        "ready": False,
        "status": status,
        "requiredPoints": required_points,
        "availablePoints": available_points,
        "coversOneHour": False,
        "forecastEt0Mm": None,
        "forecastEndSoilPercent": None,
        "forecastMinSoilPercent": None,
    }
    if status != "ok" or not isinstance(points, list) or len(points) < required_points:
        return base

    one_hour = points[:required_points]
    et0_values: list[float] = []
    soil_values: list[float] = []
    timestamps: list[datetime] = []
    for point in one_hour:
        if not isinstance(point, dict):
            return base
        et0 = _as_float(point.get("et0Mm"))
        soil = _as_float(point.get("soilMoisturePercent"))
        timestamp_text = point.get("timestamp")
        try:
            timestamp = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return base
        if et0 is None or et0 < 0 or soil is None or not 0 <= soil <= 100:
            return base
        et0_values.append(et0)
        soil_values.append(soil)
        timestamps.append(timestamp)

    intervals = [
        (later - earlier).total_seconds() / 60
        for earlier, later in zip(timestamps, timestamps[1:])
    ]
    ordered = all(
        interval > 0 and abs(interval - settings.sample_minutes) <= 1 / 60
        for interval in intervals
    )
    covered_minutes = (
        (timestamps[-1] - timestamps[0]).total_seconds() / 60 + settings.sample_minutes
        if ordered else 0.0
    )
    covers_one_hour = covered_minutes >= 60
    if not covers_one_hour:
        return {**base, "coversOneHour": False}
    return {
        **base,
        "available": True,
        "ready": True,
        "coversOneHour": True,
        "forecastEt0Mm": round(sum(et0_values), 3),
        "forecastEndSoilPercent": round(soil_values[-1], 2),
        "forecastMinSoilPercent": round(min(soil_values), 2),
    }


def assess_environment(
    current: dict[str, Any], forecast: dict[str, Any], settings: Settings,
    *, actuator: dict[str, Any] | None = None, now: datetime | None = None,
) -> RiskAssessment:
    """Produce event candidates and a safe sampling recommendation.

    Every threshold comes from :class:`Settings`; this intentionally avoids
    browser-side policy and makes test/deployment tuning transparent.
    """
    now = now or datetime.now(timezone.utc)
    actuator = actuator or {}
    events: list[EventAssessment] = []
    reasons: list[str] = []
    received_text = current.get("receivedAt")
    age_seconds: float | None = None
    if received_text:
        try:
            received = datetime.fromisoformat(str(received_text).replace("Z", "+00:00"))
            age_seconds = max(0.0, (now - received).total_seconds())
        except ValueError:
            pass
    fresh = age_seconds is not None and age_seconds <= settings.data_stale_seconds
    freshness = {
        "fresh": fresh,
        "ageSeconds": round(age_seconds, 1) if age_seconds is not None else None,
        "staleAfterSeconds": settings.data_stale_seconds,
        "receivedAt": received_text,
    }
    failed = [name for name, ok in (
        ("air", current.get("airOk")), ("soil", current.get("soilOk")),
        ("wind", current.get("windOk")), ("solar", current.get("solarOk")),
    ) if not ok]
    pressure = _as_float(current.get("airPressureHpa"))
    if pressure is None or pressure <= 0:
        failed.append("pressure")
    if failed:
        events.append(EventAssessment("SENSOR_FAILURE", "high", "传感器读取失败或数值无效",
                                      {"failed": failed}, "检查接线和传感器，保持人工审核，禁止自动灌溉"))
        reasons.append("存在无效传感器：" + "、".join(failed))
    if not fresh:
        events.append(EventAssessment("DATA_INTERRUPTION", "high", "ESP32 实时数据中断或陈旧",
                                      freshness, "检查 USB 串口链路；保持阀门安全关闭"))
        reasons.append("实时数据不新鲜")

    air = current.get("air") if isinstance(current.get("air"), dict) else {}
    soil = current.get("soil") if isinstance(current.get("soil"), dict) else {}
    temperature = _as_float(air.get("temperatureC"))
    moisture = _as_float(soil.get("moisturePercent"))
    wind = _as_float(current.get("windSpeedMs"))
    solar = _as_float(current.get("solarRadiationWm2"))
    forecast_info = _forecast_evidence(forecast, settings)
    forecast_ready = bool(forecast_info["ready"])
    forecast_et0 = _as_float(forecast_info["forecastEt0Mm"])
    forecast_end_soil = _as_float(forecast_info["forecastEndSoilPercent"])
    forecast_min_soil = _as_float(forecast_info["forecastMinSoilPercent"])
    forecast_declining = bool(
        forecast_ready and moisture is not None and forecast_end_soil is not None
        and forecast_end_soil < moisture
    )
    forecast_crosses_trigger = bool(
        forecast_ready and forecast_min_soil is not None
        and forecast_min_soil < settings.irrigation_trigger_percent
    )
    high_et = bool(
        forecast_ready and forecast_et0 is not None
        and forecast_et0 >= settings.irrigation_high_et0_1h_mm
    )

    candidate_rule: str | None = None
    if moisture is not None and fresh and not failed:
        if moisture < settings.irrigation_severe_dry_percent:
            candidate_rule = "SEVERE_DRY"
        elif moisture < settings.irrigation_trigger_percent:
            if forecast_ready and (forecast_declining or high_et):
                candidate_rule = "DECLINING_OR_HIGH_ET0"
        elif moisture <= settings.irrigation_predictive_max_percent:
            if forecast_ready and forecast_crosses_trigger and high_et:
                candidate_rule = "PREDICTED_CROSSING_AND_HIGH_ET0"
    irrigation_candidate = {
        "eligible": candidate_rule is not None,
        "rule": candidate_rule,
        "moisturePercent": moisture,
        "thresholds": {
            "severeDryPercent": settings.irrigation_severe_dry_percent,
            "triggerPercent": settings.irrigation_trigger_percent,
            "predictiveMaxPercent": settings.irrigation_predictive_max_percent,
            "highEt0OneHourMm": settings.irrigation_high_et0_1h_mm,
        },
        "forecastReady": forecast_ready,
        "forecastDeclining": forecast_declining,
        "forecastCrossesTrigger": forecast_crosses_trigger,
        "forecast": forecast_info,
    }

    if candidate_rule is not None:
        message = {
            "SEVERE_DRY": "土壤严重干燥，形成灌溉候选",
            "DECLINING_OR_HIGH_ET0": "土壤偏干且预测继续下降或未来一小时 ET₀ 较高",
            "PREDICTED_CROSSING_AND_HIGH_ET0": "预测一小时内跌破触发线且未来一小时 ET₀ 较高",
        }[candidate_rule]
        events.append(EventAssessment(
            "SOIL_ABNORMALLY_DRY", "high", message, irrigation_candidate,
            "形成灌溉候选，等待本地安全审核和人工长按确认",
        ))
        reasons.append(message)

    if high_et:
        evidence = {
            "temperatureC": temperature, "solarWm2": solar, "windMs": wind,
            "soilMoisturePercent": moisture,
            "highEt0OneHourMm": settings.irrigation_high_et0_1h_mm,
            "forecast": forecast_info,
        }
        events.append(EventAssessment("HIGH_EVAPOTRANSPIRATION_RISK", "high", "未来一小时累计 ET₀ 较高",
                                      evidence, "提高采样频率；可请求一次低频云端分析；不得自动开阀"))
        reasons.append("未来一小时累计 ET₀ 达到高蒸散阈值")

    valve_open = actuator.get("state") == "OPEN"
    night_stable = bool(
        fresh and not failed and candidate_rule is None and not high_et and not valve_open
        and solar is not None and solar <= settings.night_solar_wm2
        and wind is not None and wind <= settings.night_wind_ms
    )
    if night_stable:
        events.append(EventAssessment("NIGHT_STABLE", "info", "低光照、低风速且无灌溉候选，夜间环境稳定",
                                      {"solarWm2": solar, "windMs": wind, "soilMoisturePercent": moisture},
                                      "可切换夜间节能采样；水阀打开时自动取消该模式"))

    if failed or not fresh:
        level, score, mode = "ATTENTION", 70, SamplingMode.DEBUG
    elif candidate_rule is not None:
        level, score, mode = "IRRIGATION_CANDIDATE", 88 if high_et else 76, SamplingMode.IRRIGATION_MONITORING
    elif high_et:
        level, score, mode = "HIGH_EVAPOTRANSPIRATION", 72, SamplingMode.IRRIGATION_MONITORING
    elif night_stable:
        level, score, mode = "NORMAL", 10, SamplingMode.NIGHT_ECO
        reasons.append("夜间稳定，可降低非关键采样频率")
    elif (
        moisture is not None
        and moisture <= settings.irrigation_predictive_max_percent
    ) or forecast_crosses_trigger:
        level, score, mode = "ATTENTION", 45, SamplingMode.NORMAL_MONITORING
        reasons.append("土壤偏干，但预测条件尚不足以形成灌溉候选")
    else:
        level, score, mode = "NORMAL", 20, SamplingMode.NORMAL_MONITORING
        reasons.append("当前多传感器状态稳定")

    # This is a hard policy boundary independent of the normal recommendation.
    if valve_open:
        mode = SamplingMode.IRRIGATION_MONITORING
        reasons.append("水阀开启中，强制快速采样且禁止夜间节能模式")

    return RiskAssessment(
        risk_level=level, risk_score=score, reasons=reasons,
        data_freshness=freshness, recommended_sampling_mode=mode,
        recommended_read_interval_ms=SAMPLING_INTERVALS_MS[mode],
        recommended_cloud_analysis=level in {"HIGH_EVAPOTRANSPIRATION", "IRRIGATION_CANDIDATE"},
        irrigation_candidate=irrigation_candidate,
        events=events,
    )
