from __future__ import annotations

import json
from pathlib import Path

from dual_forecast.offline_log import (
    erase_and_restart,
    export_records,
    import_records,
    load_recent_continuous_import_window,
    read_status,
)


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


def test_imports_existing_export_format_as_a_recent_continuous_window(tmp_path):
    input_path = tmp_path / "offline.csv"
    input_path.write_text(
        "integrityOk,bootSessionId,uptimeMs,windOk,airOk,soilOk,solar1Ok,solar2Ok,"
        "airPressureHpa,windSpeedMs,airTemperatureC,airHumidityPercent,soilTemperatureC,"
        "soilMoisturePercent,solar1Wm2,solar2Wm2\n"
        "True,7,1000,True,True,True,True,True,1003,1.2,25.1,60.2,23.3,41.4,20,100\n"
        "True,7,301000,True,True,True,True,True,1003,1.3,25.2,60.3,23.4,41.3,21,101\n",
        encoding="utf-8",
    )
    samples = load_recent_continuous_import_window(input_path)
    assert len(samples) == 2
    assert samples[-1]["sm"] == 41.3

    device = FakeSerial([
        _line("@HISTORY_IMPORT_ACK ", {"accepted": True, "stage": "begin"}),
        _line("@HISTORY_IMPORT_ACK ", {
            "accepted": True, "stage": "end", "importedRecords": 2,
            "requiredRecords": 288, "reason": "history_imported_waiting_for_samples",
        }),
    ])
    result = import_records(device, input_path)
    writes = b"".join(device.writes).decode()
    assert result["importedRecords"] == 2
    assert "@HISTORY_IMPORT_BEGIN" in writes
    assert writes.count("@HISTORY_IMPORT_RECORD") == 2
    assert "@HISTORY_IMPORT_END" in writes


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
    assert "USB_HISTORY_IMPORT_BEGIN_PREFIX" in firmware
    assert "deviceFinishHistoryImport" in firmware
    assert "DeviceRuntimeInstance.setAutomaticModeEnabled(false)" in firmware
