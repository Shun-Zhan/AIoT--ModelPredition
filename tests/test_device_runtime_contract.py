from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "firmware" / "esp32_s3_all_sensors" / "device_runtime.h"
SOURCE = ROOT / "firmware" / "esp32_s3_all_sensors" / "device_runtime.cpp"


def read_runtime_files() -> tuple[str, str]:
    return HEADER.read_text(encoding="utf-8"), SOURCE.read_text(encoding="utf-8")


def test_runtime_scope_is_three_files_only():
    tracked_runtime_names = {
        path.name
        for path in (ROOT / "firmware" / "esp32_s3_all_sensors").glob("device_runtime.*")
    }
    assert tracked_runtime_names == {"device_runtime.h", "device_runtime.cpp"}
    assert HEADER.exists()
    assert SOURCE.exists()


def test_runtime_uses_a_generic_ntp_or_host_system_clock_contract():
    header, source = read_runtime_files()

    assert "deviceRuntimeIsValidUtcEpoch" in header
    assert "deviceRuntimeIsValidUtcEpoch" in source
    assert "DS3231" not in header
    assert "Ds3231" not in source
    assert "Wire.h" not in header


def test_v2_record_contains_version_length_sequence_time_slot_values_checksum():
    header, source = read_runtime_files()

    record_start = header.index("struct __attribute__((packed)) DeviceRuntimeRecordV2")
    record_end = header.index("};", record_start)
    record = header[record_start:record_end]
    for field in (
        "version",
        "length",
        "sequence",
        "epoch",
        "slot",
        "airTemperatureC",
        "soilMoisturePercent",
        "checksum",
    ):
        assert field in record
    assert "DEVICE_RUNTIME_RECORD_VERSION = 2" in header
    assert "deviceRuntimeRecordChecksum" in source
    assert "deviceRuntimeRecordIsValid" in source


def test_history_has_288_five_minute_continuity_and_recovery_api():
    header, source = read_runtime_files()

    assert "DEVICE_RUNTIME_RING_CAPACITY = 288" in header
    assert "DEVICE_RUNTIME_SAMPLE_INTERVAL_SECONDS = 5UL * 60UL" in header
    assert "appendCompleteSample" in header
    assert "restoreChronological" in header
    assert "copyChronological" in header
    assert "lastAppendResetWindow" in header
    assert "expectedSlot" in source
    assert "expectedEpoch" in source
    assert "clear();" in source


def test_local_irrigation_contract_has_thresholds_and_all_safety_gates():
    header, source = read_runtime_files()

    for text in (
        "20.0f",
        "30.0f",
        "45.0f",
        "0.30f",
        "75.0f",
        "DEVICE_RUNTIME_SINGLE_WATERING_SECONDS = 60",
        "DEVICE_RUNTIME_COOLDOWN_SECONDS = 15UL * 60UL",
        "DEVICE_RUNTIME_DAILY_WATERING_LIMIT_SECONDS = 600",
    ):
        assert text in header
    for field in (
        "clockValid",
        "prediction",
        "sensors",
        "valveState",
        "valveDriverHealthy",
    ):
        assert field in header
    for gate in (
        "clockGatePassed",
        "predictionGatePassed",
        "sensorGatePassed",
        "valveGatePassed",
        "shouldOpenValve",
    ):
        assert gate in header and gate in source


def test_auto_defaults_off_and_runtime_module_has_no_host_heartbeat_dependency():
    header, source = read_runtime_files()

    assert "automaticModeEnabled = false" in source
    assert "setAutomaticModeEnabled" in header
    assert "hostHeartbeat" not in header
    assert "lastHostHeartbeat" not in source
    assert "HOST_HEARTBEAT" not in source


def test_firmware_uses_ntp_clock_only_for_the_current_boot_without_hardware_rtc():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")

    assert "DEVICE_CLOCK_NTP" in firmware
    assert "deviceNtpClockValid" in firmware
    assert "deviceReadTrustedEpoch" in firmware
    assert "[CLOCK] NTP synchronized" in firmware
    assert "clockSource" in firmware
    assert "DS3231" not in firmware
    assert "deviceClockValid" in firmware
