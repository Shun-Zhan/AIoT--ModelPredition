from dual_forecast.cli import parser


def test_serial_receiver_cli_accepts_a_local_usb_port():
    args = parser().parse_args(
        ["receive-esp32-serial", "--serial-port", "/dev/cu.wchusbserial10"]
    )

    assert args.serial_port == "/dev/cu.wchusbserial10"
    assert args.baudrate == 115200
    assert args.telemetry_prefix == "@TELEMETRY "

