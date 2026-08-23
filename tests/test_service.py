from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading

from fastapi.testclient import TestClient

import dual_forecast.service as service_module
from dual_forecast.config import SETTINGS
from dual_forecast.irrigation import IrrigationService
from dual_forecast.schemas import SensorSnapshot
from dual_forecast.schemas import DeviceCloudResult, DeviceForecast, DeviceIrrigationState, ForecastResponse
from dual_forecast.service import create_app, demo_forecast_from_live_snapshot
from dual_forecast.storage import Store


def payload(i=0, *, solar1=True, solar2=True):
    return {
        "uptimeMs": i * 300000,
        "windOk": True, "windVoltage": 1.2, "windSpeedMs": 2.0,
        "airOk": True, "air": {"temperatureC": 24.0, "humidityPercent": 65.0},
        "soilOk": True, "soil": {"temperatureC": 21.0, "moisturePercent": 55.0},
        "solar1Ok": solar1, "solarRadiation1Wm2": 400,
        "solar2Ok": solar2, "solarRadiation2Wm2": 600,
        "AirPressure": 1013,
        "flow": {
            "ok": True,
            "signalPin": 12,
            "zeroIsValid": True,
            "pulseCount": i * 450,
            "frequencyHz": 0.0,
            "flowRateLpm": 0.0,
            "totalLiters": float(i),
        },
        "receivedAt": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)).isoformat(),
    }


def test_schema_uses_solar2_as_incoming_and_solar1_as_reflection():
    snap = SensorSnapshot.model_validate(payload())
    assert snap.incoming_solar() == 600
    assert snap.reflected_solar() == 400
    assert snap.net_shortwave_solar() == (200, "measured_reflection")
    assert snap.airPressureHpa == 1013
    snap = SensorSnapshot.model_validate(payload(solar2=False))
    assert snap.net_shortwave_solar() == (None, "incoming_invalid")
    snap = SensorSnapshot.model_validate(payload(solar1=False))
    assert snap.net_shortwave_solar() == (462, "default_albedo_fallback")


def test_dashboard_exposes_flow_meter_data():
    snap = SensorSnapshot.model_validate(payload(i=2))
    dashboard = service_module.snapshot_to_dashboard(
        snap, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    assert dashboard["flow"]["flowRateLpm"] == 0.0
    assert dashboard["flow"]["totalLiters"] == 2.0


def test_service_warms_up_and_latest_is_missing(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    client = TestClient(create_app(settings))
    response = client.post("/v1/snapshots", json=payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "warming_up"
    assert body["requiredSamples"] == 288
    assert client.get("/v1/forecast/latest").status_code == 404


def test_health_starts_without_desktop_models_when_esp32_is_authoritative(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "db.sqlite",
        artifact_dir=tmp_path / "artifacts",
    )

    response = TestClient(create_app(settings)).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "modelsReady": False,
        "mode": "device_authoritative",
        "fastTestMode": settings.fast_test_mode,
        "requiredSamples": settings.required_samples,
    }


def test_device_dashboard_does_not_present_stale_cached_data_as_live(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "db.sqlite",
        artifact_dir=tmp_path / "artifacts",
    )
    store = Store(settings.database_path)
    old_received_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    store.save_live_snapshot(SensorSnapshot.model_validate(payload()), old_received_at)
    store.insert_snapshot(SensorSnapshot.model_validate(payload()), old_received_at, [])

    latest = TestClient(create_app(settings)).get("/v1/dashboard/latest").json()

    assert latest["snapshot"] is None


def test_device_command_response_has_user_visible_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "db.sqlite",
        artifact_dir=tmp_path / "artifacts",
    )
    client = TestClient(create_app(settings))

    response = client.post("/v1/actuator/debug/open")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["queued"] is True
    assert body["requestId"]
    assert body["action"] == "DEBUG_VALVE_PULSE"
    assert body["message"]
    assert body["safetyReasons"] == []


def test_device_analysis_hides_cached_result_until_matching_llm_result(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    store = Store(settings.database_path)
    previous_boot_result_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", status="awaiting_confirmation", requestId="old-result",
        action="START_WATERING", proposedAction="START_WATERING",
        finalAction="START_WATERING", durationSeconds=16, reason="old result",
        expiresAt=datetime.now(timezone.utc) + timedelta(minutes=2),
    ))
    client = TestClient(create_app(settings))

    queued = client.post("/v1/cloud/analyze").json()
    status = client.get("/v1/cloud/status").json()

    assert queued["status"] == "queued"
    assert status["latestCall"]["status"] == "pending"
    assert status["latestCall"]["requestId"] == queued["requestId"]
    assert status["decision"]["status"] == "pending"
    assert status["decision"]["requestId"] != "old-result"

    app_js = client.get("/v1/dashboard/app.js").text
    assert "> 150000" in app_js
    assert "45 秒内没有收到" not in app_js


def test_device_cloud_status_uses_telemetry_runtime_not_previous_result(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    live = payload()
    live["receivedAt"] = datetime.now(timezone.utc).isoformat()
    live["cloudRuntime"] = {
        "initialized": True,
        "enabled": True,
        "apiKeyConfigured": True,
        "requestPending": True,
    }
    client = TestClient(create_app(settings))

    assert client.post("/v1/telemetry/live", json=live).status_code == 200
    status = client.get("/v1/cloud/status").json()

    assert status["enabled"] is True
    assert status["configured"] is True
    assert status["cloudRuntime"]["requestPending"] is True
    assert status["latestCall"] is None


def test_device_confirmation_ack_is_reflected_in_decision_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    store = Store(settings.database_path)
    source_id = "cloud-result-current"
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", status="awaiting_confirmation", requestId=source_id,
        action="START_WATERING", proposedAction="START_WATERING",
        finalAction="START_WATERING", durationSeconds=16, reason="soil dry",
        expiresAt=datetime.now(timezone.utc) + timedelta(minutes=2),
    ))
    command = {
        "schemaVersion": "2.0", "requestId": "confirm-current",
        "action": "CONFIRM_WATERING", "sourceRequestId": source_id,
        "durationSeconds": 16, "reasonCode": "UI",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "ttlSeconds": 30, "transport": "UI_COMMAND",
    }
    assert store.enqueue_command(command)
    store.record_ack({
        "requestId": "confirm-current", "accepted": False,
        "action": "CONFIRM_WATERING", "reason": "cooldown", "actualState": "CLOSED",
    })

    decision = TestClient(create_app(settings)).get("/v1/cloud/status").json()["decision"]

    assert decision["status"] == "rejected_on_confirmation"
    assert decision["finalAction"] == "NO_OP"
    assert decision["safetyReasons"] == ["cooldown"]


def test_device_volume_completion_overrides_stale_cloud_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    store = Store(settings.database_path)
    request_id = "volume-complete-1"
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", status="rejected", requestId=request_id,
        action="START_WATERING", proposedAction="START_WATERING",
        finalAction="NO_OP", durationSeconds=30, reason="soil dry",
        reasonCode="SEVERE_SOIL_DRYNESS", safetyReasons=["valve is already open"],
        expiresAt=datetime.now(timezone.utc) + timedelta(minutes=2),
    ))
    store.save_device_irrigation_state(DeviceIrrigationState(
        schemaVersion="2.0", state="CLOSED", action="STOP_WATERING",
        reasonCode="volume_reached_closed", deliveredLiters=0.031111,
        targetLiters=0.032117, remainingLiters=0.001006,
        flowPulseCount=36, wateringControlMode="volume_closed_loop",
    ))

    decision = TestClient(create_app(settings)).get("/v1/cloud/status").json()["decision"]

    assert decision["status"] == "completed"
    assert decision["finalAction"] == "NO_OP"
    assert decision["reasonCode"] == "VOLUME_REACHED_CLOSED"
    assert decision["safetyReasons"] == []


def test_old_device_volume_completion_does_not_override_new_cloud_result(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    store = Store(settings.database_path)
    store.save_device_irrigation_state(DeviceIrrigationState(
        schemaVersion="2.0", state="CLOSED", action="STOP_WATERING",
        reasonCode="volume_reached_closed", deliveredLiters=0.031111,
        targetLiters=0.032117, remainingLiters=0.001006,
        flowPulseCount=36, wateringControlMode="volume_closed_loop",
    ))
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", status="rejected", requestId="new-analysis-1",
        action="START_WATERING", proposedAction="START_WATERING",
        finalAction="NO_OP", durationSeconds=30, reason="new result",
        reasonCode="COOLDOWN", safetyReasons=["watering cooldown is active"],
        expiresAt=datetime.now(timezone.utc) + timedelta(minutes=2),
    ))

    decision = TestClient(create_app(settings)).get("/v1/cloud/status").json()["decision"]

    assert decision["status"] == "rejected"
    assert decision["reasonCode"] == "COOLDOWN"
    assert decision["safetyReasons"] == ["watering cooldown is active"]


def test_duplicate_is_reported(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    client = TestClient(create_app(settings))
    client.post("/v1/snapshots", json=payload())
    body = client.post("/v1/snapshots", json=payload()).json()
    assert any("duplicate" in item for item in body["warnings"])


def test_dashboard_exposes_latest_snapshot(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    client = TestClient(create_app(settings))
    client.post("/v1/snapshots", json=payload())

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "AIoT 智慧灌溉监控" in page.text

    latest = client.get("/v1/dashboard/latest")
    assert latest.status_code == 200
    snapshot = latest.json()["snapshot"]
    assert snapshot["air"]["temperatureC"] == 24.0
    assert snapshot["et0Ok"]
    assert snapshot["et0MmPerHour"] > 0
    assert snapshot["solarRadiationWm2"] == 200
    assert snapshot["et0Method"].startswith("FAO-56")
    assert "参考作物蒸散率（ET₀）" in page.text
    assert "净短波辐射（Rns）" in page.text
    assert "sensor-card" in page.text
    assert "实时流量（YF-S201）" in page.text
    assert "flowRate" in page.text
    assert "🌡️" in page.text
    assert "💧" in page.text
    assert "🌱" in page.text
    assert "☀️" in page.text
    app_js = client.get("/v1/dashboard/app.js")
    assert app_js.status_code == 200
    assert "当前 ESP32 固件不支持人工调试开阀动作" in app_js.text
    edge = latest.json()["edge"]
    assert edge["thresholds"]["irrigationSoilMoisturePercent"] == 30.0
    assert edge["thresholds"]["unit"] == "%"
    assert "无物理单位" in edge["riskScoreNote"]


def test_live_telemetry_refreshes_dashboard_without_storing_model_sample(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    client = TestClient(create_app(settings))
    live_payload = payload()
    live_payload["air"]["temperatureC"] = 26.5

    assert client.post("/v1/telemetry/live", json=live_payload).status_code == 200
    latest = client.get("/v1/dashboard/latest").json()
    assert latest["snapshot"]["air"]["temperatureC"] == 26.5
    assert latest["forecast"]["status"] == "demo_preview"
    assert latest["forecast"]["historySource"] == "live_demo_projection"
    assert latest["forecast"]["displayOnly"] is True
    assert len(latest["forecast"]["forecast"]) == 12
    assert client.get("/v1/forecast/latest").status_code == 404


def test_dashboard_replaces_warming_up_placeholder_points_with_demo_preview(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    store = Store(settings.database_path)
    store.save_device_forecast(DeviceForecast(
        schemaVersion="2.0", generatedAt="1970-01-01T00:00:00Z", status="warming_up",
        modelVersion="nbeats-et0-v1", availableSamples=7, requiredSamples=288,
        forecast=[
            {"timestamp": "1970-01-01T00:00:00Z", "et0Mm": 0.0, "soilMoisturePercent": 0.0}
            for _ in range(12)
        ],
    ))
    client = TestClient(create_app(settings))
    live_payload = payload()

    assert client.post("/v1/telemetry/live", json=live_payload).status_code == 200
    latest = client.get("/v1/dashboard/latest").json()

    assert latest["forecast"]["status"] == "demo_preview"
    assert latest["forecast"]["historySource"] == "live_demo_projection"
    assert latest["forecast"]["displayOnly"] is True
    assert len(latest["forecast"]["forecast"]) == 12
    assert latest["deviceForecast"]["status"] == "warming_up"
    assert latest["deviceForecast"]["availableSamples"] == 7


def test_dashboard_keeps_valid_device_forecast_over_demo_preview(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    store = Store(settings.database_path)
    store.save_device_forecast(DeviceForecast(
        schemaVersion="2.0", generatedAt="2026-08-23T01:00:00Z", status="ok",
        modelVersion="nbeats-et0-v1", availableSamples=288, requiredSamples=288,
        forecast=[
            {"timestamp": "2026-08-23T01:05:00Z", "et0Mm": 0.01, "soilMoisturePercent": 42.0}
        ],
    ))
    client = TestClient(create_app(settings))
    live_payload = payload()

    assert client.post("/v1/telemetry/live", json=live_payload).status_code == 200
    latest = client.get("/v1/dashboard/latest").json()

    assert latest["forecast"]["status"] == "ok"
    assert latest["forecast"]["historySource"] is None
    assert latest["forecast"]["forecast"][0]["soilMoisturePercent"] == 42.0


def test_legacy_desktop_mode_keeps_valid_reference_forecast_when_device_is_warming_up(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    store = Store(settings.database_path)
    store.save_device_forecast(DeviceForecast(
        schemaVersion="2.0", generatedAt="1970-01-01T00:00:00Z", status="warming_up",
        availableSamples=7, requiredSamples=288,
        forecast=[
            {"timestamp": "1970-01-01T00:00:00Z", "et0Mm": 0.0, "soilMoisturePercent": 0.0}
            for _ in range(12)
        ],
    ))
    client = TestClient(create_app(settings))
    live_payload = payload()
    assert client.post("/v1/telemetry/live", json=live_payload).status_code == 200

    store.save_forecast(ForecastResponse(
        status="ok", generatedAt="2026-08-23T01:00:00Z", requiredSamples=288,
        availableSamples=288,
        forecast=[{"timestamp": "2026-08-23T01:05:00Z", "et0Mm": 0.02, "soilMoisturePercent": 45.0}],
    ))
    latest = client.get("/v1/dashboard/latest").json()

    assert latest["forecast"]["status"] == "ok"
    assert latest["forecast"]["forecast"][0]["soilMoisturePercent"] == 45.0


def test_demo_forecast_requires_live_soil_and_et0_and_is_bounded():
    assert demo_forecast_from_live_snapshot(None) is None
    assert demo_forecast_from_live_snapshot({"soil": {"moisturePercent": 50}}) is None
    preview = demo_forecast_from_live_snapshot({
        "soil": {"moisturePercent": 55},
        "et0MmPerHour": 0.24,
    })
    assert preview["status"] == "demo_preview"
    assert preview["safetyLocked"] is True
    assert len(preview["forecast"]) == 12
    assert preview["forecast"][-1]["soilMoisturePercent"] < 55


def test_dashboard_prefers_device_forecast_decision_and_cloud_result(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    store = Store(settings.database_path)
    store.save_device_forecast(DeviceForecast(
        schemaVersion="2.0", generatedAt="2026-08-11T08:00:00Z", status="ok",
        nextHourEt0Mm=0.18, soilMoistureInOneHour=41.2,
    ))
    store.save_device_irrigation_state(DeviceIrrigationState(
        schemaVersion="2.0", updatedAt="2026-08-11T08:00:01Z", state="CLOSED", action="NO_OP",
    ))
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", updatedAt="2026-08-11T08:00:02Z", status="offline",
        finalAction="NO_OP", reason="network unavailable",
    ))

    client = TestClient(create_app(settings))
    latest = client.get("/v1/dashboard/latest")
    assert latest.status_code == 200
    body = latest.json()
    assert body["forecast"]["schemaVersion"] == "2.0"
    assert body["forecast"]["soilMoistureInOneHour"] == 41.2
    assert body["decision"]["finalAction"] == "NO_OP"
    assert body["cloud"]["status"] == "offline"
    assert body["device"]["cloudResult"]["finalAction"] == "NO_OP"


def test_dashboard_renders_volume_closed_loop_and_flow_fault(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    store = Store(settings.database_path)
    store.save_device_irrigation_state(DeviceIrrigationState(
        schemaVersion="2.0", updatedAt="2026-08-11T08:00:01Z", state="CLOSED",
        action="NO_OP", reasonCode="flow_fault",
        targetLiters=8.0, deliveredLiters=3.2, remainingLiters=4.8,
        flowRateLpm=0.0, flowPulseCount=128, flowFault=True,
        flowFaultReason="flow_fault", wateringControlMode="volume_closed_loop",
    ))

    client = TestClient(create_app(settings))
    latest = client.get("/v1/dashboard/latest")
    assert latest.status_code == 200
    irrigation = latest.json()["deviceIrrigationState"]
    assert irrigation["targetLiters"] == 8.0
    assert irrigation["deliveredLiters"] == 3.2
    assert irrigation["remainingLiters"] == 4.8
    assert irrigation["flowRateLpm"] == 0.0
    assert irrigation["flowPulseCount"] == 128
    assert irrigation["flowFault"] is True
    assert irrigation["flowFaultReason"] == "flow_fault"
    assert irrigation["wateringControlMode"] == "volume_closed_loop"
    assert irrigation["reasonCode"] == "flow_fault"

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "irrigationVolume" in page.text
    assert "flowFaultAlert" in page.text

    app_js = client.get("/v1/dashboard/app.js").text
    assert "ESP32 按流量脉冲自动关阀" in app_js
    assert "8 秒内无流量" in app_js
    assert "volume_closed_loop" in app_js
    assert "function volumeText(value)" in app_js
    assert "volumeText(irrigation.targetLiters)" in app_js
    assert "milliliters.toFixed(digits) + ' mL'" in app_js
    assert "本地 ET₀ 目标：" in app_js
    assert "云端最长窗口：" in app_js
    assert "实际水量由 ESP32 本地 ET₀ 目标和流量脉冲决定" in app_js
    assert "本次灌溉已完成，水阀已关闭。" in app_js


def test_device_dashboard_marks_old_rejection_as_expired_not_current_safety(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    store = Store(settings.database_path)
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", status="rejected", requestId="old-cloud-result",
        action="START_WATERING", proposedAction="START_WATERING", finalAction="NO_OP",
        durationSeconds=39, reason="soil is dry",
        expiresAt=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        safetyReasons=["watering cooldown is active"],
    ))

    body = TestClient(create_app(settings)).get("/v1/dashboard/latest").json()

    assert body["decision"]["status"] == "expired"
    assert body["decision"]["safetyReasons"] == ["decision has expired"]
    assert body["decision"]["finalAction"] == "NO_OP"


def test_dashboard_has_neutral_expired_copy_for_demo_mode(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "demo-ui.sqlite", artifact_dir=tmp_path / "artifacts")
    app_js = TestClient(create_app(settings)).get("/v1/dashboard/app.js").text
    assert "var demoModeActive = false;" in app_js
    assert "等待新的云端分析结果。" in app_js


def test_device_reboot_keeps_previous_cloud_result_as_non_executable_history(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    store = Store(settings.database_path)
    previous_boot_result_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", status="rejected", requestId="previous-boot",
        action="START_WATERING", proposedAction="START_WATERING", finalAction="NO_OP",
        durationSeconds=60, reason="soil is dry",
        expiresAt=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        safetyReasons=["watering cooldown is active"],
    ), previous_boot_result_at)
    live = payload()
    live["uptimeMs"] = 5_000
    live["receivedAt"] = datetime.now(timezone.utc).isoformat()

    client = TestClient(create_app(settings))
    assert client.post("/v1/telemetry/live", json=live).status_code == 200
    latest = client.get("/v1/dashboard/latest").json()
    status = client.get("/v1/cloud/status").json()

    assert latest["cloud"]["status"] == "expired"
    assert latest["cloud"]["finalAction"] == "NO_OP"
    assert "重新请求云端分析" in latest["cloud"]["safetyReasons"][0]
    assert latest["deviceCloudResult"]["status"] == "expired"
    assert status["latestCall"]["status"] == "expired"
    assert status["decision"]["status"] == "expired"
    assert status["decision"]["finalAction"] == "NO_OP"


def test_device_reboot_automatically_queues_one_fresh_cloud_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOT_DEVICE_AUTHORITATIVE", "1")
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts"
    )
    store = Store(settings.database_path)
    previous_boot_result_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    store.save_device_cloud_result(DeviceCloudResult(
        schemaVersion="2.0", status="awaiting_confirmation", requestId="previous-boot",
        action="START_WATERING", proposedAction="START_WATERING", finalAction="START_WATERING",
        durationSeconds=30, reason="soil is dry",
        expiresAt=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
    ), previous_boot_result_at)
    live = payload()
    live["uptimeMs"] = 5_000
    live["receivedAt"] = datetime.now(timezone.utc).isoformat()
    live["cloudRuntime"] = {
        "initialized": True, "enabled": True,
        "apiKeyConfigured": True, "requestPending": False,
    }

    client = TestClient(create_app(settings))
    assert client.post("/v1/telemetry/live", json=live).status_code == 200
    latest = client.get("/v1/dashboard/latest").json()

    assert latest["decision"]["status"] == "pending"
    queued = store.latest_command("CLOUD_ANALYZE")
    assert queued is not None
    assert queued["command"]["reasonCode"] == "AUTO_REANALYZE_AFTER_RESTART"
    assert queued["command"]["ttlSeconds"] == 180
    client.get("/v1/dashboard/latest")
    assert store.latest_command("CLOUD_ANALYZE")["requestId"] == queued["requestId"]


def test_cloud_and_actuator_endpoints_are_safe_by_default(tmp_path):
    settings = replace(
        SETTINGS, database_path=tmp_path / "db.sqlite",
        artifact_dir=tmp_path / "artifacts", llm_enabled=False,
    )
    client = TestClient(create_app(settings))
    status = client.get("/v1/cloud/status")
    assert status.status_code == 200
    assert not status.json()["enabled"]
    assert status.json()["actuator"]["state"] == "CLOSED"
    assert not status.json()["autoIrrigation"]["enabled"]
    assert status.json()["operationMode"] == "semi_automatic"

    automatic = client.post("/v1/operation-mode", json={"mode": "automatic"})
    assert automatic.status_code == 200
    assert automatic.json()["mode"] == "automatic"
    assert automatic.json()["automaticIntervalSeconds"] == 60
    assert automatic.json()["nextAutomaticAnalysisAt"]
    switched_status = client.get("/v1/cloud/status").json()
    assert switched_status["operationMode"] == "automatic"
    assert switched_status["autoIrrigation"]["enabled"]

    invalid_mode = client.post("/v1/operation-mode", json={"mode": "manual"})
    assert invalid_mode.status_code == 422

    semi = client.post("/v1/operation-mode", json={"mode": "semi_automatic"})
    assert semi.status_code == 200
    assert not client.get("/v1/cloud/status").json()["autoIrrigation"]["enabled"]

    analysis = client.post("/v1/cloud/analyze").json()
    assert analysis["finalAction"] == "NO_OP"
    assert analysis["status"] == "disabled"

    debug_open = client.post("/v1/actuator/debug/open").json()
    assert not debug_open["queued"]
    assert debug_open["status"] == "rejected"
    debug_close = client.post("/v1/actuator/debug/close").json()
    assert debug_close["queued"]
    assert debug_close["action"] == "STOP_WATERING"
    debug_status = client.get(f"/v1/actuator/debug/{debug_close['requestId']}")
    assert debug_status.status_code == 200
    assert debug_status.json()["status"] == "pending"

    chat = client.post("/v1/cloud/chat", json={"question": "今天要浇水吗？"}).json()
    assert not chat["llmUsed"]
    assert "本地离线模式" in chat["answer"]


def test_automatic_mode_worker_calls_ai_on_automatic_interval(tmp_path, monkeypatch):
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "db.sqlite",
        artifact_dir=tmp_path / "artifacts",
        llm_enabled=True,
        auto_irrigation_enabled=True,
    )
    called = threading.Event()
    triggers = []

    def record_analysis(self, *, trigger="manual"):
        triggers.append(trigger)
        called.set()
        return None

    monkeypatch.setattr(service_module, "AUTO_ANALYSIS_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(IrrigationService, "analyze", record_analysis)

    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/cloud/status").json()["operationMode"] == "semi_automatic"
        assert client.post("/v1/operation-mode", json={"mode": "automatic"}).status_code == 200
        assert called.wait(1)

    assert triggers[0] == "automatic_minute"
