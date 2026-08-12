from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

import serial
from serial.tools import list_ports


STATUS_PREFIX = "@OFFLINE_LOG_STATUS "
RECORD_PREFIX = "@OFFLINE_LOG_RECORD "
DUMP_END_PREFIX = "@OFFLINE_LOG_DUMP_END "
ERASE_ACK_PREFIX = "@OFFLINE_LOG_ERASE_ACK "
IMPORT_ACK_PREFIX = "@HISTORY_IMPORT_ACK "
IMPORT_MAX_RECORDS = 288
IMPORT_INTERVAL_MS = 5 * 60 * 1000
IMPORT_MAX_GAP_MS = IMPORT_INTERVAL_MS + 60 * 1000
IMPORT_REQUIRED_COLUMNS = (
    "integrityOk", "bootSessionId", "uptimeMs", "windOk", "airOk", "soilOk",
    "solar1Ok", "solar2Ok", "airPressureHpa", "windSpeedMs",
    "airTemperatureC", "airHumidityPercent", "soilTemperatureC",
    "soilMoisturePercent", "solar1Wm2", "solar2Wm2",
)


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
            records.append(_json_after_prefix(line, RECORD_PREFIX))
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
    ]
    with output.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    return records, summary


def erase_and_restart(device: serial.Serial) -> dict[str, Any]:
    device.reset_input_buffer()
    _send_line(device, "@OFFLINE_LOG_ERASE CONFIRM")
    response = _json_after_prefix(
        _wait_for_prefix(device, ERASE_ACK_PREFIX), ERASE_ACK_PREFIX
    )
    if not response.get("accepted"):
        raise RuntimeError(f"ESP32 擦除失败：{response.get('reason', 'unknown')}")
    return response


def _truthy(value: str) -> bool:
    return value.strip().casefold() in {"true", "1", "yes"}


def _complete_import_row(row: dict[str, str]) -> bool:
    return all(_truthy(row.get(key, "")) for key in (
        "integrityOk", "windOk", "airOk", "soilOk", "solar1Ok", "solar2Ok"
    ))


def _compact_import_sample(row: dict[str, str]) -> dict[str, float]:
    try:
        sample = {
            "t": float(row["airTemperatureC"]),
            "h": float(row["airHumidityPercent"]),
            "p": float(row["airPressureHpa"]),
            "st": float(row["soilTemperatureC"]),
            "sm": float(row["soilMoisturePercent"]),
            "si": float(row["solar2Wm2"]),
            "sr": float(row["solar1Wm2"]),
            "w": float(row["windSpeedMs"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"CSV 数值列无效：{exc}") from exc
    if not (0 <= sample["h"] <= 100 and 0 <= sample["sm"] <= 100):
        raise ValueError("CSV 中空气湿度或土壤湿度不在 0-100% 范围")
    if sample["p"] <= 0 or sample["si"] < 0 or sample["sr"] < 0 or sample["w"] < 0:
        raise ValueError("CSV 中气压、太阳辐射或风速存在无效负值")
    return sample


def load_recent_continuous_import_window(input_path: Path) -> list[dict[str, float]]:
    """Read the existing offline-log CSV and select its longest valid run.

    A device reboot starts a new ``bootSessionId`` and commonly leaves a very
    short final run.  Keeping the longest run preserves the actual five-minute
    cadence instead of accidentally importing that one post-reboot row.
    """
    with input_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("CSV 没有表头")
        missing = [name for name in IMPORT_REQUIRED_COLUMNS if name not in reader.fieldnames]
        if missing:
            raise ValueError("CSV 缺少导出字段：" + "、".join(missing))
        rows = list(reader)
    if not rows:
        raise ValueError("CSV 没有数据行")

    run: list[dict[str, float]] = []
    selected: list[dict[str, float]] = []
    previous_session: str | None = None
    previous_uptime: int | None = None
    for row in rows:
        try:
            session = row["bootSessionId"].strip()
            uptime = int(row["uptimeMs"])
        except (KeyError, TypeError, ValueError):
            run = []
            previous_session = None
            previous_uptime = None
            continue
        continuous = (
            bool(run)
            and session == previous_session
            and previous_uptime is not None
            and IMPORT_INTERVAL_MS - 60_000 <= uptime - previous_uptime <= IMPORT_MAX_GAP_MS
        )
        if not _complete_import_row(row):
            run = []
        else:
            try:
                sample = _compact_import_sample(row)
            except ValueError:
                run = []
            else:
                if not continuous:
                    run = []
                run.append(sample)
                if len(run) >= len(selected):
                    selected = run[-IMPORT_MAX_RECORDS:]
        previous_session = session
        previous_uptime = uptime
    if not selected:
        raise ValueError("CSV 中没有连续的完整传感器记录")
    return selected


def import_records(device: serial.Serial, input_path: Path) -> dict[str, Any]:
    """Safely stream the newest valid CSV run into ESP32 V2 model history."""
    samples = load_recent_continuous_import_window(input_path)
    device.reset_input_buffer()
    _send_line(device, "@HISTORY_IMPORT_BEGIN " + json.dumps({"count": len(samples)}))
    begin = _json_after_prefix(_wait_for_prefix(device, IMPORT_ACK_PREFIX), IMPORT_ACK_PREFIX)
    if not begin.get("accepted"):
        raise RuntimeError(f"ESP32 拒绝导入：{begin.get('reason', 'unknown')}")
    for sample in samples:
        _send_line(device, "@HISTORY_IMPORT_RECORD " + json.dumps(sample, separators=(",", ":")))
        time.sleep(0.02)
    _send_line(device, "@HISTORY_IMPORT_END")
    result = _json_after_prefix(
        _wait_for_prefix(device, IMPORT_ACK_PREFIX, timeout_seconds=45), IMPORT_ACK_PREFIX
    )
    if not result.get("accepted"):
        raise RuntimeError(f"ESP32 导入失败：{result.get('reason', 'unknown')}")
    if int(result.get("importedRecords", -1)) != len(samples):
        raise RuntimeError("ESP32 导入条数与电脑发送条数不一致")
    return result


def _print_status(status: dict[str, Any]) -> None:
    print(
        "LittleFS：{ready}；当前文件 {current} 条；轮换文件 {previous} 条；"
        "合计 {total} 条；模式 {mode}；周期 {interval} ms".format(
            ready="可用" if status.get("ready") else "不可用",
            current=status.get("currentRecords", 0),
            previous=status.get("previousRecords", 0),
            total=status.get("totalRecords", 0),
            mode=status.get("samplingMode", "unknown"),
            interval=status.get("readIntervalMs", "unknown"),
        )
    )


def _interactive_action() -> str:
    print("\nESP32 本地记录管理")
    print("  1. 查看记录状态")
    print("  2. 读取并导出为 CSV")
    print("  3. 擦除记录并立即启动新一轮采集")
    print("  4. 从现有导出 CSV 回灌预测历史")
    print("  0. 退出")
    choice = input("请选择：").strip()
    return {"1": "status", "2": "export", "3": "erase", "4": "import", "0": "quit"}.get(
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
                            "并已安排立即采集；只有完整传感器样本才会写入。"
                        )
                elif selected == "import":
                    input_path = Path(args.input) if args.input else Path(
                        input("请输入之前导出的 CSV 路径：").strip()
                    )
                    result = import_records(device, input_path)
                    imported = int(result["importedRecords"])
                    required = int(result["requiredRecords"])
                    print(
                        f"已回灌 {imported}/{required} 条到 ESP32 V2 历史；"
                        + ("模型推理已排队，稍候会显示趋势图。" if imported == required
                           else f"还需连续采集 {required - imported} 条完整真实样本。")
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
        choices=("interactive", "status", "export", "erase", "import"),
        default="interactive",
    )
    parser.add_argument(
        "--output", default="outputs/esp32-offline-log.csv", help="CSV export path"
    )
    parser.add_argument("--input", help="previously exported ESP32 offline-log CSV for --action import")
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
