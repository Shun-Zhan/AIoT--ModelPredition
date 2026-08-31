from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import serial
from serial.tools import list_ports


STATUS_PREFIX = "@OFFLINE_LOG_STATUS "
RECORD_PREFIX = "@OFFLINE_LOG_RECORD "
DUMP_END_PREFIX = "@OFFLINE_LOG_DUMP_END "
ERASE_ACK_PREFIX = "@OFFLINE_LOG_ERASE_ACK "


def _json_after_prefix(line: str, prefix: str) -> dict[str, Any]:
    if not line.startswith(prefix):
        raise ValueError(f"unexpected ESP32 response: {line}")
    return json.loads(line[len(prefix) :])


def _available_ports() -> list[str]:
    return [port.device for port in list_ports.comports()]


def _choose_port(requested: str | None) -> str:
    if requested:
        return requested
    ports = _available_ports()
    likely = [
        port
        for port in ports
        if any(token in port.lower() for token in ("usb", "wch", "slab", "modem"))
    ]
    if len(likely) == 1:
        return likely[0]
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise SystemExit("未发现串口。请连接 ESP32，或用 --serial-port 明确指定端口。")
    print("检测到多个串口：")
    for index, port in enumerate(ports, start=1):
        print(f"  {index}. {port}")
    try:
        selected = int(input("请选择 ESP32 串口编号：").strip())
        return ports[selected - 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit("串口编号无效。") from exc


def _send_line(device: serial.Serial, line: str) -> None:
    device.write((line + "\n").encode("utf-8"))
    device.flush()


def _wait_for_prefix(
    device: serial.Serial, prefix: str, timeout_seconds: float = 30
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        raw = device.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith(prefix):
            return line
    raise TimeoutError(
        f"等待 ESP32 响应 {prefix.strip()} 超时；请确认烧录了最新版固件，"
        "并关闭 Arduino 串口监视器和 Dashboard 串口接收器。"
    )


def read_status(device: serial.Serial) -> dict[str, Any]:
    device.reset_input_buffer()
    _send_line(device, "@OFFLINE_LOG_STATUS")
    return _json_after_prefix(_wait_for_prefix(device, STATUS_PREFIX), STATUS_PREFIX)


def export_records(device: serial.Serial, output: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    device.reset_input_buffer()
    _send_line(device, "@OFFLINE_LOG_DUMP")
    records: list[dict[str, Any]] = []
    deadline = time.monotonic() + 600
    summary: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        raw = device.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith(RECORD_PREFIX):
            record = _json_after_prefix(line, RECORD_PREFIX)
            recorded_at_epoch_ms = record.get("recordedAtEpochMs")
            if isinstance(recorded_at_epoch_ms, int) and recorded_at_epoch_ms > 0:
                record["recordedAt"] = datetime.fromtimestamp(
                    recorded_at_epoch_ms / 1000, tz=timezone.utc
                ).astimezone().isoformat(timespec="milliseconds")
            elif "recordedAtEpochMs" in record:
                record["recordedAt"] = ""
            records.append(record)
        elif line.startswith(DUMP_END_PREFIX):
            summary = _json_after_prefix(line, DUMP_END_PREFIX)
            break
    if summary is None:
        raise TimeoutError("离线记录导出超时，尚未收到 @OFFLINE_LOG_DUMP_END。")
    if not summary.get("accepted"):
        raise RuntimeError(f"ESP32 拒绝导出：{summary.get('reason', 'unknown')}")
    if summary.get("exportedRecords") != len(records):
        raise RuntimeError(
            "导出条数不一致："
            f"ESP32={summary.get('exportedRecords')}，电脑收到={len(records)}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(records[0]) if records else [
        "source",
        "index",
        "integrityOk",
        "bootSessionId",
        "uptimeMs",
        "recordedAtEpochMs",
        "recordedAt",
        "timeSource",
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    return records, summary


def erase_and_restart(
    device: serial.Serial, system_time_ms: int | None = None
) -> dict[str, Any]:
    if system_time_ms is None:
        system_time_ms = time.time_ns() // 1_000_000
    device.reset_input_buffer()
    _send_line(device, f"@OFFLINE_LOG_ERASE CONFIRM {system_time_ms}")
    response = _json_after_prefix(
        _wait_for_prefix(device, ERASE_ACK_PREFIX), ERASE_ACK_PREFIX
    )
    if not response.get("accepted"):
        raise RuntimeError(f"ESP32 擦除失败：{response.get('reason', 'unknown')}")
    return response


def _print_status(status: dict[str, Any]) -> None:
    print(
        "LittleFS：{ready}；当前文件 {current} 条；轮换文件 {previous} 条；"
        "合计 {total} 条；模式 {mode}；周期 {interval} ms；时间来源 {time_source}".format(
            ready="可用" if status.get("ready") else "不可用",
            current=status.get("currentRecords", 0),
            previous=status.get("previousRecords", 0),
            total=status.get("totalRecords", 0),
            mode=status.get("samplingMode", "unknown"),
            interval=status.get("readIntervalMs", "unknown"),
            time_source=status.get("timeSource", "unknown"),
        )
    )


def _interactive_action() -> str:
    print("\nESP32 本地记录管理")
    print("  1. 查看记录状态")
    print("  2. 读取并导出为 CSV")
    print("  3. 擦除记录并立即启动新一轮采集")
    print("  0. 退出")
    choice = input("请选择：").strip()
    return {"1": "status", "2": "export", "3": "erase", "0": "quit"}.get(
        choice, "invalid"
    )


def manage_offline_log(args: argparse.Namespace) -> None:
    port = _choose_port(args.serial_port)
    action = args.action
    try:
        device = serial.Serial(
            port, args.baudrate, timeout=0.5, write_timeout=5
        )
    except serial.SerialException as exc:
        if getattr(exc, "errno", None) == 2:
            detected = _available_ports()
            detected_text = "、".join(detected) if detected else "无"
            raise SystemExit(
                f"ESP32 串口 {port} 不存在。当前检测到：{detected_text}。"
                "USB 重插后端口名可能变化；建议省略 --serial-port 让程序自动识别。"
            ) from None
        raise SystemExit(
            f"无法打开 ESP32 串口 {port}：{exc}。请关闭 Arduino 串口监视器、"
            "Dashboard 串口接收器或其他占用该端口的程序。"
        ) from None
    try:
        with device:
            # Some ESP32-S3 boards reset when the serial port is opened.
            time.sleep(args.connect_delay)
            while True:
                selected = _interactive_action() if action == "interactive" else action
                if selected == "quit":
                    return
                if selected == "invalid":
                    print("选择无效，请重试。")
                    continue
                if selected == "status":
                    _print_status(read_status(device))
                elif selected == "export":
                    records, summary = export_records(device, Path(args.output))
                    print(
                        f"已导出 {len(records)} 条到 {Path(args.output).resolve()}；"
                        f"完整性异常 {summary.get('corruptRecords', 0)} 条。"
                    )
                elif selected == "erase":
                    confirmed = args.yes
                    if not confirmed:
                        confirmed = (
                            input("此操作不可恢复。请输入 ERASE 确认擦除：").strip()
                            == "ERASE"
                        )
                    if not confirmed:
                        print("已取消，没有擦除任何记录。")
                    else:
                        erase_and_restart(device)
                        print(
                            "记录已擦除或初始化，ESP32 已恢复 OFFLINE_LOGGING 模式，"
                            "电脑系统时间已写入，且已安排立即采集；"
                            "只有完整传感器样本才会写入。"
                        )
                if action != "interactive":
                    return
    except (TimeoutError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


def add_offline_log_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "offline-log", help="inspect, export, or erase ESP32 LittleFS offline records"
    )
    parser.add_argument(
        "--serial-port",
        help="ESP32 USB port; auto-detected when exactly one suitable port exists",
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "--action",
        choices=("interactive", "status", "export", "erase"),
        default="interactive",
    )
    parser.add_argument(
        "--output", default="outputs/esp32-offline-log.csv", help="CSV export path"
    )
    parser.add_argument(
        "--yes", action="store_true", help="confirm erase without an interactive prompt"
    )
    parser.add_argument(
        "--connect-delay",
        type=float,
        default=2.0,
        help="seconds to wait after opening a serial port",
    )
    parser.set_defaults(func=manage_offline_log)
