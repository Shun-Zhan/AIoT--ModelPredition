from __future__ import annotations

import json
from pathlib import Path

from dual_forecast.offline_log import erase_and_restart, export_records, read_status


class FakeSerial:
    def __init__(self, lines: list[str]):
        self.lines = [(line + "\n").encode() for line in lines]
        self.writes: list[bytes] = []

    def reset_input_buffer(self):
        pass

    def write(self, value: bytes):
        self.writes.append(value)
        return len(value)

    def flush(self):
        pass

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


def _line(prefix: str, payload: dict) -> str:
    return prefix + json.dumps(payload, separators=(",", ":"))


def test_reads_offline_log_status():
    device = FakeSerial([
        "ordinary boot diagnostic",
        _line(
            "@OFFLINE_LOG_STATUS ",
            {
                "ready": True,
                "currentRecords": 3,
                "previousRecords": 2,
                "totalRecords": 5,
            },
        ),
    ])

    status = read_status(device)

    assert device.writes == [b"@OFFLINE_LOG_STATUS\n"]
    assert status["totalRecords"] == 5


def test_exports_records_to_csv_and_checks_count(tmp_path):
    record = {
        "source": "current",
        "index": 0,
        "integrityOk": True,
        "bootSessionId": 42,
        "uptimeMs": 1234,
        "soilMoisturePercent": 37.5,
    }
    device = FakeSerial([
        _line("@OFFLINE_LOG_DUMP_BEGIN ", {"expectedRecords": 1}),
        _line("@OFFLINE_LOG_RECORD ", record),
        _line(
            "@OFFLINE_LOG_DUMP_END ",
            {"accepted": True, "exportedRecords": 1, "corruptRecords": 0},
        ),
    ])
    output = tmp_path / "offline.csv"

    records, summary = export_records(device, output)

    assert device.writes == [b"@OFFLINE_LOG_DUMP\n"]
    assert records == [record]
    assert summary["corruptRecords"] == 0
    assert "soilMoisturePercent" in output.read_text(encoding="utf-8-sig")


def test_erase_requires_firmware_confirmation_and_restarts_logging():
    device = FakeSerial([
        _line(
            "@OFFLINE_LOG_ERASE_ACK ",
            {
                "accepted": True,
                "samplingMode": "OFFLINE_LOGGING",
                "reason": "erased_and_sampling_scheduled",
            },
        )
    ])

    response = erase_and_restart(device)

    assert device.writes == [b"@OFFLINE_LOG_ERASE CONFIRM\n"]
    assert response["samplingMode"] == "OFFLINE_LOGGING"


def test_firmware_requires_explicit_erase_confirmation_and_checks_integrity():
    firmware = (
        Path(__file__).resolve().parents[1]
        / "firmware"
        / "esp32_s3_all_sensors"
        / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")

    assert 'USB_OFFLINE_LOG_ERASE_COMMAND = "@OFFLINE_LOG_ERASE CONFIRM"' in firmware
    assert "offlineLogRecordIsValid(record)" in firmware
    assert '\\"reason\\":\\"valve_open\\"' in firmware
    assert "nextSensorReadAtMs = 0;" in firmware
