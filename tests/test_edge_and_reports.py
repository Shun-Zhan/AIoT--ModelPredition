from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from dual_forecast.config import SETTINGS
from dual_forecast.edge import SamplingMode, assess_environment
from dual_forecast.esp32_receiver import _handle_config_ack_line, _send_pending_configs
from dual_forecast.irrigation import IrrigationService
from dual_forecast.schemas import IrrigationAction
from dual_forecast.service import create_app
from dual_forecast.storage import Store


def live(*, soil=40.0, temp=32.0, wind=3.0, solar=700.0, air_ok=True, received_at=None):
    return {
        "receivedAt": (received_at or datetime.now(timezone.utc)).isoformat(),
        "airOk": air_ok, "soilOk": True, "windOk": True, "solarOk": True,
        "airPressureHpa": 1005, "windSpeedMs": wind, "solarRadiationWm2": solar,
        "air": {"temperatureC": temp, "humidityPercent": 45},
        "soil": {"temperatureC": 22, "moisturePercent": soil},
    }


def forecast(*, end_soil=40.0, min_soil=None, et0=0.30, status="ok", points=12):
    now = datetime.now(timezone.utc)
    values = [end_soil + 0.1 * (points - index - 1) for index in range(points)]
    if min_soil is not None and values:
        values[len(values) // 2] = min_soil
    return {
        "status": status,
        "forecast": [
            {
                "timestamp": (now + timedelta(minutes=5 * (index + 1))).isoformat(),
                "et0Mm": et0 / points,
                "soilMoisturePercent": soil,
            }
            for index, soil in enumerate(values)
        ],
    }


def test_high_evapotranspiration_requires_complete_high_et0_forecast():
    assessment = assess_environment(live(soil=50), forecast(end_soil=50, et0=0.30), SETTINGS)
    assert assessment.risk_level == "HIGH_EVAPOTRANSPIRATION"
    assert assessment.recommended_sampling_mode == SamplingMode.IRRIGATION_MONITORING
    event = next(event for event in assessment.events if event.code == "HIGH_EVAPOTRANSPIRATION_RISK")
    assert event.evidence["forecast"]["ready"]
    assert event.evidence["forecast"]["forecastEt0Mm"] == 0.3


def test_dry_soil_and_open_valve_never_recommends_night_mode():
    assessment = assess_environment(live(soil=19.9, temp=20, wind=0, solar=0), {"status": "warming_up"}, SETTINGS,
                                    actuator={"state": "OPEN"})
    assert assessment.risk_level == "IRRIGATION_CANDIDATE"
    assert assessment.irrigation_candidate["rule"] == "SEVERE_DRY"
    assert assessment.recommended_sampling_mode == SamplingMode.IRRIGATION_MONITORING
    assert assessment.recommended_read_interval_ms <= 5000


@pytest.mark.parametrize(
    ("moisture", "prediction", "candidate", "rule"),
    [
        (19.9, {"status": "warming_up"}, True, "SEVERE_DRY"),
        (20.0, forecast(end_soil=20.0, et0=0.29), False, None),
        (29.9, forecast(end_soil=29.8, et0=0.0), True, "DECLINING_OR_HIGH_ET0"),
        (30.0, forecast(end_soil=30.1, min_soil=29.9, et0=0.30), True, "PREDICTED_CROSSING_AND_HIGH_ET0"),
        (45.0, forecast(end_soil=45.1, min_soil=29.9, et0=0.30), True, "PREDICTED_CROSSING_AND_HIGH_ET0"),
        (45.1, forecast(end_soil=29.0, min_soil=29.0, et0=0.50), False, None),
    ],
)
def test_predictive_irrigation_candidate_boundaries(moisture, prediction, candidate, rule):
    assessment = assess_environment(live(soil=moisture), prediction, SETTINGS)
    assert (assessment.risk_level == "IRRIGATION_CANDIDATE") is candidate
    assert assessment.irrigation_candidate["eligible"] is candidate
    assert assessment.irrigation_candidate["rule"] == rule


def test_mid_band_accepts_decline_or_high_et0_but_requires_ready_hour():
    irregular = forecast(end_soil=24.0, et0=0.5)
    irregular["forecast"][-1]["timestamp"] = (
        datetime.now(timezone.utc) + timedelta(minutes=65)
    ).isoformat()
    declining = assess_environment(live(soil=25), forecast(end_soil=24.9, et0=0), SETTINGS)
    high_et0 = assess_environment(live(soil=25), forecast(end_soil=25.1, et0=0.30), SETTINGS)
    neither = assess_environment(live(soil=25), forecast(end_soil=25.1, et0=0.29), SETTINGS)
    incomplete = assess_environment(
        live(soil=25), forecast(end_soil=24.0, et0=0.5, points=11), SETTINGS,
    )
    wrong_cadence = assess_environment(live(soil=25), irregular, SETTINGS)

    assert declining.risk_level == "IRRIGATION_CANDIDATE"
    assert high_et0.risk_level == "IRRIGATION_CANDIDATE"
    assert neither.risk_level != "IRRIGATION_CANDIDATE"
    assert incomplete.risk_level != "IRRIGATION_CANDIDATE"
    assert wrong_cadence.risk_level != "IRRIGATION_CANDIDATE"
    assert not incomplete.irrigation_candidate["forecastReady"]


def test_upper_band_requires_both_threshold_crossing_and_high_et0():
    both = assess_environment(live(soil=40), forecast(end_soil=40, min_soil=29.9, et0=0.30), SETTINGS)
    crossing_only = assess_environment(live(soil=40), forecast(end_soil=40, min_soil=29.9, et0=0.29), SETTINGS)
    et0_only = assess_environment(live(soil=40), forecast(end_soil=40, min_soil=30.0, et0=0.30), SETTINGS)

    assert both.risk_level == "IRRIGATION_CANDIDATE"
    assert crossing_only.risk_level != "IRRIGATION_CANDIDATE"
    assert et0_only.risk_level != "IRRIGATION_CANDIDATE"
    assert both.irrigation_candidate["forecast"]["forecastMinSoilPercent"] == 29.9


def test_stale_or_failed_sensor_causes_attention_and_event():
    old = datetime.now(timezone.utc) - timedelta(seconds=SETTINGS.data_stale_seconds + 1)
    assessment = assess_environment(live(air_ok=False, received_at=old), {}, SETTINGS)
    assert assessment.risk_level == "ATTENTION"
    assert {event.code for event in assessment.events} >= {"SENSOR_FAILURE", "DATA_INTERRUPTION"}


def test_environment_event_cooldown_and_recovery(tmp_path):
    store = Store(tmp_path / "edge.sqlite")
    assert store.record_environment_event("SOIL_ABNORMALLY_DRY", "high", "dry", {}, "check", cooldown_seconds=300)
    assert not store.record_environment_event("SOIL_ABNORMALLY_DRY", "high", "dry", {}, "check", cooldown_seconds=300)
    store.resolve_environment_event_codes_not_active({"SOIL_ABNORMALLY_DRY"}, set())
    assert store.environment_event_rows()[0]["resolved"]


class FakeSerial:
    def __init__(self): self.data = b""
    def write(self, data): self.data += data


def test_config_queue_has_independent_protocol_and_ack(tmp_path):
    store = Store(tmp_path / "config.sqlite")
    config = store.enqueue_sampling_config("NORMAL_MONITORING", 60000)
    serial = FakeSerial()
    _send_pending_configs(serial, store)
    assert serial.data.startswith(b"@CONFIG {")
    assert _handle_config_ack_line(
        f'@CONFIG_ACK {{"requestId":"{config["requestId"]}","accepted":true,"samplingMode":"NORMAL_MONITORING","readIntervalMs":60000}}', store
    )
    assert store.sampling_config_status()["status"] == "acked"


def test_sent_valve_command_without_ack_becomes_auditable_failure(tmp_path):
    settings = replace(
        SETTINGS, database_path=tmp_path / "ack-timeout.sqlite", artifact_dir=tmp_path / "artifacts",
        actuator_ack_timeout_seconds=0,
    )
    store = Store(settings.database_path)
    assert store.enqueue_command({
        "schemaVersion": "1.0", "requestId": "request-no-ack", "action": "NO_OP",
        "durationSeconds": None, "reasonCode": "TEST", "reason": "timeout test", "confidence": 1,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), "ttlSeconds": 30,
    })
    assert store.claim_pending_commands()[0]["requestId"] == "request-no-ack"
    IrrigationService(store, settings).record_valve_execution_failures()
    event = next(event for event in store.environment_event_rows() if event["code"] == "VALVE_EXECUTION_FAILURE")
    assert event["evidence"]["requestId"] == "request-no-ack"


def test_water_report_flow_and_no_flow_degrade_gracefully(tmp_path):
    base = replace(SETTINGS, database_path=tmp_path / "report.sqlite", artifact_dir=tmp_path / "artifacts", valve_flow_lpm=None)
    app = create_app(base)
    store = Store(base.database_path)
    store.record_actuator_event("water-1", IrrigationAction.START_WATERING, 120, {})
    client = TestClient(app)
    no_flow = client.get("/v1/reports/water").json()
    assert no_flow["estimatedLiters"] is None
    assert "未配置" in no_flow["estimateNote"]
    with_flow = TestClient(create_app(replace(base, valve_flow_lpm=2.5))).get("/v1/reports/water").json()
    assert with_flow["estimatedLiters"] == 5.0
    daily = client.get("/v1/reports/daily").json()
    assert daily["historyStatus"] == "数据积累中"
    assert daily["water"]["baselineSavingsPercent"] is None


def test_dashboard_keeps_long_press_and_offline_mobile_data(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "ui.sqlite", artifact_dir=tmp_path / "artifacts", llm_enabled=False)
    client = TestClient(create_app(settings))
    html = client.get("/dashboard").text
    assert "/v1/dashboard/app.js" in html
    app_js = client.get("/v1/dashboard/app.js")
    assert app_js.status_code == 200
    assert "setTimeout(confirmDecision, 1500)" in app_js.text
    assert "请持续按住：" in app_js.text
    assert "confirmStatus" in app_js.text
    assert "正在分析…" in app_js.text
    assert "setAnalyzeState" in app_js.text
    assert "/cancel" in app_js.text
    assert "pointerdown" in app_js.text
    assert "window.location.protocol" in app_js.text
    assert "/v1/dashboard/qr?url=" in app_js.text
    assert "action-button" in html
    assert "analyzeStatus" in html
    assert "et0ForecastChart" in html
    assert "soilForecastChart" in html
    assert "未来一小时 ET₀ 预测曲线" in html
    assert "forecastChart(points" in app_js.text
    assert "renderForecastCharts(forecastPoints)" in app_js.text
    assert "电脑端本地边缘网关 · ESP32 安全执行" not in html
    assert "font-size: 34px" in html
    assert "riskScoreNote" not in html
    assert "风险等级 " not in app_js.text
    assert "传感器异常：" in app_js.text
    assert "土壤湿度传感器" in app_js.text
    assert "ESP32 未来 30 分钟土壤趋势" in html
    assert "#E0E5EC" in html
    assert "--shadow-extruded" in html
    assert "prefers-reduced-motion" in html
    assert "api.qrserver.com" not in app_js.text
    assert "SpeechRecognition" in app_js.text
    assert "停止录音" in app_js.text
    assert "8 秒未检测到语音，已自动停止" in app_js.text
    assert "voiceRecognition.stop()" in app_js.text
    qr = client.get("/v1/dashboard/qr", params={"url": "http://192.168.1.20:8000/dashboard"})
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/png")
    assert qr.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert client.get("/v1/dashboard/qr", params={"url": "/dashboard"}).status_code == 400
    assert client.get("/v1/reports/water").status_code == 200
    assert client.get("/v1/events").status_code == 200
