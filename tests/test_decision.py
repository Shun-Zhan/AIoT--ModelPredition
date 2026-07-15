from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dual_forecast.config import SETTINGS
from dual_forecast.decision import (
    DecisionEngine,
    GatewayCall,
    GatewayFailure,
    SimulatedActuator,
    VolcengineGatewayClient,
)
from dual_forecast.schemas import (
    DecisionContext,
    IrrigationAction,
    ModelIrrigationDecision,
)
from dual_forecast.storage import Store


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


def context(*, request_id="request-1", moisture=25.0, valid=True):
    return DecisionContext(
        requestId=request_id,
        generatedAt=NOW,
        current={"soilMoisturePercent": moisture, "allSensorsValid": valid},
        trends={"soilMoistureDeltaPercent": -1.0},
        forecast={"status": "ok", "points": []},
        actuator={"mode": "simulated", "state": "OFF"},
        constraints={"maxDurationSeconds": 60},
    )


def model_decision(action=IrrigationAction.START_WATERING, duration=30):
    return ModelIrrigationDecision(
        schemaVersion="1.0",
        action=action,
        durationSeconds=duration,
        reasonCode="FORECAST_DRYING",
        reason="预测湿度继续下降",
        confidence=0.9,
    )


class FakeGateway:
    configured = True

    def __init__(self, decision=None, error=None):
        self.value = decision or model_decision()
        self.error = error
        self.calls = 0

    def decide(self, decision_context):
        self.calls += 1
        if self.error:
            raise self.error
        return GatewayCall(
            decision=self.value,
            raw_output=self.value.model_dump_json(),
            latency_ms=12,
            prompt_tokens=100,
            completion_tokens=20,
        )


def engine(tmp_path: Path, gateway, **setting_overrides):
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "decision.sqlite3",
        llm_enabled=True,
        actuator_mode="simulated",
        **setting_overrides,
    )
    clock = lambda: NOW
    actuator = SimulatedActuator(clock)
    store = Store(settings.database_path)
    return DecisionEngine(settings, store, gateway=gateway, actuator=actuator, now=clock), store


def test_model_decision_contract_rejects_non_json_shapes():
    assert model_decision().action == IrrigationAction.START_WATERING
    with pytest.raises(ValidationError):
        ModelIrrigationDecision.model_validate_json(
            '{"schemaVersion":"1.0","action":"START_WATERING","durationSeconds":null,'
            '"reasonCode":"DRY","reason":"dry","confidence":0.9}'
        )
    with pytest.raises(ValidationError):
        ModelIrrigationDecision.model_validate_json(
            '{"schemaVersion":"1.0","action":"NO_OP","durationSeconds":10,'
            '"reasonCode":"OK","reason":"ok","confidence":0.9}'
        )
    with pytest.raises(ValidationError):
        ModelIrrigationDecision.model_validate_json(
            '{"schemaVersion":"1.0","action":"START_WATERING","durationSeconds":61,'
            '"reasonCode":"DRY","reason":"dry","confidence":0.9}'
        )
    with pytest.raises(ValidationError):
        ModelIrrigationDecision.model_validate_json(
            '{"schemaVersion":"1.0","action":"NO_OP","durationSeconds":null,'
            '"reasonCode":"OK","reason":"ok","confidence":0.9,"extra":true}'
        )


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
        )


def test_volcengine_client_uses_openai_compatible_contract():
    completions = FakeCompletions(model_decision().model_dump_json())
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    gateway = VolcengineGatewayClient(SETTINGS, api_key="test-only", client=client)

    result = gateway.decide(context())

    assert result.decision.action == IrrigationAction.START_WATERING
    assert result.prompt_tokens == 12
    assert completions.kwargs["model"] == "doubao-1.5-thinking-pro"
    assert completions.kwargs["stream"] is False
    assert context().requestId in completions.kwargs["messages"][1]["content"]


def test_volcengine_client_rejects_markdown_response():
    completions = FakeCompletions("```json\n{}\n```")
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    gateway = VolcengineGatewayClient(SETTINGS, api_key="test-only", client=client)

    with pytest.raises(GatewayFailure):
        gateway.decide(context())


def test_valid_start_is_executed_and_audited(tmp_path):
    gateway = FakeGateway()
    decision_engine, store = engine(tmp_path, gateway)

    result = decision_engine.evaluate(context(), trigger="test", execute=True)

    assert result.status == "executed"
    assert result.finalAction == IrrigationAction.START_WATERING
    assert result.executed is True
    assert decision_engine.actuator.is_watering is True
    assert store.daily_watering_seconds(NOW) == 30
    assert store.latest_decision() == result


def test_invalid_sensor_skips_gateway(tmp_path):
    gateway = FakeGateway()
    decision_engine, _ = engine(tmp_path, gateway)

    result = decision_engine.evaluate(context(valid=False), trigger="test", execute=True)

    assert result.status == "rejected"
    assert result.finalAction == IrrigationAction.NO_OP
    assert gateway.calls == 0


@pytest.mark.parametrize("message", ["timeout", "401 unauthorized", "429 rate limit", "500 upstream", "empty response"])
def test_gateway_failures_fall_back_to_no_op(tmp_path, message):
    gateway = FakeGateway(error=GatewayFailure(message, latency_ms=10))
    decision_engine, _ = engine(tmp_path, gateway)

    result = decision_engine.evaluate(context(), trigger="test", execute=True)

    assert result.status == "gateway_error"
    assert result.finalAction == IrrigationAction.NO_OP
    assert result.executed is False


def test_gateway_failure_stops_running_actuator(tmp_path):
    gateway = FakeGateway(error=GatewayFailure("timeout"))
    decision_engine, store = engine(tmp_path, gateway)
    decision_engine.actuator.apply(IrrigationAction.START_WATERING, 30, NOW)

    result = decision_engine.evaluate(context(), trigger="test", execute=True)

    assert result.finalAction == IrrigationAction.STOP_WATERING
    assert result.executed is True
    assert decision_engine.actuator.is_watering is False
    assert store.latest_decision() == result


def test_cooldown_and_daily_limit_override_model(tmp_path):
    gateway = FakeGateway()
    decision_engine, store = engine(tmp_path, gateway)
    store.record_actuator_event(
        NOW - timedelta(minutes=5),
        "earlier",
        IrrigationAction.START_WATERING,
        30,
        "simulated",
    )

    cooldown = decision_engine.evaluate(context(), trigger="test", execute=True)
    assert cooldown.status == "rejected"
    assert "watering cooldown is active" in cooldown.safetyReasons

    limited_gateway = FakeGateway()
    limited_engine, _ = engine(tmp_path / "daily", limited_gateway, max_daily_watering_seconds=20)
    limited = limited_engine.evaluate(context(request_id="daily-limit"), trigger="test", execute=True)
    assert limited.status == "rejected"
    assert "daily watering limit would be exceeded" in limited.safetyReasons


def test_request_id_is_idempotent(tmp_path):
    gateway = FakeGateway()
    decision_engine, _ = engine(tmp_path, gateway)

    first = decision_engine.evaluate(context(), trigger="test", execute=False)
    second = decision_engine.evaluate(context(), trigger="test", execute=True)

    assert second == first
    assert gateway.calls == 1
    assert second.executed is False


def test_periodic_and_threshold_triggers(tmp_path):
    gateway = FakeGateway(model_decision(IrrigationAction.NO_OP, None))
    decision_engine, _ = engine(tmp_path, gateway)
    decision_engine.evaluate(context(), trigger="first", execute=False)

    assert decision_engine.should_evaluate(
        now=NOW + timedelta(minutes=5),
        previous_moisture=40,
        current_moisture=39,
        health_changed=False,
    ) is None
    assert decision_engine.should_evaluate(
        now=NOW + timedelta(minutes=16),
        previous_moisture=40,
        current_moisture=39,
        health_changed=False,
    ) == "periodic"
    assert decision_engine.should_evaluate(
        now=NOW + timedelta(minutes=5),
        previous_moisture=31,
        current_moisture=29,
        health_changed=False,
    ) == "moisture_threshold_crossed"
    assert decision_engine.should_evaluate(
        now=NOW + timedelta(minutes=5),
        previous_moisture=40,
        current_moisture=40,
        health_changed=True,
    ) == "sensor_health_changed"


def test_simulated_actuator_stops_at_deadline():
    clock = [NOW]
    actuator = SimulatedActuator(lambda: clock[0])
    actuator.apply(IrrigationAction.START_WATERING, 30, NOW)
    assert actuator.is_watering is True
    clock[0] = NOW + timedelta(seconds=31)
    assert actuator.is_watering is False
