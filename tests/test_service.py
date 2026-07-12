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

