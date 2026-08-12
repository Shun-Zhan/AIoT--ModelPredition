from dual_forecast.cli import _write_cloud_env, parser
from dual_forecast.esp32_receiver import (
    FAST_TEST_DATABASE,
    _prediction_interval_seconds,
    _receiver_database,
)


def test_serial_receiver_cli_accepts_a_local_usb_port():
    args = parser().parse_args(
        ["receive-esp32-serial", "--serial-port", "/dev/cu.wchusbserial10"]
    )

    assert args.serial_port == "/dev/cu.wchusbserial10"
    assert args.baudrate == 115200
    assert args.telemetry_prefix == "@TELEMETRY "


def test_offline_log_cli_defaults_to_interactive_management():
    args = parser().parse_args(
        ["offline-log", "--serial-port", "/dev/cu.usbmodem-test"]
    )

    assert args.serial_port == "/dev/cu.usbmodem-test"
    assert args.action == "interactive"
    assert args.output == "outputs/esp32-offline-log.csv"
    assert args.yes is False


def test_offline_log_cli_accepts_exported_csv_import():
    args = parser().parse_args([
        "offline-log", "--serial-port", "/dev/cu.usbmodem-test",
        "--action", "import", "--input", "outputs/esp32-offline-log.csv",
    ])

    assert args.action == "import"
    assert args.input == "outputs/esp32-offline-log.csv"


def test_cloud_configuration_preserves_unrelated_env_settings(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "AIOT_FARM_CROP=番茄\n"
        "VEI_API_KEY=old-key\n"
        "AIOT_AUTO_IRRIGATION_ENABLED=1\n",
        encoding="utf-8",
    )

    _write_cloud_env("new-key", env_path)
    content = env_path.read_text(encoding="utf-8")

    assert "AIOT_FARM_CROP=番茄" in content
    assert "AIOT_AUTO_IRRIGATION_ENABLED=1" in content
    assert "VEI_API_KEY=new-key" in content
    assert "old-key" not in content
    assert content.count("VEI_API_KEY=") == 1


def test_fast_serial_receiver_uses_short_interval_and_isolated_database():
    args = parser().parse_args([
        "receive-esp32-serial",
        "--serial-port", "/dev/cu.test",
        "--fast-test",
        "--fast-test-interval-seconds", "2",
    ])

    assert _prediction_interval_seconds(args) == 2
    assert _receiver_database(args) == FAST_TEST_DATABASE
