"""Bridge ESP32 TCP telemetry into the local dual-forecast HTTP service."""

from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any


def esp32_message_to_snapshot(
    message: dict[str, Any], *, fallback_air_pressure_hpa: int | None = None
) -> dict[str, Any]:
    """Convert all_sensors.ino JSON into the /v1/snapshots schema."""
    wind = message["wind"]
    air = message["air"]
    soil = message["soil"]
    solar = message["solar"]
    solar_1 = solar["sensor_1"]
    solar_2 = solar["sensor_2"]
    air_pressure_hpa = int(message.get("air_pressure_hpa", 0))
    if air_pressure_hpa <= 0 and fallback_air_pressure_hpa is not None:
        air_pressure_hpa = fallback_air_pressure_hpa

    return {
        "uptimeMs": int(message["uptime_ms"]),
        "windOk": bool(wind["ok"]),
        "windVoltage": float(wind["voltage_v"]),
        "windSpeedMs": float(wind["speed_m_s"]),
        "airOk": bool(air["ok"]),
        "air": {
            "temperatureC": float(air["temperature_c"]),
            "humidityPercent": float(air["humidity_pct"]),
        },
        "soilOk": bool(soil["ok"]),
        "soil": {
            "temperatureC": float(soil["temperature_c"]),
            "moisturePercent": float(soil["moisture_pct"]),
        },
        "solar1Ok": bool(solar_1["ok"]),
        "solarRadiation1Wm2": int(solar_1["radiation_w_m2"]),
        "solar2Ok": bool(solar_2["ok"]),
        "solarRadiation2Wm2": int(solar_2["radiation_w_m2"]),
        "AirPressure": air_pressure_hpa,
    }


def submit_snapshot(api_url: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Submit one normalized snapshot to the running prediction service."""
    payload = json.dumps(snapshot).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def receive_esp32(args: argparse.Namespace) -> None:
    """Keep one TCP connection to ESP32 and forward sampled messages to FastAPI."""
    last_submit_at = 0.0

    while True:
        try:
            print(f"Connecting to ESP32 TCP server at {args.esp_host}:{args.esp_port} ...")
            with socket.create_connection((args.esp_host, args.esp_port), timeout=10) as connection:
                connection.settimeout(None)
                print("Connected. Receiving ESP32 telemetry.")

                with connection.makefile("r", encoding="utf-8", newline="\n") as stream:
                    for line in stream:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            # all_sensors.ino emits a plain-text greeting at connection time.
                            print(f"ESP32: {line}")
                            continue

                        now = time.monotonic()
                        if now - last_submit_at < args.min_interval_seconds:
                            continue

                        try:
                            raw_pressure_hpa = int(message.get("air_pressure_hpa", 0))
                            snapshot = esp32_message_to_snapshot(
                                message,
                                fallback_air_pressure_hpa=args.fallback_air_pressure_hpa,
                            )
                            result = submit_snapshot(args.api_url, snapshot)
                        except (KeyError, TypeError, ValueError) as exc:
                            print(f"Ignored malformed ESP32 telemetry: {exc}")
                            continue
                        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                            print(f"Prediction service unavailable: {exc}")
                            continue

                        last_submit_at = now
                        if raw_pressure_hpa <= 0:
                            print(
                                "AirPressure is 0; using fallback "
                                f"{args.fallback_air_pressure_hpa} hPa until the sensor is installed."
                            )
                        print(
                            "Submitted snapshot: "
                            f"status={result.get('status')}, "
                            f"samples={result.get('availableSamples')}/{result.get('requiredSamples')}"
                        )
        except OSError as exc:
            print(f"ESP32 connection unavailable: {exc}. Retrying in 3 seconds...")
            time.sleep(3)


def add_receiver_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "receive-esp32",
        help="receive ESP32 TCP telemetry and submit it to the local prediction service",
    )
    parser.add_argument("--esp-host", default="192.168.4.1", help="ESP32 TCP server address")
    parser.add_argument("--esp-port", type=int, default=3333, help="ESP32 TCP server port")
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000/v1/snapshots",
        help="local dual-forecast snapshot endpoint",
    )
    parser.add_argument(
        "--min-interval-seconds",
        type=float,
        default=300.0,
        help="minimum interval between submitted snapshots; defaults to the model's five-minute cadence",
    )
    parser.add_argument(
        "--fallback-air-pressure-hpa",
        type=int,
        default=1013,
        help="pressure used when ESP32 reports 0 before its pressure sensor is installed",
    )
    parser.set_defaults(func=receive_esp32)
