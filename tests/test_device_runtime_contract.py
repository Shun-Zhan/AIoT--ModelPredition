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
    assert "DEVICE_RUNTIME_DAILY_WATERING_LIMIT_SECONDS" not in debug_block
    assert '"daily_limit"' not in debug_block
    assert '"debug_started_5s"' in debug_block
    assert "valveCountsForFormalCooldown = false" in debug_block
    assert "deviceManualStartAllowed" not in debug_block
    formal_block = firmware[formal_start:]
    assert "deviceManualStartAllowed" in formal_block
    assert "valveCountsForFormalCooldown = true" in formal_block


def test_voice_open_valve_uses_debug_path_but_voice_start_uses_formal_path():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    voice_start = firmware.index("static void handleVoiceCommand")
    voice_end = firmware.index("static void serviceVoiceUart", voice_start)
    voice_block = firmware[voice_start:voice_end]

    open_start = voice_block.index("case VOICE_CMD_OPEN_VALVE")
    formal_start = voice_block.index("case VOICE_CMD_START_IRRIGATION")
    close_start = voice_block.index("case VOICE_CMD_CLOSE_VALVE")
    open_block = voice_block[open_start:formal_start]
    formal_block = voice_block[formal_start:close_start]

    assert '"action\\":\\"DEBUG_VALVE_PULSE' in open_block
    assert '"durationSeconds\\":5' in open_block
    assert '"action\\":\\"START_WATERING' in formal_block
    assert "DEVICE_RUNTIME_SINGLE_WATERING_SECONDS" in formal_block

    debug_start = firmware.index('strcmp(action, "DEBUG_VALVE_PULSE")')
    formal_handler_start = firmware.index(
        'strcmp(action, "START_WATERING")', debug_start
    )
    debug_handler = firmware[debug_start:formal_handler_start]
    assert "valveRequiresHostHeartbeat = !voiceSource" in debug_handler


def test_daily_total_is_session_only_and_never_blocks_valve_commands():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    _, runtime = read_runtime_files()

    assert 'putUInt("daily_sec"' not in firmware
    assert 'getUInt("daily_sec"' not in firmware
    assert 'constraints["maxSingleWateringSeconds"]' in firmware
    assert 'constraints["wateringLast7Days"]' not in firmware
    assert "DEVICE_IRRIGATION_DAILY_LIMIT" not in runtime


def test_first_cloud_request_waits_for_complete_sensor_sample():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")

    assert "static bool deviceCloudInputReady()" in firmware
    assert "pendingCloudAnalysisRequestId" in firmware
    assert '"queued_waiting_for_sensors"' in firmware
    sample_start = firmware.index("void processDeviceRuntimeSample(", 1000)
    sample_start = firmware.index("void processDeviceRuntimeSample(", sample_start + 1)
    sample_end = firmware.index("void initDeviceRuntime", sample_start)
    sample_block = firmware[sample_start:sample_end]
    assert sample_block.index("latestDeviceSample = sample") < sample_block.index(
        "deviceSubmitPendingCloudWhenReady()"
    )

    cycle_start = firmware.index("  latestSensorSnapshotValid =\n")
    cycle_end = firmware.index("const bool completeForOfflineLog", cycle_start)
    cycle_block = firmware[cycle_start:cycle_end]
    runtime_update = cycle_block.index("processDeviceRuntimeSample(snapshot)")
    assert runtime_update < cycle_block.index("serviceUsbControl()")
    assert runtime_update < cycle_block.index("sendTelemetry(snapshot, edgePrediction)")
    assert cycle_block.count("processDeviceRuntimeSample(snapshot)") == 1


def test_cloud_confirmation_reuses_cloud_candidate_gates_without_prediction_only_rejection():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    gate_start = firmware.index("static bool deviceConfirmedCloudStartAllowed(", 1000)
    gate_start = firmware.index("static bool deviceConfirmedCloudStartAllowed(", gate_start + 1)
    gate_end = firmware.index("void handleDeviceUiCommand", gate_start)
    gate = firmware[gate_start:gate_end]

    assert "deviceCloudIrrigationCandidate()" in gate
    assert "sensorsValid" in gate
    assert "dailyLimitSafe" not in gate
    assert '"daily_limit"' not in gate
    assert "cooldownSafe" in gate
    assert "deviceForecast.valid" not in gate
    assert "prediction_invalid" not in gate

    confirm_start = firmware.index('strcmp(action, "START_WATERING")', gate_end)
    confirm_end = firmware.index('strcmp(action, "CLOUD_ANALYZE")', confirm_start)
    confirm = firmware[confirm_start:confirm_end]
    assert "deviceConfirmedCloudStartAllowed(duration, requestId)" in confirm


def test_only_formal_watering_updates_cooldown_timestamp():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    relay_start = firmware.index("void setValveRelay(bool open)")
    relay_end = firmware.index("bool jsonStringValue", relay_start)
    relay_block = firmware[relay_start:relay_end]

    assert "if (valveCountsForFormalCooldown && nowEpochUtc != 0)" in relay_block
    assert "deviceLastWateringEpochUtc = nowEpochUtc" in relay_block
    assert "Preferences" not in relay_block
    assert '"aiot_irrig"' not in relay_block
    assert '"last_epoch"' not in relay_block
    assert "valveCountsForFormalCooldown = false" in relay_block


def test_watering_cooldown_is_session_only_and_never_restored_from_nvs():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    load_start = firmware.index("static void deviceLoadIrrigationCounters()")
    load_end = firmware.index("static void deviceRestoreHistory", load_start)
    load_block = firmware[load_start:load_end]

    assert "deviceLastWateringEpochUtc = 0" in load_block
    assert "Preferences" not in load_block
    assert '"aiot_irrig"' not in firmware
    assert '"last_epoch"' not in firmware
    assert '"formal_v2"' not in firmware
    assert '"daily_sec"' not in firmware


def test_ui_ack_reports_gpio_level_without_claiming_physical_feedback():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    ack_start = firmware.rindex("static void emitDeviceUiAck")
    ack_end = firmware.index("void emitDeviceCloudResult", ack_start)
    ack = firmware[ack_start:ack_end]
    assert 'document["relayGpio"] = VALVE_RELAY_PIN' in ack
    assert "digitalRead(VALVE_RELAY_PIN)" in ack
    assert 'document["physicalFeedbackAvailable"] = false' in ack


def test_safety_close_publishes_v2_closed_state_for_dashboard_sync():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    close_start = firmware.index("void closeValveForSafety(")
    close_end = firmware.index("void handleValveCommand", close_start)
    close = firmware[close_start:close_end]

    assert "setValveRelay(false)" in close
    assert "sendValveAck(requestId, true, reason)" in close
    assert 'emitDeviceUiAck(requestId, true, "STOP_WATERING", reason)' in close
    assert "emitDeviceIrrigationState(requestId)" in close


def test_installed_relay_uses_high_level_trigger_with_low_safe_state():
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    assert "VALVE_RELAY_ACTIVE_HIGH = true" in firmware
    assert "open == VALVE_RELAY_ACTIVE_HIGH ? HIGH : LOW" in firmware


def test_timeout_and_heartbeat_are_independent_safety_gates():
    """超时关阀与心跳兜底必须是两个独立判断，不能用 else-if 互相短路。"""
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    start = firmware.index("void serviceUsbControl() {")
    end = firmware.index("// -------------------- Device-authoritative", start)
    block = firmware[start:end]
    # 两个 closeValveForSafety 调用必须各自独立成 if，不能是 else if。
    assert block.count("closeValveForSafety(\"duration_timeout_closed\")") == 1
    assert block.count("closeValveForSafety(\"host_heartbeat_timeout_closed\")") == 1
    assert "else if (valveOpen && valveRequiresHostHeartbeat" not in block


def test_cooldown_has_monotonic_fallback_when_clock_is_unset():
    """NTP 失效时冷却计时必须回退到单调 millis，避免自动灌溉硬超时后立刻再次开阀。"""
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    assert "uint32_t deviceLastWateringMs = 0;" in firmware
    assert "static bool deviceCooldownActive(uint32_t nowEpochUtc)" in firmware
    assert "deviceLastWateringMs = millis();" in firmware
    # 冷却兜底必须在关阀路径记录单调时钟。
    relay = firmware[firmware.index("void setValveRelay(bool open) {"):firmware.index("void closeValveForSafety(", firmware.index("void setValveRelay(bool open) {"))]
    assert "deviceLastWateringMs = millis();" in relay


def test_legacy_command_start_watering_uses_volume_closed_loop():
    """旧 @COMMAND 的 START_WATERING 也必须接入按升数闭环，不能绕过流量保护。"""
    firmware = (
        ROOT / "firmware" / "esp32_s3_all_sensors" / "esp32_s3_all_sensors.ino"
    ).read_text(encoding="utf-8")
    start = firmware.index("void handleValveCommand(")
    end = firmware.index("void handleDisplayCommand", start)
    block = firmware[start:end]
    assert "deviceComputeTargetLiters()" in block
    assert "volumeWatering.targetLiters = targetLiters" in block
    assert "volumeWatering.startPulseCount = flow.pulseCount" in block
