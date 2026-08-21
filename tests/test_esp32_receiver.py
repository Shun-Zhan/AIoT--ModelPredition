import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dual_forecast.esp32_receiver import (
    _handle_device_result_line,
    _handle_ack_line,
    _handle_config_ack_line,
    _send_pending_commands,
    _send_pending_configs,
    esp32_message_to_snapshot,
    parse_discovery_announcement,
    parse_device_result_line,
    resolve_mdns_fallback_endpoint,
    result_to_display_command,
    snapshot_is_complete_for_prediction,
)
from dual_forecast.storage import Store
from dual_forecast.schemas import DeviceCloudResult, DeviceForecast, DeviceIrrigationState, DeviceUiAck


def test_firmware_tcp_control_buffer_accepts_full_command_envelope():
    firmware = (
        Path(__file__).resolve().parents[1]
        / "firmware"
        / "esp32_s3_all_sensors"
        / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")

    assert "HOST_CONTROL_LINE_CAPACITY = 1024" in firmware
    assert firmware.count("line[HOST_CONTROL_LINE_CAPACITY]") >= 2
    assert "line[192]" not in firmware


def test_esp32_message_maps_to_service_snapshot():
    message = {
        "uptime_ms": 300000,
        "wind": {"ok": True, "voltage_v": 1.2, "speed_m_s": 2.3},
        "air_pressure_hpa": 1013,
        "air": {"ok": True, "temperature_c": 25.1, "humidity_pct": 63.2},
        "soil": {"ok": True, "temperature_c": 22.4, "moisture_pct": 51.7},
        "solar": {
            "sensor_1": {"ok": True, "radiation_w_m2": 410},
            "sensor_2": {"ok": True, "radiation_w_m2": 430},
        },
    }

    snapshot = esp32_message_to_snapshot(message)

    assert snapshot == {
        "uptimeMs": 300000,
        "windOk": True,
        "windVoltage": 1.2,
        "windSpeedMs": 2.3,
        "airOk": True,
        "air": {"temperatureC": 25.1, "humidityPercent": 63.2},
        "soilOk": True,
        "soil": {"temperatureC": 22.4, "moisturePercent": 51.7},
        "solar1Ok": True,
        "solarRadiation1Wm2": 410,
        "solar2Ok": True,
        "solarRadiation2Wm2": 430,
        "AirPressure": 1013,
    }


def test_zero_pressure_uses_configured_fallback():
    message = {
        "uptime_ms": 1,
        "wind": {"ok": True, "voltage_v": 0.0, "speed_m_s": 0.0},
        "air_pressure_hpa": 0,
        "air": {"ok": True, "temperature_c": 20.0, "humidity_pct": 60.0},
        "soil": {"ok": True, "temperature_c": 20.0, "moisture_pct": 50.0},
        "solar": {
            "sensor_1": {"ok": True, "radiation_w_m2": 0},
            "sensor_2": {"ok": True, "radiation_w_m2": 0},
        },
    }

    snapshot = esp32_message_to_snapshot(message, fallback_air_pressure_hpa=1013)

    assert snapshot["AirPressure"] == 1013


def test_flow_meter_zero_is_valid_and_is_normalized():
    message = {
        "uptime_ms": 1,
        "wind": {"ok": True, "voltage_v": 0.0, "speed_m_s": 0.0},
        "air_pressure_hpa": 1013,
        "air": {"ok": True, "temperature_c": 20.0, "humidity_pct": 60.0},
        "soil": {"ok": True, "temperature_c": 20.0, "moisture_pct": 50.0},
        "solar": {
            "sensor_1": {"ok": True, "radiation_w_m2": 0},
            "sensor_2": {"ok": True, "radiation_w_m2": 0},
        },
        "flow": {
            "ok": True,
            "signal_pin": 12,
            "zero_is_valid": True,
            "pulse_count": 0,
            "frequency_hz": 0,
            "flow_rate_lpm": 0,
            "total_liters": 0,
        },
    }

    snapshot = esp32_message_to_snapshot(message)

    assert snapshot["flow"] == {
        "ok": True,
        "signalPin": 12,
        "zeroIsValid": True,
        "pulseCount": 0,
        "frequencyHz": 0.0,
        "flowRateLpm": 0.0,
        "totalLiters": 0.0,
    }


def test_prediction_requires_incoming_solar_sensor_2_not_only_reflection():
    base = {
        "windOk": True, "airOk": True, "soilOk": True,
        "solar1Ok": True, "solar2Ok": False, "AirPressure": 1013,
    }
    assert not snapshot_is_complete_for_prediction(base)
    base["solar2Ok"] = True
    assert snapshot_is_complete_for_prediction(base)


def test_esp32_edge_prediction_is_forwarded_to_live_dashboard():
    message = {
        "uptime_ms": 300000,
        "wind": {"ok": True, "voltage_v": 1.2, "speed_m_s": 2.3},
        "air_pressure_hpa": 1013,
        "air": {"ok": True, "temperature_c": 25.1, "humidity_pct": 63.2},
        "soil": {"ok": True, "temperature_c": 22.4, "moisture_pct": 36.7},
        "solar": {
            "sensor_1": {"ok": True, "radiation_w_m2": 410},
            "sensor_2": {"ok": True, "radiation_w_m2": 430},
        },
        "edge_prediction": {
            "valid": True,
            "mode": "edge_fallback",
            "predicted_soil_moisture_30m_pct": 36.4,
            "drying_rate_pct_per_h": 0.58,
            "risk_level": "ATTENTION",
            "reason": "rapid_drying",
            "updated_uptime_ms": 300000,
        },
    }

    snapshot = esp32_message_to_snapshot(message)

    assert snapshot["edgePrediction"] == {
        "valid": True,
        "mode": "edge_fallback",
        "predictedSoilMoisture30mPercent": 36.4,
        "dryingRatePercentPerHour": 0.58,
        "riskLevel": "ATTENTION",
        "reason": "rapid_drying",
        "updatedUptimeMs": 300000,
    }


def test_esp32_cloud_runtime_state_is_forwarded_without_credentials():
    message = {
        "uptime_ms": 300000,
        "wind": {"ok": True, "voltage_v": 1.2, "speed_m_s": 2.3},
        "air_pressure_hpa": 1013,
        "air": {"ok": True, "temperature_c": 25.1, "humidity_pct": 63.2},
        "soil": {"ok": True, "temperature_c": 22.4, "moisture_pct": 36.7},
        "solar": {
            "sensor_1": {"ok": True, "radiation_w_m2": 410},
            "sensor_2": {"ok": True, "radiation_w_m2": 430},
        },
        "cloud": {
            "initialized": True,
            "enabled": True,
            "api_key_configured": True,
            "request_pending": True,
        },
    }

    snapshot = esp32_message_to_snapshot(message)

    assert snapshot["cloudRuntime"] == {
        "initialized": True,
        "enabled": True,
        "apiKeyConfigured": True,
        "requestPending": True,
    }
    assert "apiKey" not in snapshot["cloudRuntime"]


def test_esp32_performance_diagnostics_are_forwarded():
    message = {
        "uptime_ms": 300000,
        "wind": {"ok": True, "voltage_v": 0.0, "speed_m_s": 0.0},
        "air_pressure_hpa": 1013,
        "air": {"ok": True, "temperature_c": 20.0, "humidity_pct": 50.0},
        "soil": {"ok": True, "temperature_c": 20.0, "moisture_pct": 50.0},
        "solar": {
            "sensor_1": {"ok": True, "radiation_w_m2": 0},
            "sensor_2": {"ok": True, "radiation_w_m2": 0},
        },
        "performance": {
            "chip_temperature_c": 48.5,
            "heap_free_bytes": 220000,
            "heap_min_free_bytes": 180000,
            "heap_size_bytes": 320000,
            "heap_used_percent": 31.25,
            "cpu_freq_mhz": 240,
            "flash_size_bytes": 8388608,
            "sketch_size_bytes": 1000000,
            "free_sketch_bytes": 2000000,
            "wifi": {"connected": True, "rssi_dbm": -55, "ip": "192.168.1.20"},
        },
    }

    snapshot = esp32_message_to_snapshot(message)

    assert snapshot["performance"]["chipTemperatureC"] == 48.5
    assert snapshot["performance"]["heapUsedPercent"] == 31.25
    assert snapshot["performance"]["wifiIp"] == "192.168.1.20"


def test_incomplete_packet_is_not_used_for_prediction():
    snapshot = {
        "windOk": True,
        "airOk": True,
        "soilOk": True,
        "solar1Ok": True,
        "solar2Ok": False,
        "AirPressure": 1013,
    }
    # Sensor 1 is reflected shortwave only.  It cannot substitute for the
    # incoming Rs↓ energy measured by sensor 2 in an ET₀ model sample.
    assert not snapshot_is_complete_for_prediction(snapshot)

    snapshot["soilOk"] = False
    assert not snapshot_is_complete_for_prediction(snapshot)


def test_prediction_result_is_encoded_for_the_esp32_display():
    result = {
        "status": "ok",
        "availableSamples": 288,
        "requiredSamples": 288,
        "forecast": [
            {"et0Mm": 0.01, "soilMoisturePercent": 48.2},
            {"et0Mm": 0.02, "soilMoisturePercent": 48.0},
        ],
    }

    assert result_to_display_command(result) == (
        "DISPLAY status=ok samples=288/288 et0=0.030 soil=48.0\n"
    )


def test_warming_up_status_without_forecast_keeps_the_display_protocol_valid():
    assert result_to_display_command(
        {"status": "warming_up", "availableSamples": 239, "requiredSamples": 288}
    ) == "DISPLAY status=warming_up samples=239/288 et0=0.000 soil=0.0\n"


def test_v2_device_result_prefixes_are_parsed_and_cached(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    lines = [
        ('@FORECAST {"schemaVersion":"2.0","generatedAt":"2026-08-11T08:00:00Z",'
         '"status":"ok","nextHourEt0Mm":0.24,"soilMoistureInOneHour":42.5}',
         "forecast", DeviceForecast),
        ('@IRRIGATION_STATE {"schemaVersion":"2.0","updatedAt":"2026-08-11T08:00:01Z",'
         '"state":"CLOSED","action":"NO_OP"}',
         "irrigation_state", DeviceIrrigationState),
        ('@CLOUD_RESULT {"schemaVersion":"2.0","updatedAt":"2026-08-11T08:00:02Z",'
         '"status":"offline","finalAction":"NO_OP","reason":"network unavailable"}',
         "cloud_result", DeviceCloudResult),
        ('@UI_ACK {"schemaVersion":"2.0","updatedAt":"2026-08-11T08:00:03Z",'
         '"requestId":"ui-request-1","accepted":true}',
         "ui_ack", DeviceUiAck),
    ]

    for line, result_type, model_type in lines:
        parsed = parse_device_result_line(line)
        assert parsed is not None
        assert parsed[0] == result_type
        assert isinstance(parsed[1], model_type)
        assert _handle_device_result_line(line, store)

    cached = store.latest_device_results()
    assert cached["forecast"]["schemaVersion"] == "2.0"
    assert cached["forecast"]["soilMoistureInOneHour"] == 42.5
    assert cached["irrigationState"]["state"] == "CLOSED"
    assert cached["cloudResult"]["finalAction"] == "NO_OP"
    assert cached["uiAck"]["accepted"] is True


def test_v2_irrigation_state_replaces_open_with_automatic_closed_state(tmp_path):
    store = Store(tmp_path / "db.sqlite")

    assert _handle_device_result_line(
        '@IRRIGATION_STATE {"schemaVersion":"2.0","requestId":"voice-1",'
        '"state":"OPEN","valveState":"OPEN","action":"NO_OP",'
        '"remainingSeconds":5}',
        store,
    )
    assert _handle_device_result_line(
        '@IRRIGATION_STATE {"schemaVersion":"2.0","requestId":"voice-1",'
        '"state":"CLOSED","valveState":"CLOSED","action":"NO_OP",'
        '"remainingSeconds":0}',
        store,
    )

    state = store.latest_device_results()["irrigationState"]
    assert state["state"] == "CLOSED"
    assert state["valveState"] == "CLOSED"
    assert state["remainingSeconds"] == 0


def test_v2_irrigation_state_parses_volume_closed_loop_fields(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    line = (
        '@IRRIGATION_STATE {"schemaVersion":"2.0","requestId":"volume-1",'
        '"state":"OPEN","action":"START_WATERING","reasonCode":"volume_reached_closed",'
        '"targetLiters":12.5,"deliveredLiters":12.5,"remainingLiters":0.0,'
        '"flowRateLpm":0.0,"flowPulseCount":450,"flowFault":false,'
        '"flowFaultReason":"","wateringControlMode":"volume_closed_loop"}'
    )

    parsed = parse_device_result_line(line)
    assert parsed is not None
    result_type, payload = parsed
    assert result_type == "irrigation_state"
    assert isinstance(payload, DeviceIrrigationState)
    assert payload.targetLiters == 12.5
    assert payload.deliveredLiters == 12.5
    assert payload.remainingLiters == 0.0
    assert payload.flowRateLpm == 0.0
    assert payload.flowPulseCount == 450
    assert payload.flowFault is False
    assert payload.flowFaultReason == ""
    assert payload.wateringControlMode == "volume_closed_loop"
    assert payload.reasonCode == "volume_reached_closed"

    assert _handle_device_result_line(line, store)
    cached = store.latest_device_results()["irrigationState"]
    assert cached["targetLiters"] == 12.5
    assert cached["remainingLiters"] == 0.0
    assert cached["wateringControlMode"] == "volume_closed_loop"
    assert cached["flowFault"] is False


def test_v2_irrigation_state_flow_fault_fields_round_trip(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    line = (
        '@IRRIGATION_STATE {"schemaVersion":"2.0","requestId":"volume-2",'
        '"state":"CLOSED","action":"NO_OP","reasonCode":"flow_fault",'
        '"targetLiters":8.0,"deliveredLiters":3.2,"remainingLiters":4.8,'
        '"flowRateLpm":0.0,"flowPulseCount":128,"flowFault":true,'
        '"flowFaultReason":"flow_fault","wateringControlMode":"volume_closed_loop"}'
    )

    parsed = parse_device_result_line(line)
    assert parsed is not None
    payload = parsed[1]
    assert payload.flowFault is True
    assert payload.flowFaultReason == "flow_fault"
    assert payload.reasonCode == "flow_fault"
    assert payload.deliveredLiters == 3.2
    assert payload.remainingLiters == 4.8
    assert payload.flowPulseCount == 128

    assert _handle_device_result_line(line, store)
    cached = store.latest_device_results()["irrigationState"]
    assert cached["flowFault"] is True
    assert cached["flowFaultReason"] == "flow_fault"
    assert cached["reasonCode"] == "flow_fault"


def test_v2_irrigation_state_rejects_negative_volume_fields():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeviceIrrigationState.model_validate({
            "schemaVersion": "2.0", "state": "OPEN",
            "targetLiters": -1.0, "wateringControlMode": "volume_closed_loop",
        })


def test_v2_ui_ack_updates_the_matching_command_queue(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.enqueue_command({
        "schemaVersion": "2.0", "requestId": "ui-debug-pulse-1",
        "action": "DEBUG_VALVE_PULSE", "durationSeconds": 5,
    })
    store.mark_command_sent("ui-debug-pulse-1")

    assert _handle_device_result_line(
        '@UI_ACK {"schemaVersion":"2.0","requestId":"ui-debug-pulse-1",'
        '"accepted":false,"action":"DEBUG_VALVE_PULSE",'
        '"reason":"daily_limit","actualState":"CLOSED"}',
        store,
    )

    status = store.command_status("ui-debug-pulse-1")
    assert status["status"] == "rejected"
    assert status["ack"]["reason"] == "daily_limit"
    assert status["ack"]["actualState"] == "CLOSED"


def test_malformed_v2_device_result_is_consumed_without_cache_write(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert _handle_device_result_line('@FORECAST {"schemaVersion":"1.0"}', store)
    assert store.latest_device_forecast() is None


def test_telemetry_receiver_updates_live_only_and_does_not_submit_snapshot(monkeypatch):
    from argparse import Namespace
    from dual_forecast.esp32_receiver import _ReceiverState, _handle_telemetry_message

    calls = []

    def submit(url, snapshot):
        calls.append((url, snapshot))
        return {"status": "ok"}

    monkeypatch.setattr("dual_forecast.esp32_receiver.submit_snapshot", submit)
    args = Namespace(
        live_api_url="http://127.0.0.1:8000/v1/telemetry/live",
        api_url="http://127.0.0.1:8000/v1/snapshots",
        fallback_air_pressure_hpa=1013,
        min_interval_seconds=0.1,
        fast_test=False,
        fast_test_interval_seconds=0.1,
    )
    message = {
        "uptime_ms": 1,
        "wind": {"ok": True, "voltage_v": 1.0, "speed_m_s": 1.0},
        "air_pressure_hpa": 1013,
        "air": {"ok": True, "temperature_c": 20.0, "humidity_pct": 50.0},
        "soil": {"ok": True, "temperature_c": 20.0, "moisture_pct": 50.0},
        "solar": {
            "sensor_1": {"ok": True, "radiation_w_m2": 100},
            "sensor_2": {"ok": True, "radiation_w_m2": 200},
        },
    }

    _handle_telemetry_message(message, args, _ReceiverState())
    assert len(calls) == 1
    assert calls[0][0] == args.live_api_url


def test_ui_transport_marker_emits_ui_command_prefix(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path / "db.sqlite")
    command = {
        "schemaVersion": "1.0", "requestId": "ui-request-123", "action": "STOP_WATERING",
        "durationSeconds": None, "reasonCode": "UI", "reason": "ui action", "confidence": 1,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), "ttlSeconds": 30,
    }
    assert store.enqueue_command(command)
    assert store.mark_command_for_ui_transport(command["requestId"])
    serial = FakeSerial()
    _send_pending_commands(serial, store)
    assert serial.data.startswith(b"@UI_COMMAND {")


def test_auto_discovery_uses_the_current_udp_sender_ip():
    endpoint = parse_discovery_announcement(
        b'AIOT_DISCOVERY {"service":"aiot-esp32","port":3333}\n',
        "172.20.10.27",
    )

    assert endpoint is not None
    assert endpoint.host == "172.20.10.27"
    assert endpoint.port == 3333


def test_auto_discovery_rejects_unrelated_or_invalid_packets():
    assert parse_discovery_announcement(b"other device", "192.168.1.10") is None
    assert parse_discovery_announcement(
        b'AIOT_DISCOVERY {"service":"another-device","port":3333}', "192.168.1.10"
    ) is None
    assert parse_discovery_announcement(
        b'AIOT_DISCOVERY {"service":"aiot-esp32","port":0}', "192.168.1.10"
    ) is None


def test_mdns_fallback_uses_resolved_esp32_address(monkeypatch):
    monkeypatch.setattr("dual_forecast.esp32_receiver.socket.gethostbyname", lambda _: "172.20.10.2")
    assert resolve_mdns_fallback_endpoint(3333).host == "172.20.10.2"


def test_mdns_fallback_is_optional_when_name_is_unavailable(monkeypatch):
    def fail(_: str) -> str:
        raise OSError("mDNS unavailable")

    monkeypatch.setattr("dual_forecast.esp32_receiver.socket.gethostbyname", fail)
    assert resolve_mdns_fallback_endpoint(3333) is None


class FakeSerial:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data


class FakeTcpWriter(FakeSerial):
    max_control_line_bytes = 191


class FailingSerial:
    def write(self, data):
        raise OSError("USB disconnected")


def test_only_prefixed_ack_is_parsed(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert not _handle_ack_line('{"requestId":"normal-log"}', store)
    assert _handle_ack_line('@ACK {"requestId":"req-12345","accepted":true,"actualState":"OPEN"}', store)


def test_pending_command_uses_command_prefix(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    from datetime import datetime, timedelta, timezone
    assert store.enqueue_command({
        "schemaVersion": "1.0", "requestId": "request-command", "action": "NO_OP",
        "durationSeconds": None, "reasonCode": "TEST", "reason": "test", "confidence": 1,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), "ttlSeconds": 30,
    })
    serial = FakeSerial()
    _send_pending_commands(serial, store)
    assert serial.data.startswith(b"@COMMAND {")


def test_wifi_command_is_compacted_for_deployed_192_byte_firmware_buffer(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path / "db.sqlite")
    request_id = "analysis-13a1d447-2192-4947-b933-b8671a7176ec"
    assert store.enqueue_command({
        "schemaVersion": "1.0",
        "requestId": request_id,
        "action": "START_WATERING",
        "durationSeconds": 60,
        "reasonCode": "MANUAL_ACTUATOR_DEBUG",
        "reason": "long audit reason retained only in SQLite",
        "confidence": 1,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "ttlSeconds": 30,
    })
    writer = FakeTcpWriter()

    _send_pending_commands(writer, store)

    wire = writer.data.decode().rstrip("\n")
    assert len(wire.encode()) <= writer.max_control_line_bytes
    payload = json.loads(wire.removeprefix("@COMMAND "))
    assert payload["requestId"] == request_id
    assert payload["action"] == "START_WATERING"
    assert payload["durationSeconds"] == 60
    assert payload["ttlSeconds"] == 30
    assert "reason" not in payload
    assert "confidence" not in payload


def test_debug_valve_pulse_compaction_keeps_fixed_duration(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path / "db.sqlite")
    request_id = "13a1d447-2192-4947-b933-b8671a7176ec"
    assert store.enqueue_command({
        "schemaVersion": "2.0",
        "requestId": request_id,
        "action": "DEBUG_VALVE_PULSE",
        "durationSeconds": 5,
        "reasonCode": "UI",
        "reason": "long audit reason retained only in SQLite",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "ttlSeconds": 30,
        "transport": "UI_COMMAND",
    })
    writer = FakeTcpWriter()

    _send_pending_commands(writer, store)

    wire = writer.data.decode().rstrip("\n")
    assert len(wire.encode()) <= writer.max_control_line_bytes
    payload = json.loads(wire.removeprefix("@UI_COMMAND "))
    assert payload["action"] == "DEBUG_VALVE_PULSE"
    assert payload["durationSeconds"] == 5


def test_confirm_watering_compaction_keeps_cloud_decision_binding(tmp_path):
    store = Store(tmp_path / "commands.sqlite")
    command = {
        "schemaVersion": "2.0",
        "requestId": "confirm-command-12345678",
        "action": "CONFIRM_WATERING",
        "sourceRequestId": "cloud-decision-12345678",
        "durationSeconds": 27,
        "reasonCode": "UI",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "ttlSeconds": 30,
        "transport": "UI_COMMAND",
        "extraLongField": "x" * 300,
    }
    assert store.enqueue_command(command)
    connection = FakeTcpWriter()
    _send_pending_commands(connection, store)
    payload = json.loads(connection.data.decode().removeprefix("@UI_COMMAND "))
    assert payload["durationSeconds"] == 27
    assert payload["sourceRequestId"] == "cloud-decision-12345678"


def test_failed_serial_write_keeps_command_pending(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    from datetime import datetime, timedelta, timezone
    assert store.enqueue_command({
        "schemaVersion": "1.0", "requestId": "request-retry", "action": "NO_OP",
        "durationSeconds": None, "reasonCode": "TEST", "reason": "test", "confidence": 1,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), "ttlSeconds": 30,
    })
    try:
        _send_pending_commands(FailingSerial(), store)
    except OSError:
        pass
    assert store.pending_commands()[0]["requestId"] == "request-retry"


def test_confirm_watering_open_ack_records_actuator_event(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path / "commands.sqlite")
    command = {
        "schemaVersion": "2.0", "requestId": "confirm-event",
        "action": "CONFIRM_WATERING", "sourceRequestId": "cloud-event",
        "durationSeconds": 16, "reasonCode": "UI",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "ttlSeconds": 30, "transport": "UI_COMMAND",
    }
    assert store.enqueue_command(command)

    store.record_ack({
        "requestId": "confirm-event", "accepted": True,
        "action": "CONFIRM_WATERING", "actualState": "OPEN", "reason": "started",
    })

    summary = store.actuator_summary(datetime.now(timezone.utc) - timedelta(minutes=1))
    assert summary["wateringCount"] == 1
    assert summary["wateringSeconds"] == 16


def test_config_protocol_remains_separate_from_valve_commands(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    config = store.enqueue_sampling_config("NIGHT_ECO", 600000)
    serial = FakeSerial()
    _send_pending_configs(serial, store)
    assert serial.data.startswith(b"@CONFIG ")
    assert not serial.data.startswith(b"@COMMAND ")
    assert _handle_config_ack_line(
        '@CONFIG_ACK {"requestId":"%s","accepted":false,"samplingMode":"DEBUG","readIntervalMs":2000,"reason":"valve_open_requires_fast_sampling"}' % config["requestId"],
        store,
    )
    assert store.sampling_config_status()["status"] == "rejected"
