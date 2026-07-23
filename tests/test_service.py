from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from dual_forecast.config import SETTINGS
from dual_forecast.schemas import SensorSnapshot
from dual_forecast.service import create_app


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


def test_schema_solar_mean_and_pressure_alias():
    snap = SensorSnapshot.model_validate(payload())
    assert snap.solar_mean() == 500
    assert snap.airPressureHpa == 1013
    snap = SensorSnapshot.model_validate(payload(solar2=False))
    assert snap.solar_mean() == 400


def test_service_warms_up_and_latest_is_missing(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    client = TestClient(create_app(settings))
    response = client.post("/v1/snapshots", json=payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "warming_up"
    assert body["requiredSamples"] == 288
    assert client.get("/v1/forecast/latest").status_code == 404


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
    assert "AIoT 农场监控" in page.text

    latest = client.get("/v1/dashboard/latest")
    assert latest.status_code == 200
    assert latest.json()["snapshot"]["air"]["temperatureC"] == 24.0


def test_live_telemetry_refreshes_dashboard_without_storing_model_sample(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    client = TestClient(create_app(settings))
    live_payload = payload()
    live_payload["air"]["temperatureC"] = 26.5

    assert client.post("/v1/telemetry/live", json=live_payload).status_code == 200
    latest = client.get("/v1/dashboard/latest").json()
    assert latest["snapshot"]["air"]["temperatureC"] == 26.5


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

    analysis = client.post("/v1/cloud/analyze").json()
    assert analysis["finalAction"] == "NO_OP"
    assert analysis["status"] == "disabled"

    chat = client.post("/v1/cloud/chat", json={"question": "今天要浇水吗？"}).json()
    assert not chat["llmUsed"]
    assert "本地离线模式" in chat["answer"]
