from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from dual_forecast.cloud import CloudCall, CloudFailure
from dual_forecast.config import SETTINGS
from dual_forecast.esp32_receiver import _handle_ack_line, _send_pending_commands
from dual_forecast.irrigation import IrrigationService
from dual_forecast.schemas import ForecastPoint, ForecastResponse, IrrigationAction, IrrigationDecision, SensorSnapshot
from dual_forecast.storage import Store


def snapshot(moisture=20.0, *, soil_ok=True):
    return SensorSnapshot.model_validate({
        "uptimeMs": 1000, "windOk": True, "windVoltage": 0.1, "windSpeedMs": 1.0,
        "airOk": True, "air": {"temperatureC": 25, "humidityPercent": 60},
        "soilOk": soil_ok, "soil": {"temperatureC": 22, "moisturePercent": moisture},
        "solar1Ok": True, "solarRadiation1Wm2": 400,
        "solar2Ok": True, "solarRadiation2Wm2": 420, "AirPressure": 1013,
    })


def decision(request_id="request-123", *, action=IrrigationAction.START_WATERING,
             duration=30, expires_delta=30):
    return IrrigationDecision(
        schemaVersion="1.0", requestId=request_id, action=action,
        durationSeconds=duration if action == IrrigationAction.START_WATERING else None,
        reasonCode="SOIL_DRY", reason="soil is dry", confidence=0.9,
        expiresAt=datetime.now(timezone.utc) + timedelta(seconds=expires_delta),
    )


def service(tmp_path, *, enabled=False):
    settings = replace(SETTINGS, database_path=tmp_path / "db.sqlite", artifact_dir=tmp_path / "artifacts", llm_enabled=enabled)
    store = Store(settings.database_path)
    now = datetime.now(timezone.utc)
    store.insert_snapshot(snapshot(), now, [])
    store.save_live_snapshot(snapshot(), now)
    return IrrigationService(store, settings), store


def test_start_waits_for_human_confirmation_then_is_queued(tmp_path):
    svc, store = service(tmp_path)
    context = svc.current_context("request-123")
    result = svc.evaluate(decision(), context, trigger="test")
    assert result.status == "awaiting_confirmation"
    assert store.claim_pending_commands() == []
    confirmed = svc.confirm(result.requestId)
    assert confirmed.status == "confirmed_waiting_device"
    queued = store.claim_pending_commands()
    assert queued[0]["action"] == "START_WATERING"
    assert queued[0]["durationSeconds"] == 30


def test_cancelled_suggestion_never_queues_a_device_command(tmp_path):
    svc, store = service(tmp_path)
    result = svc.evaluate(decision(), svc.current_context("request-123"), trigger="test")

    cancelled = svc.cancel(result.requestId)

    assert cancelled.status == "cancelled_by_user"
    assert cancelled.finalAction == IrrigationAction.NO_OP
    assert cancelled.durationSeconds is None
    assert not cancelled.humanConfirmed
    assert store.claim_pending_commands() == []


def test_expired_and_incomplete_sensor_decisions_are_rejected(tmp_path):
    svc, _ = service(tmp_path)
    expired = svc.evaluate(decision("request-expired", expires_delta=-1), svc.current_context("request-expired"), trigger="test")
    assert expired.status == "rejected"
    assert "expired" in expired.safetyReasons[0]

    svc.store.save_live_snapshot(snapshot(soil_ok=False), datetime.now(timezone.utc))
    invalid = svc.evaluate(decision("request-invalid"), svc.current_context("request-invalid"), trigger="test")
    assert invalid.status == "rejected"
    assert any("incomplete" in item for item in invalid.safetyReasons)


def test_request_id_is_idempotent(tmp_path):
    svc, _ = service(tmp_path)
    context = svc.current_context("request-dup")
    first = svc.evaluate(decision("request-dup"), context, trigger="test")
    second = svc.evaluate(decision("request-dup", action=IrrigationAction.NO_OP), context, trigger="test")
    assert second == first


class FailingGateway:
    configured = True

    def irrigation_decision(self, context):
        raise CloudFailure("network down")


def test_cloud_failure_keeps_local_service_safe(tmp_path):
    svc, store = service(tmp_path, enabled=True)
    svc.gateway = FailingGateway()
    result = svc.analyze()
    assert result.status == "gateway_error"
    assert result.finalAction == IrrigationAction.NO_OP
    assert store.latest_snapshot() is not None


def test_context_includes_configured_farm_profile_seven_day_trends_and_no_weather_guess(tmp_path):
    settings = replace(
        SETTINGS,
        database_path=tmp_path / "farm.sqlite",
        artifact_dir=tmp_path / "artifacts",
        farm_crop="番茄",
        farm_growth_stage="开花结果期",
        farm_soil_type="壤土",
        farm_plot_area_m2=12.0,
        farm_irrigation_method="滴灌",
    )
    store = Store(settings.database_path)
    now = datetime.now(timezone.utc)
    earlier = snapshot(42.0)
    latest = snapshot(38.0)
    latest.uptimeMs = 1000 + 2 * 60 * 60 * 1000
    store.insert_snapshot(earlier, now - timedelta(hours=2), [])
    store.insert_snapshot(latest, now, [])
    store.save_live_snapshot(latest, now)
    context = IrrigationService(store, settings).current_context("farm-profile-123")

    assert context.constraints["farmProfile"] == {
        "crop": "番茄", "growthStage": "开花结果期", "soilType": "壤土",
        "plotAreaM2": 12.0, "irrigationMethod": "滴灌", "valveFlowLpm": None,
        "operatorNotes": None, "status": "configured",
    }
    soil = context.trends["windows"]["last7Days"]["soil_moisture_percent"]
    assert soil["change"] == -4.0
    assert soil["changePerHour"] == -2.0
    assert context.constraints["weather"]["status"] == "not_configured"


def test_ack_updates_final_execution_state(tmp_path):
    svc, store = service(tmp_path)
    result = svc.evaluate(decision(), svc.current_context("request-123"), trigger="test")
    svc.confirm(result.requestId)
    store.record_ack({"requestId": result.requestId, "accepted": True, "actualState": "OPEN", "reason": "started", "remainingSeconds": 29})
    store.update_decision_ack({"requestId": result.requestId, "accepted": True, "actualState": "OPEN", "reason": "started", "remainingSeconds": 29})
    latest = store.get_decision(result.requestId)
    assert latest.executed
    assert latest.status == "executed"
    assert store.latest_actuator_state()["state"] == "OPEN"


def test_close_ack_does_not_count_watering_twice(tmp_path):
    svc, store = service(tmp_path)
    result = svc.evaluate(decision(), svc.current_context("request-123"), trigger="test")
    svc.confirm(result.requestId)
    open_ack = {"requestId": result.requestId, "accepted": True, "actualState": "OPEN", "reason": "started"}
    close_ack = {"requestId": result.requestId, "accepted": True, "actualState": "CLOSED", "reason": "duration_timeout_closed"}
    store.record_ack(open_ack)
    store.record_ack(close_ack)
    store.update_decision_ack(close_ack)
    assert store.actuator_summary(datetime.now(timezone.utc) - timedelta(hours=1))["wateringCount"] == 1
    assert store.get_decision(result.requestId).status == "completed"
    assert svc.last_device_state["state"] == "CLOSED"


def test_expired_queued_command_is_not_sent(tmp_path):
    _, store = service(tmp_path)
    command = decision("request-old", expires_delta=-1).model_dump(mode="json")
    command["ttlSeconds"] = 30
    assert store.enqueue_command(command)
    assert store.claim_pending_commands() == []


def test_confirmation_rechecks_expiry_and_current_soil(tmp_path):
    svc, store = service(tmp_path)
    expiring = svc.evaluate(
        decision("request-expiring", expires_delta=1),
        svc.current_context("request-expiring"),
        trigger="test",
    )
    stored = expiring.model_copy(update={"expiresAt": datetime.now(timezone.utc) - timedelta(seconds=1)})
    store.save_decision(stored, {}, None)
    expired = svc.confirm(stored.requestId)
    assert expired.status == "rejected_on_confirmation"
    assert any("expired" in reason for reason in expired.safetyReasons)

    wet_result = svc.evaluate(
        decision("request-wet"), svc.current_context("request-wet"), trigger="test"
    )
    store.save_live_snapshot(snapshot(moisture=80), datetime.now(timezone.utc))
    wet = svc.confirm(wet_result.requestId)
    assert wet.status == "rejected_on_confirmation"
    assert any("above target" in reason for reason in wet.safetyReasons)


def test_confirmation_rechecks_valve_state_and_daily_limit(tmp_path):
    svc, store = service(tmp_path)
    valve_result = svc.evaluate(
        decision("request-valve"), svc.current_context("request-valve"), trigger="test"
    )
    assert store.enqueue_command({
        "schemaVersion": "1.0", "requestId": "existing-open", "action": "NO_OP",
        "durationSeconds": None, "reasonCode": "TEST", "reason": "test",
        "confidence": 1, "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "ttlSeconds": 30,
    })
    store.record_ack({
        "requestId": "existing-open", "accepted": True,
        "actualState": "OPEN", "reason": "already running",
    })
    valve_open = svc.confirm(valve_result.requestId)
    assert any("already open" in reason for reason in valve_open.safetyReasons)

    svc2, store2 = service(tmp_path / "daily")
    daily_result = svc2.evaluate(
        decision("request-daily"), svc2.current_context("request-daily"), trigger="test"
    )
    store2.record_actuator_event(
        "concurrent-watering", IrrigationAction.START_WATERING,
        svc2.settings.max_daily_watering_seconds, {"accepted": True},
    )
    daily_limited = svc2.confirm(daily_result.requestId)
    assert any("daily watering limit" in reason for reason in daily_limited.safetyReasons)


def test_request_mismatch_low_confidence_and_daily_limit_are_rejected(tmp_path):
    svc, store = service(tmp_path)
    mismatch = svc.evaluate(
        decision("model-request"), svc.current_context("local-request"), trigger="test"
    )
    assert mismatch.status == "rejected"
    assert any("does not match" in reason for reason in mismatch.safetyReasons)

    low = decision("request-low").model_copy(update={"confidence": 0.4})
    low_result = svc.evaluate(low, svc.current_context("request-low"), trigger="test")
    assert any("confidence" in reason for reason in low_result.safetyReasons)

    store.record_actuator_event(
        "past-watering", IrrigationAction.START_WATERING,
        svc.settings.max_daily_watering_seconds, {"accepted": True},
    )
    limited = svc.evaluate(
        decision("request-limit"), svc.current_context("request-limit"), trigger="test"
    )
    assert any("daily watering limit" in reason for reason in limited.safetyReasons)


class StartWateringGateway:
    configured = True

    def irrigation_decision(self, context):
        proposed = decision(context.requestId)
        return proposed, CloudCall(
            content=proposed.model_dump_json(),
            latency_ms=12,
            prompt_tokens=100,
            completion_tokens=40,
        )


def ready_forecast() -> ForecastResponse:
    now = datetime.now(timezone.utc)
    return ForecastResponse(
        status="ok", generatedAt=now, requiredSamples=288, availableSamples=288,
        forecast=[ForecastPoint(timestamp=now + timedelta(minutes=5), et0Mm=0.1, soilMoisturePercent=19.5)],
    )


def test_auto_irrigation_requires_forecast_then_queues_after_local_gates(tmp_path):
    svc, store = service(tmp_path, enabled=True)
    svc.settings = replace(
        svc.settings,
        auto_irrigation_enabled=True,
        auto_irrigation_min_confidence=0.8,
        auto_irrigation_require_forecast_ready=True,
    )
    svc.gateway = StartWateringGateway()

    held = svc.analyze(trigger="periodic")
    assert held.status == "awaiting_confirmation"
    assert any("complete local forecast" in reason for reason in held.safetyReasons)
    assert store.claim_pending_commands() == []

    store.save_forecast(ready_forecast())
    queued = svc.analyze(trigger="periodic")
    assert queued.status == "auto_confirmed_waiting_device"
    assert queued.autoConfirmed
    assert not queued.humanConfirmed
    command = store.claim_pending_commands()[0]
    assert command["action"] == "START_WATERING"
    assert command["reason"].startswith("local-auto-approved:")


def test_auto_irrigation_never_bypasses_fresh_sensor_gate(tmp_path):
    svc, store = service(tmp_path, enabled=True)
    svc.settings = replace(svc.settings, auto_irrigation_enabled=True)
    svc.gateway = StartWateringGateway()
    store.save_forecast(ready_forecast())
    store.save_live_snapshot(snapshot(), datetime.now(timezone.utc) - timedelta(seconds=svc.settings.data_stale_seconds + 1))

    held = svc.analyze(trigger="periodic")

    assert held.status == "rejected"
    assert any("incomplete" in reason for reason in held.safetyReasons)
    assert store.claim_pending_commands() == []


class RecordingSerial:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data


def test_cloud_to_serial_to_valve_ack_complete_loop(tmp_path):
    """Exercise the complete cloud suggestion -> local safety -> ESP32 ACK chain."""
    svc, store = service(tmp_path, enabled=True)
    svc.gateway = StartWateringGateway()

    suggested = svc.analyze(trigger="end-to-end-test")
    assert suggested.status == "awaiting_confirmation"
    assert suggested.finalAction == IrrigationAction.START_WATERING
    assert store.latest_llm_call()["response"]["action"] == "START_WATERING"
    saved_context = store.latest_llm_call()["context"]
    assert "wateringLast7Days" in saved_context["constraints"]
    assert "recentReviewedDecisions" in saved_context["constraints"]

    confirmed = svc.confirm(suggested.requestId)
    assert confirmed.status == "confirmed_waiting_device"

    serial = RecordingSerial()
    _send_pending_commands(serial, store)
    wire_line = serial.data.decode("utf-8").strip()
    assert wire_line.startswith("@COMMAND ")
    command = json.loads(wire_line.removeprefix("@COMMAND "))
    assert command["requestId"] == suggested.requestId
    assert command["action"] == "START_WATERING"

    open_ack = json.dumps({
        "requestId": suggested.requestId,
        "accepted": True,
        "actualState": "OPEN",
        "reason": "started",
        "remainingSeconds": 29,
    }, separators=(",", ":"))
    assert _handle_ack_line("@ACK " + open_ack, store)
    assert store.get_decision(suggested.requestId).status == "executed"
    assert svc.last_device_state["state"] == "OPEN"

    close_ack = json.dumps({
        "requestId": suggested.requestId,
        "accepted": True,
        "actualState": "CLOSED",
        "reason": "duration_timeout_closed",
        "remainingSeconds": 0,
    }, separators=(",", ":"))
    assert _handle_ack_line("@ACK " + close_ack, store)
    assert store.get_decision(suggested.requestId).status == "completed"
    assert svc.last_device_state["state"] == "CLOSED"
    summary = store.actuator_summary(datetime.now(timezone.utc) - timedelta(hours=1))
    assert summary["wateringCount"] == 1
    assert summary["wateringSeconds"] == 30
