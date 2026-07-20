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
        }


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _forecast_evidence(forecast: dict[str, Any]) -> dict[str, Any]:
    points = forecast.get("forecast") if isinstance(forecast, dict) else None
    if not isinstance(points, list) or not points:
        return {"available": False, "status": forecast.get("status", "unavailable") if isinstance(forecast, dict) else "unavailable"}
    et0 = sum(_as_float(point.get("et0Mm")) or 0.0 for point in points if isinstance(point, dict))
    soil_values = [_as_float(point.get("soilMoisturePercent")) for point in points if isinstance(point, dict)]
    soil_values = [value for value in soil_values if value is not None]
    return {
        "available": True,
        "status": forecast.get("status", "ok"),
        "forecastEt0Mm": round(et0, 3),
        "forecastMinSoilPercent": round(min(soil_values), 2) if soil_values else None,
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
    forecast_info = _forecast_evidence(forecast)
    forecast_dry = bool(
        forecast_info.get("available")
        and forecast_info.get("forecastMinSoilPercent") is not None
        and float(forecast_info["forecastMinSoilPercent"]) < settings.irrigation_trigger_percent
    )
    soil_dry = moisture is not None and moisture < settings.irrigation_trigger_percent
    soil_low = moisture is not None and moisture < settings.high_et_soil_percent

    if current.get("soilOk") and soil_dry:
        events.append(EventAssessment("SOIL_ABNORMALLY_DRY", "high", "土壤湿度低于灌溉触发阈值",
                                      {"moisturePercent": moisture, "triggerPercent": settings.irrigation_trigger_percent},
                                      "形成灌溉候选，等待本地安全审核和人工长按确认"))
        reasons.append(f"土壤湿度 {moisture:.1f}% 低于触发阈值")

    high_et_conditions = {
        "soilLow": soil_low,
        "highTemperature": temperature is not None and temperature >= settings.high_et_temp_c,
        "strongSolar": solar is not None and solar >= settings.high_et_solar_wm2,
        "highWind": wind is not None and wind >= settings.high_et_wind_ms,
        "forecastDry": forecast_dry,
    }
    # Soil + the three weather signals are mandatory.  A dry forecast adds
    # confidence, but forecast warming-up must never hide a clear risk.
    high_et = all(high_et_conditions[key] for key in ("soilLow", "highTemperature", "strongSolar", "highWind"))
    if high_et:
        evidence = {
            "temperatureC": temperature, "solarWm2": solar, "windMs": wind,
            "soilMoisturePercent": moisture, "thresholds": {
                "temperatureC": settings.high_et_temp_c, "solarWm2": settings.high_et_solar_wm2,
                "windMs": settings.high_et_wind_ms, "soilPercent": settings.high_et_soil_percent,
            }, "forecast": forecast_info,
        }
        events.append(EventAssessment("HIGH_EVAPOTRANSPIRATION_RISK", "high", "高温、强光和高风速叠加，蒸散风险高",
                                      evidence, "提高采样频率；可请求一次低频云端分析；不得自动开阀"))
        reasons.append("高蒸散组合风险（温度、光照、风速、土壤、预测）")

    valve_open = actuator.get("state") == "OPEN"
    night_stable = bool(
        fresh and not failed and not soil_dry and not high_et and not valve_open
        and solar is not None and solar <= settings.night_solar_wm2
        and wind is not None and wind <= settings.night_wind_ms
    )
    if night_stable:
        events.append(EventAssessment("NIGHT_STABLE", "info", "低光照、低风速且无灌溉候选，夜间环境稳定",
                                      {"solarWm2": solar, "windMs": wind, "soilMoisturePercent": moisture},
                                      "可切换夜间节能采样；水阀打开时自动取消该模式"))

    if failed or not fresh:
        level, score, mode = "ATTENTION", 70, SamplingMode.DEBUG
    elif soil_dry:
        level, score, mode = "IRRIGATION_CANDIDATE", 88 if high_et else 76, SamplingMode.IRRIGATION_MONITORING
    elif high_et:
        level, score, mode = "HIGH_EVAPOTRANSPIRATION", 72, SamplingMode.IRRIGATION_MONITORING
    elif night_stable:
        level, score, mode = "NORMAL", 10, SamplingMode.NIGHT_ECO
        reasons.append("夜间稳定，可降低非关键采样频率")
    elif soil_low or forecast_dry:
        level, score, mode = "ATTENTION", 45, SamplingMode.NORMAL_MONITORING
        reasons.append("土壤或本地预测提示需要关注")
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
        events=events,
    )
