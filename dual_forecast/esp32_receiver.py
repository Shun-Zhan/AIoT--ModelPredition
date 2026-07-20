"""Bridge ESP32 TCP telemetry into the local dual-forecast HTTP service."""

from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .storage import Store


# Telemetry is always posted to a service on this same computer.  Do not let a
# system HTTP proxy (common on campus/company networks) route 127.0.0.1 through
# a proxy server, which can otherwise return 502 even though FastAPI is healthy.
_LOCAL_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def snapshot_is_complete_for_prediction(snapshot: dict[str, Any]) -> bool:
    """Accept only a complete environmental sample into the model time series.

    The dashboard still receives every validly decoded packet, including a
    transient failed sensor. Prediction history deliberately skips incomplete
    packets, so the following complete ESP32 packet can be used immediately.
    """
    return bool(
        snapshot.get("windOk")
        and snapshot.get("airOk")
        and snapshot.get("soilOk")
        and (snapshot.get("solar1Ok") or snapshot.get("solar2Ok"))
        and int(snapshot.get("AirPressure", 0)) > 0
    )


def submit_snapshot(api_url: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Submit one normalized snapshot to the running prediction service."""
    payload = json.dumps(snapshot).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _LOCAL_HTTP_OPENER.open(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def result_to_display_command(result: dict[str, Any]) -> str:
    """Encode a compact prediction update that the ESP32 can parse without JSON."""
    status = str(result.get("status", "unknown"))
    available = int(result.get("availableSamples", 0))
    required = int(result.get("requiredSamples", 288))
    forecast = result.get("forecast") or []
    et0_mm = sum(float(point.get("et0Mm", 0.0)) for point in forecast)
    soil_percent = float(forecast[-1].get("soilMoisturePercent", 0.0)) if forecast else 0.0
    return (
        f"DISPLAY status={status} samples={available}/{required} "
        f"et0={et0_mm:.3f} soil={soil_percent:.1f}\n"
    )


@dataclass
class _ReceiverState:
    last_submit_at: float = 0.0
    last_live_log_at: float = 0.0
    last_display_diagnostic: tuple[bool, int, bool] | None = None


def _handle_telemetry_message(
    message: dict[str, Any],
    args: argparse.Namespace,
    state: _ReceiverState,
    *,
    send_display_command: Any | None = None,
) -> None:
    """Forward one decoded ESP32 message to the dashboard and prediction API."""
    display = message.get("display")
    if isinstance(display, dict):
        diagnostic = (
            bool(display.get("enabled", False)),
            int(display.get("rx_bytes", 0)),
            bool(display.get("handshake_ok", False)),
        )
        if diagnostic != state.last_display_diagnostic:
            print(
                "HMI diagnostic: "
                f"enabled={diagnostic[0]}, rx_bytes={diagnostic[1]}, "
                f"m_protocol_handshake={diagnostic[2]}"
            )
            state.last_display_diagnostic = diagnostic

    now = time.monotonic()
    try:
        raw_pressure_hpa = int(message.get("air_pressure_hpa", 0))
        snapshot = esp32_message_to_snapshot(
            message,
            fallback_air_pressure_hpa=args.fallback_air_pressure_hpa,
        )
        submit_snapshot(args.live_api_url, snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Ignored malformed ESP32 telemetry: {exc}")
        return
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"Live dashboard unavailable: {exc}")
        return

    if now - state.last_live_log_at >= 10:
        print("Live dashboard updated from ESP32 telemetry.")
        state.last_live_log_at = now

    if now - state.last_submit_at < args.min_interval_seconds:
        return

    if not snapshot_is_complete_for_prediction(snapshot):
        print("Skipped incomplete ESP32 packet for prediction; waiting for next packet.")
        return

    try:
        result = submit_snapshot(args.api_url, snapshot)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"Prediction service unavailable: {exc}")
        return

    state.last_submit_at = now
    if send_display_command is not None:
        send_display_command(result_to_display_command(result).encode("ascii"))
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


def _handle_ack_line(line: str, store: Store) -> bool:
    """Handle only the explicit ACK prefix; ordinary device logs stay logs."""
    if not line.startswith("@ACK "):
        return False
    try:
        ack = json.loads(line.removeprefix("@ACK "))
        if not isinstance(ack, dict) or not ack.get("requestId"):
            raise ValueError("ACK requires requestId")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Ignored malformed ESP32 ACK: {exc}")
        return True
    store.record_ack(ack)
    store.update_decision_ack(ack)
    if not ack.get("accepted") or ack.get("actualState") not in {"OPEN", "CLOSED"}:
        store.record_environment_event(
            "VALVE_EXECUTION_FAILURE", "high", "ESP32 拒绝水阀命令或返回异常状态",
            {"ack": ack}, "检查继电器、传感器安全条件和 USB 串口后再人工确认",
        )
    print(f"ESP32 ACK: requestId={ack['requestId']} accepted={ack.get('accepted')} state={ack.get('actualState')}")
    return True


def _handle_config_ack_line(line: str, store: Store) -> bool:
    if not line.startswith("@CONFIG_ACK "):
        return False
    try:
        ack = json.loads(line.removeprefix("@CONFIG_ACK "))
        if not isinstance(ack, dict) or not ack.get("requestId"):
            raise ValueError("CONFIG_ACK requires requestId")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Ignored malformed ESP32 CONFIG_ACK: {exc}")
        return True
    store.record_config_ack(ack)
    print(f"ESP32 CONFIG_ACK: requestId={ack['requestId']} accepted={ack.get('accepted')} mode={ack.get('samplingMode')}")
    return True


def _send_pending_commands(connection: Any, store: Store) -> None:
    for command in store.pending_commands(limit=1):
        line = "@COMMAND " + json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n"
        connection.write(line.encode("utf-8"))
        store.mark_command_sent(str(command["requestId"]))
        print(f"Sent ESP32 command: requestId={command.get('requestId')} action={command.get('action')}")


def _send_pending_configs(connection: Any, store: Store) -> None:
    for config in store.pending_sampling_configs(limit=1):
        line = "@CONFIG " + json.dumps(config, ensure_ascii=False, separators=(",", ":")) + "\n"
        connection.write(line.encode("utf-8"))
        store.mark_sampling_config_sent(str(config["requestId"]))
        print(f"Sent ESP32 sampling config: mode={config.get('samplingMode')} interval={config.get('readIntervalMs')}ms")


def _send_heartbeat(connection: Any) -> None:
    connection.write(b"@HEARTBEAT\n")


def receive_esp32(args: argparse.Namespace) -> None:
    """Keep one TCP connection to ESP32 and forward sampled messages to FastAPI."""
    state = _ReceiverState()
    store = Store(args.database)

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

                        _handle_telemetry_message(
                            message,
                            args,
                            state,
                            send_display_command=connection.sendall,
                        )
        except OSError as exc:
            print(f"ESP32 connection unavailable: {exc}. Retrying in 3 seconds...")
            time.sleep(3)


def receive_esp32_serial(args: argparse.Namespace) -> None:
    """Read prefixed JSON telemetry from the ESP32 USB serial port."""
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("pyserial is required; run: python -m pip install -r requirements.txt") from exc

    state = _ReceiverState()
    store = Store(args.database)
    while True:
        try:
            print(f"Opening ESP32 USB serial port {args.serial_port} at {args.baudrate} baud ...")
            with serial.Serial(args.serial_port, args.baudrate, timeout=1) as connection:
                print("Connected. Receiving ESP32 USB serial telemetry.")
                while True:
                    _send_heartbeat(connection)
                    _send_pending_commands(connection, store)
                    _send_pending_configs(connection, store)
                    line = connection.readline().decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if _handle_ack_line(line, store):
                        continue
                    if _handle_config_ack_line(line, store):
                        continue
                    if not line.startswith(args.telemetry_prefix):
                        if args.print_device_log:
                            print(f"ESP32: {line}")
                        continue
                    try:
                        message = json.loads(line.removeprefix(args.telemetry_prefix))
                    except json.JSONDecodeError as exc:
                        print(f"Ignored malformed ESP32 serial telemetry: {exc}")
                        continue
                    _handle_telemetry_message(message, args, state)
        except serial.SerialException as exc:
            print(f"ESP32 serial port unavailable: {exc}. Retrying in 3 seconds...")
            time.sleep(3)


def _add_submission_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        default="runtime/forecast.sqlite3",
        help="SQLite command queue and ACK database",
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000/v1/snapshots",
        help="local dual-forecast snapshot endpoint",
    )
    parser.add_argument(
        "--live-api-url",
        default="http://127.0.0.1:8000/v1/telemetry/live",
        help="local real-time dashboard telemetry endpoint",
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


def add_receiver_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "receive-esp32",
        help="receive ESP32 TCP telemetry and submit it to the local prediction service",
    )
    parser.add_argument(
        "--esp-host",
        default="esp32-sensors.local",
        help="ESP32 TCP server address; Station mode uses esp32-sensors.local by default",
    )
    parser.add_argument("--esp-port", type=int, default=3333, help="ESP32 TCP server port")
    _add_submission_options(parser)
    parser.set_defaults(func=receive_esp32)

    serial_parser = subparsers.add_parser(
        "receive-esp32-serial",
        help="receive ESP32 USB serial telemetry and submit it to the local prediction service",
    )
    serial_parser.add_argument(
        "--serial-port",
        required=True,
        help="macOS example: /dev/cu.wchusbserial10; Windows example: COM3",
    )
    serial_parser.add_argument("--baudrate", type=int, default=115200)
    serial_parser.add_argument("--telemetry-prefix", default="@TELEMETRY ")
    serial_parser.add_argument(
        "--print-device-log",
        action="store_true",
        help="also print normal ESP32 diagnostic lines",
    )
    _add_submission_options(serial_parser)
    serial_parser.set_defaults(func=receive_esp32_serial)
