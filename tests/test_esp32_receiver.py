from dual_forecast.esp32_receiver import esp32_message_to_snapshot, result_to_display_command


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


def test_prediction_status_without_forecast_keeps_the_display_protocol_valid():
    assert result_to_display_command(
        {"status": "insufficient_data", "availableSamples": 239, "requiredSamples": 288}
    ) == "DISPLAY status=insufficient_data samples=239/288 et0=0.000 soil=0.0\n"
