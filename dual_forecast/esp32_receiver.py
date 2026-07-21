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


# ESP32 periodically emits this small UDP datagram after it has joined a Wi-Fi
# network and opened its TCP service.  It removes the need to depend on mDNS
# (esp32-sensors.local), which many phone/Windows hotspots do not forward.
AUTO_DISCOVERY_HOST = "auto"
DISCOVERY_PORT = 3334
DISCOVERY_PREFIX = b"AIOT_DISCOVERY "
# Some phone hotspots block local UDP broadcast even though ESP32 TCP and
# mDNS traffic are permitted.  The firmware advertises this stable hostname.
MDNS_FALLBACK_HOST = "esp32-sensors.local"


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

    snapshot = {
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
    raw_edge = message.get("edge_prediction")
    if isinstance(raw_edge, dict):
        valid = bool(raw_edge.get("valid", False))
        risk_level = str(raw_edge.get("risk_level", "SENSOR_INVALID"))
        if risk_level not in {"NORMAL", "ATTENTION", "DRY_RISK", "SENSOR_INVALID"}:
            risk_level = "SENSOR_INVALID"
        snapshot["edgePrediction"] = {
            "valid": valid,
            "mode": "edge_fallback",
            "predictedSoilMoisture30mPercent": (
                float(raw_edge["predicted_soil_moisture_30m_pct"]) if valid else None
            ),
            "dryingRatePercentPerHour": (
                float(raw_edge["drying_rate_pct_per_h"]) if valid else None
            ),
            "riskLevel": risk_level,
            "reason": str(raw_edge.get("reason", "sensor_invalid"))[:64] or "sensor_invalid",
            "updatedUptimeMs": int(raw_edge.get("updated_uptime_ms", message["uptime_ms"])),
        }
    return snapshot


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


@dataclass(frozen=True)
class Esp32Endpoint:
    """One locally announced ESP32 TCP endpoint."""

    host: str
    port: int


class _SocketWriter:
    """Give a TCP socket the small write() interface used by command helpers."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def write(self, data: bytes) -> None:
        self._connection.sendall(data)


def parse_discovery_announcement(data: bytes, sender_host: str) -> Esp32Endpoint | None:
    """Validate one ESP32 UDP announcement and use its source IP as authority.

    The advertised IP is deliberately ignored: it can be stale after DHCP
    changes, while the UDP sender address is the address currently reachable
    by this computer on the local network.
    """
    if not data.startswith(DISCOVERY_PREFIX):
        return None
    try:
        announcement = json.loads(data[len(DISCOVERY_PREFIX) :].decode("utf-8"))
        port = int(announcement["port"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if announcement.get("service") != "aiot-esp32" or not 1 <= port <= 65535:
        return None
    return Esp32Endpoint(host=sender_host, port=port)


def _open_discovery_socket(port: int) -> socket.socket:
    discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    discovery_socket.bind(("", port))
    discovery_socket.settimeout(10)
    return discovery_socket


def _wait_for_esp32_discovery(discovery_socket: socket.socket) -> Esp32Endpoint:
    """Wait until a valid local ESP32 UDP announcement arrives."""
    while True:
        data, sender = discovery_socket.recvfrom(1024)
        endpoint = parse_discovery_announcement(data, sender[0])
        if endpoint is not None:
            return endpoint


def resolve_mdns_fallback_endpoint(port: int) -> Esp32Endpoint | None:
    """Resolve the firmware's mDNS host when hotspot UDP is filtered.

    This deliberately returns ``None`` rather than raising for ordinary DNS
    misses, so automatic discovery can keep waiting for a later UDP broadcast
    or a later mDNS registration after the ESP32 reconnects.
    """
    try:
        host = socket.gethostbyname(MDNS_FALLBACK_HOST)
    except OSError:
        return None
    return Esp32Endpoint(host=host, port=port)


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
    use_auto_discovery = args.esp_host.casefold() == AUTO_DISCOVERY_HOST
    discovery_socket: socket.socket | None = None

    try:
        if use_auto_discovery:
            discovery_socket = _open_discovery_socket(args.discovery_port)
            print(
                "Waiting for ESP32 Wi-Fi auto-discovery announcements "
                f"on UDP port {args.discovery_port} ..."
            )

        while True:
            try:
                if discovery_socket is not None:
                    try:
                        endpoint = _wait_for_esp32_discovery(discovery_socket)
                        print(f"Discovered ESP32 at {endpoint.host}:{endpoint.port} via UDP broadcast.")
                    except socket.timeout:
                        endpoint = resolve_mdns_fallback_endpoint(args.esp_port)
                        if endpoint is None:
                            print(
                                "No ESP32 UDP discovery announcement and mDNS fallback is unavailable. "
                                "Waiting for the next announcement..."
                            )
                            continue
                        print(
                            "No ESP32 UDP discovery announcement; "
                            f"using mDNS fallback {MDNS_FALLBACK_HOST} → {endpoint.host}:{endpoint.port}."
                        )
                    esp_host, esp_port = endpoint.host, endpoint.port
                else:
                    esp_host, esp_port = args.esp_host, args.esp_port

                print(f"Connecting to ESP32 TCP server at {esp_host}:{esp_port} ...")
                with socket.create_connection((esp_host, esp_port), timeout=10) as connection:
                    # recv() times out once a second so the loop can send the
                    # safety heartbeat and queued commands even when no new
                    # sensor line happens to arrive during that second.
                    connection.settimeout(1)
                    writer = _SocketWriter(connection)
                    pending_text = ""
                    next_control_at = 0.0
                    print("Connected. Receiving ESP32 telemetry.")

                    while True:
                        now = time.monotonic()
                        if now >= next_control_at:
                            _send_heartbeat(writer)
                            _send_pending_commands(writer, store)
                            _send_pending_configs(writer, store)
                            next_control_at = now + 2.0

                        try:
                            chunk = connection.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            raise ConnectionError("ESP32 closed the TCP connection")

                        pending_text += chunk.decode("utf-8", errors="replace")
                        while "\n" in pending_text:
                            line, pending_text = pending_text.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if _handle_ack_line(line, store) or _handle_config_ack_line(line, store):
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
    finally:
        if discovery_socket is not None:
            discovery_socket.close()


def receive_esp32_serial(args: argparse.Namespace) -> None:
    """Read prefixed JSON telemetry from the ESP32 USB serial port.

    USB-to-UART bridges may disappear briefly when the board resets or power
    fluctuates.  The bridge must keep running in that case: live telemetry is
    best-effort, while the irrigation controller remains safely closed.
    """
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
                # Do not inject a heartbeat before every one-second read.
                # Apart from needless traffic, frequent writes make a flaky
                # USB-UART bridge more likely to reset.  This matches the TCP
                # receiver's two-second control cadence.
                next_control_at = 0.0
                while True:
                    now = time.monotonic()
                    if now >= next_control_at:
                        _send_heartbeat(connection)
                        _send_pending_commands(connection, store)
                        _send_pending_configs(connection, store)
                        next_control_at = now + 2.0
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
        except KeyboardInterrupt:
            raise
        except (serial.SerialException, OSError) as exc:
            print(f"ESP32 serial port unavailable: {exc}. Retrying in 3 seconds...")
            time.sleep(3)
        except Exception as exc:  # keep the unattended bridge alive and log the cause
            print(f"ESP32 serial receiver error: {type(exc).__name__}: {exc}. Retrying in 3 seconds...")
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
        default=30.0,
        help=(
            "minimum interval between complete snapshots (default: 30 seconds). "
            "The inference pipeline still aggregates these into five-minute model intervals."
        ),
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
        default=AUTO_DISCOVERY_HOST,
        help=(
            "ESP32 TCP server address. Default 'auto' listens for local UDP discovery; "
            "an IP address or esp32-sensors.local can be used as an explicit fallback"
        ),
    )
    parser.add_argument("--esp-port", type=int, default=3333, help="ESP32 TCP server port")
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=DISCOVERY_PORT,
        help="local UDP port used only when --esp-host auto (default: 3334)",
    )
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
