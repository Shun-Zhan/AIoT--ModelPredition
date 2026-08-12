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


def test_manual_debug_valve_pulse_is_explicit_fixed_and_does_not_weaken_formal_gate():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    debug_start = firmware.index('strcmp(action, "DEBUG_VALVE_PULSE")')
    formal_start = firmware.index(
        'strcmp(action, "START_WATERING")', debug_start
    )
    debug_block = firmware[debug_start:formal_start]

    assert "duration != 5UL" in debug_block
    assert "deviceSyntheticHistoryBlocksValve" in debug_block
    assert "valve_already_open" in debug_block
    assert "DEVICE_RUNTIME_DAILY_WATERING_LIMIT_SECONDS" in debug_block
    assert '"debug_started_5s"' in debug_block
    assert "valveCountsForFormalCooldown = false" in debug_block
    assert "deviceManualStartAllowed" not in debug_block
    formal_block = firmware[formal_start:]
    assert "deviceManualStartAllowed" in formal_block
    assert "valveCountsForFormalCooldown = true" in formal_block


def test_only_formal_watering_updates_cooldown_timestamp():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    relay_start = firmware.index("void setValveRelay(bool open)")
    relay_end = firmware.index("bool jsonStringValue", relay_start)
    relay_block = firmware[relay_start:relay_end]

    assert "if (valveCountsForFormalCooldown && nowEpochUtc != 0)" in relay_block
    assert "deviceLastWateringEpochUtc = nowEpochUtc" in relay_block
    assert 'putBool("formal_v2", true)' in relay_block
    assert "valveCountsForFormalCooldown = false" in relay_block


def test_legacy_debug_cooldown_state_is_migrated_without_erasing_daily_total():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    load_start = firmware.index("static void deviceLoadIrrigationCounters()")
    load_end = firmware.index("static void deviceRestoreHistory", load_start)
    load_block = firmware[load_start:load_end]

    assert 'getBool("formal_v2", false)' in load_block
    assert "!formalCooldownTagged && deviceLastWateringEpochUtc != 0" in load_block
    assert 'putUInt("last_epoch", 0)' in load_block
    assert 'putBool("formal_v2", true)' in load_block
    assert 'putUInt("daily_sec", 0)' not in load_block


def test_ui_ack_reports_gpio_level_without_claiming_physical_feedback():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    ack_start = firmware.index("static void emitDeviceUiAck")
    ack_end = firmware.index("void emitDeviceCloudResult", ack_start)
    ack = firmware[ack_start:ack_end]
    assert 'document["relayGpio"] = VALVE_RELAY_PIN' in ack
    assert "digitalRead(VALVE_RELAY_PIN)" in ack
    assert 'document["physicalFeedbackAvailable"] = false' in ack


def test_installed_relay_uses_high_level_trigger_with_low_safe_state():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    assert "VALVE_RELAY_ACTIVE_HIGH = true" in firmware
    assert "open == VALVE_RELAY_ACTIVE_HIGH ? HIGH : LOW" in firmware
