from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from dual_forecast.config import SETTINGS
from dual_forecast.decision import GatewayCall
from dual_forecast.schemas import IrrigationAction, ModelIrrigationDecision, SensorSnapshot
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


def test_health_exposes_safe_llm_defaults_and_manual_requires_forecast(tmp_path):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts")
    client = TestClient(create_app(settings))

    health = client.get("/health").json()
    assert health["gatewayConfigured"] is False
    assert health["llmEnabled"] is False
    assert health["actuatorMode"] == "simulated"
    assert client.get("/v1/decisions/latest").status_code == 404
    assert client.post("/v1/decisions/evaluate").status_code == 409


class NoOpGateway:
    configured = True

    def __init__(self):
        self.calls = 0

    def decide(self, context):
        self.calls += 1
        decision = ModelIrrigationDecision(
            schemaVersion="1.0",
            action=IrrigationAction.NO_OP,
            durationSeconds=None,
            reasonCode="NO_ACTION_NEEDED",
            reason="当前无需浇水",
            confidence=0.9,
        )
        return GatewayCall(decision=decision, raw_output=decision.model_dump_json(), latency_ms=1)


def test_complete_forecast_runs_decision_and_public_endpoints(tmp_path):
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "db.sqlite",
        artifact_dir=SETTINGS.artifact_dir,
        llm_enabled=True,
    )
    gateway = NoOpGateway()
    client = TestClient(create_app(settings, decision_gateway=gateway))

    response = None
    for i in range(288):
        response = client.post("/v1/snapshots", json=payload(i))

    assert response is not None
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["decision"]["finalAction"] == "NO_OP"
    assert gateway.calls == 1
    assert client.get("/v1/decisions/latest").status_code == 200

    manual = client.post("/v1/decisions/evaluate").json()
    assert manual["status"] == "dry_run"
    assert manual["executed"] is False
    assert gateway.calls == 2
