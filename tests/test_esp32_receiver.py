from dual_forecast.esp32_receiver import (
    _handle_ack_line,
    _handle_config_ack_line,
    _send_pending_commands,
    _send_pending_configs,
    esp32_message_to_snapshot,
    parse_discovery_announcement,
    resolve_mdns_fallback_endpoint,
    result_to_display_command,
    snapshot_is_complete_for_prediction,
)
from dual_forecast.storage import Store


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


def test_incomplete_packet_is_not_used_for_prediction():
    snapshot = {
        "windOk": True,
        "airOk": True,
        "soilOk": True,
        "solar1Ok": True,
        "solar2Ok": False,
        "AirPressure": 1013,
    }
    assert snapshot_is_complete_for_prediction(snapshot)

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
