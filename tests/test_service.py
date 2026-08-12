from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading

from fastapi.testclient import TestClient

import dual_forecast.service as service_module
from dual_forecast.config import SETTINGS
from dual_forecast.irrigation import IrrigationService
from dual_forecast.schemas import SensorSnapshot
from dual_forecast.schemas import DeviceCloudResult, DeviceForecast, DeviceIrrigationState
from dual_forecast.service import create_app
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
    assert body["action"] == "START_WATERING"
    assert body["message"]
    assert body["safetyReasons"] == []


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
    assert "🌡️" in page.text
    assert "💧" in page.text
    assert "🌱" in page.text
    assert "☀️" in page.text
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
    assert body["decision"]["state"] == "CLOSED"
    assert body["cloud"]["status"] == "offline"
    assert body["device"]["cloudResult"]["finalAction"] == "NO_OP"


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
